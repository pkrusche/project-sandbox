"""Host prerequisite checks for ``project-sandbox doctor``.

Every probe is read-only: it inspects PATH, asks each tool for its version or
status, and never starts services, builds images, or writes files.
"""

from __future__ import annotations

import json
import platform
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from . import (
    __version__,
    colima_mounts,
    colima_network,
    colima_profile,
    container_cli,
    egress_sidecar,
    local_service_network,
)

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


def _completed(argv) -> subprocess.CompletedProcess:
    """Adapt :func:`_run` (timeouts included) to egress_sidecar's runner shape."""
    probe = _run(list(argv))
    if probe is None:
        return subprocess.CompletedProcess(argv, 1, "", "probe timed out")
    return subprocess.CompletedProcess(
        argv, probe.returncode, probe.stdout, probe.stderr
    )


def check_egress_sidecars(checks: list[Check]) -> list[Check]:
    """The global egress sidecar of each reachable runtime, read-only.

    Only ``inspect`` is issued: doctor never starts, probes inside or changes
    the sidecar. ``project-sandbox-egress status`` gives the full report.
    """
    reachable = {
        check.name
        for check in checks
        if check.section == "runtimes"
        and check.status == OK
        # A stopped Apple container service cannot be asked without starting it.
        and "stopped" not in check.detail
    }
    result = []
    for runtime in (
        container_cli.APPLE_CONTAINER,
        container_cli.DOCKER,
        container_cli.PODMAN,
    ):
        if runtime.name not in reachable:
            continue
        info = egress_sidecar.inspect(runtime, _completed)
        if info is None:
            detail = "not running (the first --egress sidecar launch starts it)"
        elif info.running:
            where = (
                f" on {egress_sidecar.endpoint(info.port, info.address)}"
                if info.port is not None
                else " (not started by this version; the next launch replaces it)"
            )
            detail = f"{egress_sidecar.CONTAINER} running{where}"
        else:
            detail = f"{egress_sidecar.CONTAINER} stopped (the next launch replaces it)"
        result.append(Check("egress", f"{runtime.name} sidecar", INFO, detail))
    return result


def _verify_runner(argv, **_kwargs) -> subprocess.CompletedProcess:
    """Adapt :func:`_completed` to the keyword-taking runner of the verifiers."""
    return _completed(argv)


def _colima_resources(runtime: container_cli.Runtime, profile: str) -> Check:
    """The VM's size against the CPUs a sandbox asks for by default.

    Docker refuses a CPU limit above the VM's CPUs, so a sandbox started without
    --cpus is capped to them (a 2-CPU Colima VM runs it with 2, not the default
    4); a memory limit above its RAM is allowed, so only CPUs are reported.
    """
    probe = _run(runtime.argv("info", "--format", "{{.NCPU}} {{.MemTotal}}"))
    try:
        assert probe is not None and probe.returncode == 0
        cpus, total = (int(x) for x in probe.stdout.split())
    except (AssertionError, ValueError):
        return Check("colima", "resources", INFO, "VM size not reported by Docker")
    wanted = container_cli.DEFAULT_CPUS
    gib = total / 1024**3
    detail = f"{cpus} CPUs, {gib:.1f} GiB"
    if cpus < wanted:
        return Check(
            "colima",
            "resources",
            INFO,
            f"{detail}; sandboxes are capped to {cpus} CPUs instead of the "
            f"default {wanted} because Docker refuses more than the VM has",
            f"To give sandboxes more: colima stop --profile {profile} && "
            f"colima start --profile {profile} --cpu {wanted}",
        )
    return Check("colima", "resources", OK, detail)


def _mount_hint(profile: str, missing, flags: str) -> str:
    if colima_profile.read_record(profile) is not None:
        return "; ".join(
            f"project-sandbox colima add-root {shlex.quote(str(m))}"
            + (
                ""
                if profile == container_cli.COLIMA_DEFAULT_PROFILE
                else f" --colima-profile {profile}"
            )
            for m in missing
        )
    return (
        "`colima start --mount` replaces the profile's mounts, so list "
        f"every export: colima stop --profile {profile} && colima start "
        f"--profile {profile} {flags}"
    )


def _colima_exports(runtime: container_cli.Runtime, profile: str) -> list[Check]:
    """Whether the profile exports the home directory, and (with a roots record)
    whether its exports are exactly the expected set. Read-only."""
    exports = colima_mounts.exported_mounts(
        runtime, run=_verify_runner, which=shutil.which
    )
    if exports is None:
        return []
    checks: list[Check] = []
    home = colima_profile.home_exports(exports)
    if home:
        checks.append(
            Check(
                "colima",
                "exports",
                WARN,
                "the whole home directory is readable and writable from the VM "
                f"(exported: {', '.join(e.path for e in home)})",
                "Create a dedicated profile that exports only your workspace: "
                "project-sandbox colima init --workspace-root PATH",
            )
        )
    roots = colima_profile.read_record(profile)
    if roots is None:
        return checks
    extra, missing = colima_profile.export_drift(
        roots, exports, colima_mounts.required_state_dirs()
    )
    if extra or missing:
        parts = []
        if extra:
            parts.append("unexpected export(s): " + ", ".join(extra))
        if missing:
            parts.append("expected but not exported: " + ", ".join(missing))
        checks.append(
            Check(
                "colima",
                "exports",
                WARN,
                "; ".join(parts),
                "Add a root with `project-sandbox colima add-root PATH`; "
                "`colima start --mount` replaces exports instead of adding to them",
            )
        )
    elif not home:
        checks.append(
            Check(
                "colima",
                "exports",
                OK,
                "dedicated: only the workspace roots and the sandbox state directories",
            )
        )
    return checks


def check_colima(profile: str) -> list[Check]:
    """A Colima profile, read-only: context, engine, mounts, networks, sidecar.

    Never starts the VM or creates a network. The mount probe is one read-only
    ``colima ssh`` call that tests the current directory and the state
    directory.
    """
    runtime = container_cli.colima_runtime(profile)
    name = f"colima ({profile})"
    if shutil.which(runtime.binary) is None:
        return [Check("colima", name, INFO, "docker not found on PATH")]
    try:
        container_cli.verify_colima_profile(runtime, run=_verify_runner)
    except SystemExit as exc:
        message = str(exc)
        detail, _, hint = message.partition(" Run: ")
        # A missing context only means the profile was never started.
        status = INFO if "does not exist" in detail else WARN
        return [Check("colima", name, status, detail, f"Run: {hint}" if hint else None)]
    version = _run(runtime.argv("version", "--format", "{{.Server.Version}}"))
    detail = (
        f"{_version(version.stdout) or version.stdout.strip()}; "
        if version is not None and version.returncode == 0
        else ""
    )
    checks = [
        Check(
            "colima",
            name,
            OK,
            f"{detail}profile running, context {runtime.context}",
        )
    ]

    checks.append(_colima_resources(runtime, profile))

    # The state directories the sandbox bind-mounts, not the host-only session
    # records; ones that do not exist yet cannot be exported and are skipped.
    paths = [
        Path.cwd(),
        *(d for d in colima_mounts.required_state_dirs() if d.exists()),
    ]
    try:
        missing = colima_mounts.missing_in_vm(
            runtime,
            paths,
            run=_verify_runner,
            which=shutil.which,
        )
    except SystemExit as exc:
        checks.append(Check("colima", "mounts", WARN, str(exc).splitlines()[0]))
    else:
        if missing is None:
            checks.append(
                Check(
                    "colima",
                    "mounts",
                    INFO,
                    "colima not on PATH; mount visibility not checked",
                )
            )
        elif missing:
            exports = colima_mounts.exported_mounts(
                runtime, run=_verify_runner, which=shutil.which
            )
            flags = (
                colima_mounts.mount_flags(exports, missing)
                if exports is not None
                else "<current exports> " + " ".join(f"--mount {m}:w" for m in missing)
            )
            checks.append(
                Check(
                    "colima",
                    "mounts",
                    WARN,
                    "not visible in the VM: " + ", ".join(str(m) for m in missing),
                    _mount_hint(profile, missing, flags),
                )
            )
        else:
            checks.append(
                Check(
                    "colima",
                    "mounts",
                    OK,
                    "current directory and the state directories the sandbox mounts are visible",
                )
            )

    checks += _colima_exports(runtime, profile)

    for spec in colima_network.SPECS:
        record = colima_network.inspect(runtime, _completed, spec.name)
        if record is None:
            checks.append(
                Check(
                    "colima",
                    spec.name,
                    INFO,
                    "not created yet (the first --egress sidecar launch creates it)",
                )
            )
            continue
        try:
            colima_network.validate(runtime, record, spec)
        except colima_network.NetworkError as exc:
            checks.append(Check("colima", spec.name, WARN, str(exc)))
        else:
            checks.append(Check("colima", spec.name, OK, f"owned, {spec.role}"))

    info = egress_sidecar.inspect(runtime, _completed)
    if info is None:
        sidecar = Check(
            "egress",
            f"{runtime.key} sidecar",
            INFO,
            "not running (the first --egress sidecar launch starts it)",
        )
    elif not info.running:
        sidecar = Check(
            "egress",
            f"{runtime.key} sidecar",
            INFO,
            f"{egress_sidecar.CONTAINER} stopped (the next launch replaces it)",
        )
    elif not egress_sidecar._attached(info):
        sidecar = Check(
            "egress",
            f"{runtime.key} sidecar",
            WARN,
            f"{egress_sidecar.CONTAINER} running but not attached to "
            f"{colima_network.SANDBOX_NETWORK} as {colima_network.ALIAS!r}",
            "The next launch recreates it, or run `project-sandbox-egress "
            f"restart --runtime colima --colima-profile {profile}`",
        )
    else:
        sidecar = Check(
            "egress",
            f"{runtime.key} sidecar",
            INFO,
            f"{egress_sidecar.CONTAINER} running on "
            f"{colima_network.ALIAS}:{colima_network.PROXY_PORT}",
        )
    return [*checks, sidecar]


def run_checks(colima_profile: str | None = None) -> list[Check]:
    checks = [check_python()]
    checks += check_apple_container()
    checks.append(check_docker())
    checks.append(check_podman())
    checks.append(check_chroot())
    checks.append(auto_runtime_check(checks))
    checks += check_git()
    checks.append(check_jj())
    checks += check_egress_sidecars(checks)
    # Colima is opt-in: shown when asked for by name or when colima is installed.
    if colima_profile is not None or shutil.which("colima") is not None:
        checks += check_colima(colima_profile or container_cli.COLIMA_DEFAULT_PROFILE)
    return checks


_SECTION_TITLES = {
    "host": "Host",
    "runtimes": "Container runtimes",
    "vcs": "Version control",
    "colima": "Colima",
    "egress": "Egress sidecar",
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
    usage = "usage: project-sandbox doctor [--json] [--colima-profile NAME]"
    args = list(argv)
    as_json = "--json" in args
    args = [a for a in args if a != "--json"]
    profile = None
    if args[:1] == ["--colima-profile"] and len(args) == 2:
        profile = args[1]
        if not container_cli._COLIMA_PROFILE_RE.fullmatch(profile):
            raise SystemExit(f"Invalid Colima profile name: {profile!r}")
    elif args:
        raise SystemExit(usage)
    checks = run_checks(profile)
    ok = not any(check.status == FAIL for check in checks)
    if as_json:
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
