"""Host prerequisite checks for ``project-sandbox doctor``.

Every probe is read-only: it inspects PATH, asks each tool for its version or
status, and never starts services, builds images, or writes files.
"""

from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass

from . import __version__, container_cli, local_service_network

OK = "ok"
WARN = "warn"
FAIL = "fail"
INFO = "info"
_LABELS = {OK: "[ok]  ", WARN: "[warn]", FAIL: "[fail]", INFO: "[--]  "}

# A daemon that is installed but wedged can make `docker version` hang for a
# long time; doctor must always come back with an answer.
PROBE_TIMEOUT = 10
MIN_PYTHON = (3, 11)
JJ_NO_COLOCATE_HINT = "jj >= 0.46 is required for --branch in colocated jj repos"

_VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)?")


@dataclass(frozen=True)
class Check:
    section: str
    name: str
    status: str
    detail: str
    hint: str | None = None


@dataclass(frozen=True)
class Probe:
    returncode: int
    stdout: str
    stderr: str


def _run(argv: list[str]) -> Probe | None:
    """Run a read-only probe; None when it cannot be executed or times out."""
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=PROBE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return Probe(proc.returncode, proc.stdout, proc.stderr)


def _version(text: str) -> str | None:
    match = _VERSION_RE.search(text)
    return match.group(0) if match else None


def _first_line(probe: Probe) -> str:
    text = (probe.stderr or probe.stdout).strip()
    return text.splitlines()[0] if text else f"exit status {probe.returncode}"


def _auto_candidates() -> tuple[container_cli.Runtime, ...]:
    if sys.platform == "darwin":
        return (
            container_cli.APPLE_CONTAINER,
            container_cli.DOCKER,
            container_cli.PODMAN,
        )
    return (container_cli.DOCKER, container_cli.PODMAN)


def check_python() -> Check:
    current = sys.version_info[:3]
    detail = ".".join(str(part) for part in current)
    if current[:2] < MIN_PYTHON:
        return Check(
            "host",
            "python",
            FAIL,
            detail,
            f"project-sandbox requires Python {'.'.join(map(str, MIN_PYTHON))}+",
        )
    return Check("host", "python", OK, detail)


def check_apple_container() -> list[Check]:
    runtime = container_cli.APPLE_CONTAINER
    section = "runtimes"
    if sys.platform != "darwin":
        return [Check(section, runtime.name, INFO, "macOS only")]
    if shutil.which(runtime.binary) is None:
        return [
            Check(
                section,
                runtime.name,
                INFO,
                f"{runtime.binary} not found on PATH",
                "Install from https://github.com/apple/container/releases",
            )
        ]
    probe = _run([runtime.binary, "--version"])
    version = _version(probe.stdout) if probe and probe.returncode == 0 else None
    status = _run([runtime.binary, "system", "status"])
    # A stopped service is fine: direct runs call `container system start`.
    service = (
        "running"
        if status is not None and status.returncode == 0
        else "stopped (started on demand)"
    )
    checks = [
        Check(
            section,
            runtime.name,
            OK,
            f"{version or 'unknown version'}; system service {service}",
        )
    ]
    checks.append(check_apple_dns())
    return checks


def check_apple_dns() -> Check:
    """The host alias used by --agent-proxy, --internet-proxy, and --pi-ollama."""
    hostname = local_service_network.HOSTNAME
    name = f"dns {hostname}"
    probe = _run([container_cli.APPLE_CONTAINER.binary, "system", "dns", "list"])
    if probe is None or probe.returncode != 0:
        return Check(
            "runtimes",
            name,
            INFO,
            "could not list container DNS domains",
            "Needed only for host-local proxies and Ollama; see docs/apple-container-dns.md",
        )
    if hostname in probe.stdout.split():
        return Check("runtimes", name, OK, "localhost DNS mapping present")
    return Check(
        "runtimes",
        name,
        INFO,
        "localhost DNS mapping not configured",
        f"Needed only for host-local proxies and Ollama: "
        f"{local_service_network.APPLE_SETUP_COMMAND}",
    )


def check_docker() -> Check:
    runtime = container_cli.DOCKER
    if shutil.which(runtime.binary) is None:
        return Check("runtimes", runtime.name, INFO, "docker not found on PATH")
    # Asking for the server version proves the daemon is reachable, not just
    # that a client binary is installed.
    probe = _run([runtime.binary, "version", "--format", "{{.Server.Version}}"])
    if probe is None:
        return Check(
            "runtimes",
            runtime.name,
            WARN,
            f"daemon did not answer within {PROBE_TIMEOUT}s",
            "Start Docker (or Docker Desktop) and retry",
        )
    if probe.returncode != 0:
        return Check(
            "runtimes",
            runtime.name,
            WARN,
            f"daemon unreachable: {_first_line(probe)}",
            "Start the Docker daemon, or check membership of the docker group",
        )
    return Check(
        "runtimes",
        runtime.name,
        OK,
        f"{_version(probe.stdout) or probe.stdout.strip()}; daemon reachable",
    )


def check_podman() -> Check:
    runtime = container_cli.PODMAN
    if shutil.which(runtime.binary) is None:
        return Check("runtimes", runtime.name, INFO, "podman not found on PATH")
    probe = _run(
        [
            runtime.binary,
            "info",
            "--format",
            "{{.Version.Version}} {{.Host.Security.Rootless}}",
        ]
    )
    if probe is None or probe.returncode != 0:
        detail = (
            f"did not answer within {PROBE_TIMEOUT}s"
            if probe is None
            else f"unusable: {_first_line(probe)}"
        )
        hint = (
            "Start the Podman machine: podman machine start"
            if sys.platform == "darwin"
            else "Run `podman info` to diagnose the rootless setup"
        )
        return Check("runtimes", runtime.name, WARN, detail, hint)
    version, _, rootless = probe.stdout.strip().rpartition(" ")
    version = _version(version) or version
    if rootless.lower() != "true":
        return Check(
            "runtimes",
            runtime.name,
            WARN,
            f"{version}; rootful, which project-sandbox refuses to use",
            "Switch to rootless: podman machine set --rootful=false"
            if sys.platform == "darwin"
            else "Run project-sandbox as a normal user with rootless podman",
        )
    return Check("runtimes", runtime.name, OK, f"{version}; rootless, ready")


def check_chroot() -> Check:
    runtime = container_cli.CHROOT
    if not sys.platform.startswith("linux"):
        return Check("runtimes", runtime.name, INFO, "Linux only")
    if shutil.which(runtime.binary) is None:
        return Check(
            "runtimes",
            runtime.name,
            INFO,
            "unshare not found on PATH",
            "Install util-linux to use --runtime chroot",
        )
    probe = _run([runtime.binary, "--map-root-user", "--mount", "true"])
    if probe is None or probe.returncode != 0:
        return Check(
            "runtimes",
            runtime.name,
            WARN,
            "unprivileged user namespaces unavailable"
            + ("" if probe is None else f": {_first_line(probe)}"),
            "Only --runtime chroot needs this; check kernel.unprivileged_userns_clone "
            "and AppArmor userns restrictions",
        )
    return Check("runtimes", runtime.name, OK, "unprivileged user namespaces available")


def check_socat() -> Check | None:
    if not sys.platform.startswith("linux"):
        return None
    if shutil.which("socat") is None:
        return Check(
            "tools",
            "socat",
            INFO,
            "not found on PATH",
            "Needed only for --pi-ollama / host-local proxies on Linux bridge networks",
        )
    return Check("tools", "socat", OK, "found")


def check_git() -> list[Check]:
    if shutil.which("git") is None:
        return [
            Check(
                "vcs",
                "git",
                FAIL,
                "not found on PATH",
                "Install git; it supplies the sandbox identity and --branch worktrees",
            )
        ]
    probe = _run(["git", "--version"])
    version = _version(probe.stdout) if probe and probe.returncode == 0 else None
    checks = [Check("vcs", "git", OK, version or "unknown version")]
    missing = [key for key in ("user.name", "user.email") if not _git_global(key)]
    if missing:
        checks.append(
            Check(
                "vcs",
                "git identity",
                WARN,
                f"global {' and '.join(missing)} not set",
                "Commits inside the sandbox will lack an author: "
                "git config --global user.name/user.email",
            )
        )
    else:
        checks.append(
            Check("vcs", "git identity", OK, "global user.name and user.email set")
        )
    return checks


def _git_global(key: str) -> str | None:
    probe = _run(["git", "config", "--global", "--get", key])
    if probe is None or probe.returncode != 0:
        return None
    return probe.stdout.strip() or None


def check_jj() -> Check:
    if shutil.which("jj") is None:
        return Check(
            "vcs",
            "jj",
            INFO,
            "not found on PATH",
            "Needed only for --branch in jj repositories",
        )
    probe = _run(["jj", "--version"])
    version = _version(probe.stdout) if probe and probe.returncode == 0 else None
    # Same capability probe jj_workspace uses, so doctor agrees with --branch.
    help_probe = _run(["jj", "workspace", "add", "--help"])
    if help_probe is None or "--no-colocate" not in help_probe.stdout:
        return Check(
            "vcs",
            "jj",
            WARN,
            f"{version or 'unknown version'}; no `workspace add --no-colocate`",
            JJ_NO_COLOCATE_HINT,
        )
    return Check(
        "vcs", "jj", OK, f"{version or 'unknown version'}; supports --no-colocate"
    )


def auto_runtime_check(checks: list[Check]) -> Check:
    """Report what --runtime auto picks and whether that pick is usable.

    Auto-selection only looks at PATH, so an installed engine with a stopped
    daemon still wins; that is the case doctor most needs to surface. A broken
    engine that auto would not pick is only a warning on its own line.
    """
    by_name = {check.name: check for check in checks if check.section == "runtimes"}
    candidates = _auto_candidates()
    for runtime in candidates:
        if shutil.which(runtime.binary) is None:
            continue
        picked = by_name.get(runtime.name)
        if picked is not None and picked.status != OK:
            usable = [
                other.name
                for other in candidates
                if other is not runtime
                and by_name.get(other.name) is not None
                and by_name[other.name].status == OK
            ]
            hint = (
                f"Fix {runtime.name} or pass --runtime {usable[0]}"
                if usable
                else f"Fix {runtime.name} before running a sandbox"
            )
            return Check(
                "runtimes",
                "auto",
                FAIL,
                f"selects {runtime.name}, which is unusable",
                hint,
            )
        return Check("runtimes", "auto", OK, f"selects {runtime.name}")
    names = ", ".join(runtime.name for runtime in candidates)
    return Check(
        "runtimes",
        "auto",
        FAIL,
        "no supported container runtime on PATH",
        f"Install one of: {names}",
    )


def run_checks() -> list[Check]:
    checks = [check_python()]
    checks += check_apple_container()
    checks.append(check_docker())
    checks.append(check_podman())
    checks.append(check_chroot())
    checks.append(auto_runtime_check(checks))
    socat = check_socat()
    if socat is not None:
        checks.append(socat)
    checks += check_git()
    checks.append(check_jj())
    return checks


_SECTION_TITLES = {
    "host": "Host",
    "runtimes": "Container runtimes",
    "tools": "Networking tools",
    "vcs": "Version control",
}


def render_text(checks: list[Check]) -> str:
    lines = [
        f"project-sandbox {__version__} doctor ({sys.platform}, {platform.machine()})"
    ]
    width = max(len(check.name) for check in checks)
    section = None
    for check in checks:
        if check.section != section:
            section = check.section
            lines += ["", _SECTION_TITLES.get(section, section)]
        lines.append(f"  {_LABELS[check.status]} {check.name:<{width}}  {check.detail}")
        if check.hint and check.status != OK:
            lines.append(f"  {'':6} {'':<{width}}  -> {check.hint}")
    failures = sum(check.status == FAIL for check in checks)
    warnings = sum(check.status == WARN for check in checks)
    lines += ["", f"{failures} failure(s), {warnings} warning(s)"]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if any(item != "--json" for item in argv):
        raise SystemExit("usage: project-sandbox doctor [--json]")
    checks = run_checks()
    ok = not any(check.status == FAIL for check in checks)
    if "--json" in argv:
        print(
            json.dumps(
                {
                    "ok": ok,
                    "version": __version__,
                    "platform": sys.platform,
                    "checks": [asdict(check) for check in checks],
                },
                indent=2,
            )
        )
    else:
        print(render_text(checks))
    return 0 if ok else 1
