#!/usr/bin/env python3
"""Opt-in, billable: capture and verify GitHub Copilot CLI egress

(egress-sidecar-macos task 9.2; the capture behind design.md Decision 5.)

Records which hosts and paths the Copilot CLI uses, and checks that a
Copilot-only egress profile works while GitHub repository access stays blocked.
Every phase runs a real project-sandbox session (firewall mode) whose
--internet-proxy is a local, TLS-intercepting Squid fixture; the fixture's
throwaway CA is baked into the test image with --ca-cert.

Phases (default: all, in this order):
  capture  Copilot runs through an allow-all fixture that intercepts every
           host, so each request's method, host and path is logged.
  verify   Copilot runs through a fixture enforcing the Copilot-only profile
           (egress_policy.COPILOT_HOSTS), once per api.github.com variant until
           one passes:
             deny   api.github.com is not allowed at all
             paths  api.github.com is intercepted and only
                    egress_policy.COPILOT_API_PATHS are allowed
  block    A bash session probes GitHub repository access (git, REST, raw,
           codeload, gists) through the winning profile. Every probe must be
           denied by the fixture.

Needs a Copilot subscription and a token the CLI can resolve, exactly as
scripts/e2e-copilot.py: a fine-grained PAT with only "Copilot Requests" in pass
(copilot-github-token) or `gh auth login`. Each Copilot run is one short,
billable request. The token never enters the report: the fixture logs no
headers or query strings, and all output is redacted.

Writes copilot-egress-report.json, copilot-egress-report.md and the raw
fixture logs to --report-dir.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Generator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import jinja2

from project_sandbox import container_cli, copilot_auth, egress_policy

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "scripts" / "fixtures" / "copilot-egress"
PHASES = ("capture", "verify", "block")
VARIANTS = ("deny", "paths")
PERSONAL_HOST = copilot_auth.DEFAULT_HOST
OK_MARKER = "COPILOT_EGRESS_OK_7C2E"
FIXTURE_PORT = 3128
# Denials the Copilot CLI tolerates, blocked on purpose (design.md Decision 5):
# its update check is repository API, and no telemetry may leave the sandbox.
# (method, host regex, path regex); reported, but not a failure.
EXPECTED_DENIALS: tuple[tuple[str, str, str], ...] = (
    ("GET", r"^api\.github\.com$", r"^/repos/github/copilot-cli/releases"),
    ("CONNECT", r"^telemetry\.[a-z0-9-]+\.githubcopilot\.com$", r"^$"),
)
GITHUB_REPO_HOSTS = (
    "github.com",
    "api.github.com",
    "raw.githubusercontent.com",
    "codeload.github.com",
    "gist.github.com",
)

# Runs inside the sandbox (bash session) for the `block` phase. The fixture
# log is the oracle; this only drives the requests and records what the
# client saw.
BLOCK_PROBE_SOURCE = r"""#!/usr/bin/env python3
import json
import os
import subprocess
import urllib.error
import urllib.request

REPO = "octocat/Hello-World"
URLS = [
    f"https://github.com/{REPO}",
    f"https://github.com/{REPO}.git/info/refs?service=git-upload-pack",
    f"https://api.github.com/repos/{REPO}",
    f"https://api.github.com/repos/{REPO}/contents/README",
    f"https://raw.githubusercontent.com/{REPO}/master/README",
    f"https://codeload.github.com/{REPO}/tar.gz/refs/heads/master",
    "https://api.github.com/gists/public",
    "https://gist.github.com/",
]
results = []
for url in URLS:
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            results.append({"probe": url, "client": f"http {response.status}"})
    except urllib.error.HTTPError as exc:
        server = exc.headers.get("Server", "")
        results.append({"probe": url, "client": f"http {exc.code} server={server}"})
    except Exception as exc:  # reset, refused, TLS failure
        results.append({"probe": url, "client": f"error {type(exc).__name__}"})
git = subprocess.run(
    ["git", "ls-remote", f"https://github.com/{REPO}.git"],
    capture_output=True, text=True, timeout=60,
    env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
)
results.append({"probe": "git ls-remote", "client": f"exit {git.returncode}"})
with open("copilot-egress-block.json", "w", encoding="utf-8") as handle:
    json.dump(results, handle, indent=2)
print("BLOCK_PROBES_DONE")
"""

LOG_RE = re.compile(
    r"FIXTURE (?P<ts>\d+\.\d+) (?P<status>\S+)/(?P<code>\d+) (?P<method>\S+) "
    r"(?P<url>\S+) sni=(?P<sni>\S*) bump=(?P<bump>\S*) err=(?P<err>\S*) "
    r"cause=(?P<cause>\S*)"
)
_ID_SEGMENT = re.compile(
    r"^(?:\d+|[0-9a-f]{8,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------
# Arguments
# --------------------------------------------------------------------------


def _csv(choices: tuple[str, ...]):
    def parse(value: str) -> tuple[str, ...]:
        items = tuple(item.strip() for item in value.split(",") if item.strip())
        unknown = [item for item in items if item not in choices]
        if not items or unknown:
            raise argparse.ArgumentTypeError(
                f"expected a comma-separated subset of {','.join(choices)}"
            )
        return items

    return parse


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--runtime",
        default="auto",
        choices=("auto", "apple-container", "docker", "podman"),
    )
    p.add_argument("--base-image", default="python:3.12-slim")
    p.add_argument("--image-tag", default="project-sandbox-e2e-copilot-egress:latest")
    p.add_argument(
        "--phases",
        type=_csv(PHASES),
        default=PHASES,
        help="comma-separated phases to run (default: capture,verify,block)",
    )
    p.add_argument(
        "--variants",
        type=_csv(VARIANTS),
        default=("paths",),
        help=(
            "api.github.com variants to try in verify, in order (default: paths; "
            "the 2026-11-04 capture found the CLI fails with deny)"
        ),
    )
    p.add_argument(
        "--extra-host",
        action="append",
        default=[],
        metavar="HOST",
        help="add a host (or *.domain) to the verified profile; repeatable",
    )
    p.add_argument(
        "--api-path",
        action="append",
        default=[],
        metavar="REGEX",
        help="add an api.github.com path regex for the `paths` variant; repeatable",
    )
    p.add_argument(
        "--report-dir",
        type=Path,
        help="where to write the report (default: a new directory under .project-sandbox/)",
    )
    p.add_argument("--model", help="Copilot model (default: the agent's)")
    p.add_argument("--timeout", type=int, default=300)
    # No --no-build: every run bakes a fresh fixture CA into the image via --ca-cert.
    p.add_argument("--keep", action="store_true", help="keep the test project")
    return p


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = parser()
    args = p.parse_args(argv)
    for host in args.extra_host:
        try:
            egress_policy.normalize_host_pattern(host, source="--extra-host")
        except egress_policy.EgressPolicyError as exc:
            p.error(str(exc))
    for pattern in args.api_path:
        if any(ch.isspace() for ch in pattern):
            p.error(f"--api-path must not contain whitespace: {pattern!r}")
        try:
            re.compile(pattern)
        except re.error as exc:
            p.error(f"--api-path {pattern!r} is not a valid regex: {exc}")
    if "block" in args.phases and "verify" not in args.phases:
        p.error("the block phase needs the verify phase to pick a profile")
    return args


# --------------------------------------------------------------------------
# Profile and fixture config
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Profile:
    name: str
    hosts: tuple[str, ...]
    bump_hosts: tuple[str, ...] = ()
    bump_paths: tuple[str, ...] = ()


def copilot_profile(
    variant: str, extra_hosts: list[str] | tuple[str, ...] = (), api_paths=()
) -> Profile:
    hosts = [*egress_policy.COPILOT_HOSTS]
    hosts += [
        egress_policy.normalize_host_pattern(h, source="--extra-host")
        for h in extra_hosts
    ]
    if variant == "deny":
        return Profile("deny", _squid_hosts(hosts))
    if variant == "paths":
        api = egress_policy.COPILOT_API_HOST
        return Profile(
            "paths",
            _squid_hosts([*hosts, api]),
            bump_hosts=(api,),
            bump_paths=(*egress_policy.COPILOT_API_PATHS, *api_paths),
        )
    raise ValueError(f"unknown variant: {variant}")


def _squid_hosts(hosts) -> tuple[str, ...]:
    """Host patterns as Squid dstdomain entries (``*.x`` -> ``.x``), deduped."""
    rendered = []
    for host in egress_policy._dedupe_hosts(hosts):
        rendered.append(host[1:] if host.startswith("*.") else host)
    return tuple(rendered)


def render_squid_conf(mode: str, profile: Profile | None = None) -> str:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(FIXTURE_DIR),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("squid.conf.j2")
    if mode == "capture":
        return template.render(mode="capture")
    assert profile is not None
    return template.render(
        mode="verify",
        hosts=profile.hosts,
        bump_hosts=profile.bump_hosts,
        bump_paths=profile.bump_paths,
    )


# --------------------------------------------------------------------------
# Log parsing and summaries
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    status: str
    code: int
    method: str
    host: str
    path: str
    sni: str
    bump: str
    err: str
    cause: str

    @property
    def denied(self) -> bool:
        return (
            self.status.startswith("TCP_DENIED")
            or self.err == "TCP_RESET"
            or self.bump == "terminate"
        )


def parse_log(text: str) -> list[Entry]:
    entries = []
    for line in text.splitlines():
        match = LOG_RE.search(line)
        if not match:
            continue
        method, url = match["method"], match["url"]
        if method == "-":
            # Squid's record for a connection closed before any request, such
            # as the fixture readiness probe; not traffic.
            continue
        if method == "CONNECT":
            host, _, _port = url.rpartition(":")
            path = ""
        else:
            parts = urlsplit(url)
            host, path = parts.hostname or "", parts.path or "/"
        entries.append(
            Entry(
                status=match["status"],
                code=int(match["code"]),
                method=method,
                host=host.lower(),
                path=path,
                sni=_dash(match["sni"]),
                bump=_dash(match["bump"]),
                err=_dash(match["err"]),
                cause=_dash(match["cause"]),
            )
        )
    return entries


def _dash(value: str) -> str:
    return "" if value == "-" else value


def path_template(path: str) -> str:
    """Collapse ids, hashes and UUIDs so repeated requests group together."""
    if not path:
        return ""
    segments = ["{id}" if _ID_SEGMENT.match(s) else s for s in path.split("/")]
    return "/".join(segments)


def summarize_requests(entries: list[Entry]) -> list[dict]:
    """One row per (method, host, path template), CONNECTs excluded."""
    rows: dict[tuple[str, str, str], dict] = {}
    for entry in entries:
        if entry.method == "CONNECT":
            continue
        key = (entry.method, entry.host, path_template(entry.path))
        row = rows.setdefault(
            key,
            {"method": key[0], "host": key[1], "path": key[2], "count": 0, "codes": []},
        )
        row["count"] += 1
        if entry.code not in row["codes"]:
            row["codes"].append(entry.code)
    return sorted(rows.values(), key=lambda r: (r["host"], r["path"], r["method"]))


def hosts_seen(entries: list[Entry]) -> list[str]:
    return sorted({e.host for e in entries if e.host})


def is_expected_denial(row: dict) -> bool:
    return any(
        row["method"] == method
        and re.search(host, row["host"])
        and re.search(path, row["path"])
        for method, host, path in EXPECTED_DENIALS
    )


def denials(entries: list[Entry]) -> list[dict]:
    seen: dict[tuple, dict] = {}
    for entry in entries:
        if not entry.denied:
            continue
        key = (
            entry.method,
            entry.host,
            path_template(entry.path),
            entry.cause or entry.err,
        )
        row = seen.setdefault(
            key,
            {
                "method": key[0],
                "host": key[1],
                "path": key[2],
                "cause": key[3],
                "count": 0,
            },
        )
        row["count"] += 1
    return list(seen.values())


def github_leaks(entries: list[Entry]) -> list[dict]:
    """GitHub repository-host transactions the fixture did not deny.

    A spliced or bumped CONNECT that the profile allowed for Copilot's own
    token exchange is not a leak; a request on such a tunnel is, unless it
    was denied.
    """
    leaks = []
    for entry in entries:
        if entry.host not in GITHUB_REPO_HOSTS or entry.denied:
            continue
        if entry.method == "CONNECT" and entry.bump == "bump":
            continue  # the requests inside the tunnel are logged separately
        leaks.append(asdict(entry))
    return leaks


# --------------------------------------------------------------------------
# Runtime helpers
# --------------------------------------------------------------------------


def fixture_cli(runtime: container_cli.Runtime) -> str:
    """CLI that hosts the Squid fixture: Apple's `container`, else Docker."""
    if runtime == container_cli.APPLE_CONTAINER:
        return "container"
    if shutil.which("docker") is None:
        raise SystemExit(
            "the Squid fixture runs on Docker for this runtime; install Docker"
        )
    return "docker"


def fixture_image_tag() -> str:
    digest = hashlib.sha256()
    for path in sorted(FIXTURE_DIR.iterdir()):
        if path.is_file() and path.suffix != ".j2":
            digest.update(path.name.encode() + b"\0" + path.read_bytes())
    return f"project-sandbox-e2e-copilot-egress-squid:{digest.hexdigest()[:12]}"


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def checked(command: list[str], *, timeout: int = 600) -> str:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            + (result.stdout + result.stderr)[-2000:]
        )
    return result.stdout


@contextlib.contextmanager
def running_fixture(
    cli: str, image: str, workdir: Path, name: str
) -> Generator[tuple[int, list[str]], None, None]:
    """Start the fixture, yield (port, log lines holder), always remove it."""
    port = free_loopback_port()
    logs: list[str] = []
    checked(
        [
            cli,
            "run",
            "-d",
            "--name",
            name,
            "-p",
            f"127.0.0.1:{port}:{FIXTURE_PORT}",
            "-v",
            f"{workdir}:/fixture",
            image,
            "serve",
        ]
    )
    try:
        wait_ready(cli, name, port)
        yield port, logs
    finally:
        result = subprocess.run(
            [cli, "logs", name],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
        logs.append(result.stdout + result.stderr)
        subprocess.run([cli, "rm", "-f", name], capture_output=True, check=False)


def wait_ready(cli: str, name: str, port: int, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        logs = subprocess.run(
            [cli, "logs", name],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
        text = logs.stdout + logs.stderr
        if "FATAL" in text:
            raise RuntimeError("Squid fixture failed to start:\n" + text[-2000:])
        if "Accepting" in text:
            with contextlib.suppress(OSError):
                socket.create_connection(("127.0.0.1", port), timeout=2).close()
                return
        time.sleep(1)
    raise RuntimeError(f"Squid fixture {name} was not ready within {timeout:.0f}s")


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------


@dataclass
class Run:
    returncode: int
    output: str
    log_text: str


def redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def session_command(
    args: argparse.Namespace,
    project: Path,
    *,
    agent: str,
    prompt: str,
    port: int,
    ca_cert: Path,
    log_path: Path,
) -> list[str]:
    """project-sandbox argv for one session. Never carries the token."""
    command = [
        sys.executable,
        "-m",
        "project_sandbox",
        "--runtime",
        args.runtime,
        "--image-tag",
        args.image_tag,
        "--agent",
        agent,
        "--internet-proxy",
        f"http://127.0.0.1:{port}",
        "--ca-cert",
        str(ca_cert),
        "--prompt-text",
        prompt,
        "--log",
        str(log_path),
        "--timeout",
        str(args.timeout),
    ]
    if agent == "copilot" and args.model:
        command += ["--model", args.model]
    return [*command, str(project), args.base_image]


def run_session(command: list[str], log_path: Path, secrets: list[str]) -> Run:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, errors="replace", check=False
    )
    log_text = log_path.read_text(errors="replace") if log_path.exists() else ""
    return Run(
        completed.returncode,
        redact(completed.stdout + completed.stderr, secrets),
        redact(log_text, secrets),
    )


@dataclass
class PhaseResult:
    phase: str
    profile: str
    ok: bool
    failures: list[str] = field(default_factory=list)
    hosts: list[str] = field(default_factory=list)
    requests: list[dict] = field(default_factory=list)
    denials: list[dict] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


class Suite:
    def __init__(
        self, args, runtime, project: Path, workdir: Path, report_dir: Path, secrets
    ):
        self.args = args
        self.cli = fixture_cli(runtime)
        self.image = fixture_image_tag()
        self.project = project
        self.workdir = workdir
        self.report_dir = report_dir
        self.secrets = secrets
        self.ca_cert = workdir / "ca" / "ca.pem"
        self.counter = 0

    def prepare(self) -> None:
        inspected = subprocess.run(
            [self.cli, "image", "inspect", self.image], capture_output=True, check=False
        )
        if inspected.returncode != 0:
            print(f"Building Squid fixture image {self.image}", flush=True)
            checked(
                [self.cli, "build", "-t", self.image, str(FIXTURE_DIR)], timeout=1200
            )
        checked(
            [
                self.cli,
                "run",
                "--rm",
                "-v",
                f"{self.workdir}:/fixture",
                self.image,
                "make-ca",
            ]
        )
        if not self.ca_cert.is_file():
            raise RuntimeError("fixture CA was not written")

    def _session(
        self, label: str, conf: str, *, agent: str, prompt: str
    ) -> tuple[Run, list[Entry]]:
        self.counter += 1
        (self.workdir / "squid.conf").write_text(conf, encoding="utf-8")
        name = f"sbx-e2e-copilot-egress-{self.counter}-{int(time.time())}"
        log_path = self.project / ".e2e-logs" / f"{label}.log"
        with running_fixture(self.cli, self.image, self.workdir, name) as (port, logs):
            run = run_session(
                session_command(
                    self.args,
                    self.project,
                    agent=agent,
                    prompt=prompt,
                    port=port,
                    ca_cert=self.ca_cert,
                    log_path=log_path,
                ),
                log_path,
                self.secrets,
            )
        fixture_log = redact("".join(logs), self.secrets)
        (self.report_dir / f"fixture-{label}.log").write_text(
            fixture_log, encoding="utf-8"
        )
        (self.report_dir / f"session-{label}.txt").write_text(
            f"exit {run.returncode}\n\n{run.output}", encoding="utf-8"
        )
        return run, parse_log(fixture_log)

    def copilot(self, label: str, conf: str) -> tuple[Run, list[Entry], list[str]]:
        run, entries = self._session(
            label,
            conf,
            agent="copilot",
            prompt=f"Reply with exactly this text and nothing else: {OK_MARKER}",
        )
        failures = []
        if run.returncode != 0:
            failures.append(f"copilot exit {run.returncode}")
        if OK_MARKER not in run.log_text:
            failures.append("reply marker missing from the session log")
        if not entries:
            failures.append(
                "the fixture logged no traffic (does the CLI honour HTTPS_PROXY?)"
            )
        return run, entries, failures

    def capture(self) -> PhaseResult:
        _, entries, failures = self.copilot("capture", render_squid_conf("capture"))
        profile_hosts = set(egress_policy.COPILOT_HOSTS)
        seen = hosts_seen(entries)
        return PhaseResult(
            "capture",
            "allow-all (intercept everything)",
            ok=not failures,
            failures=failures,
            hosts=seen,
            requests=summarize_requests(entries),
            extra={
                "hosts_not_in_profile": [h for h in seen if h not in profile_hosts],
                "profile_hosts_unused": sorted(profile_hosts - set(seen)),
                "api_github_paths": sorted(
                    {
                        f"{e.method} {path_template(e.path)}"
                        for e in entries
                        if e.host == egress_policy.COPILOT_API_HOST
                        and e.method != "CONNECT"
                    }
                ),
            },
        )

    def verify(self, variant: str) -> tuple[PhaseResult, Profile]:
        profile = copilot_profile(variant, self.args.extra_host, self.args.api_path)
        _, entries, failures = self.copilot(
            f"verify-{variant}", render_squid_conf("verify", profile)
        )
        denied = denials(entries)
        unexpected = [d for d in denied if not is_expected_denial(d)]
        if unexpected:
            failures.append(
                f"{len(unexpected)} distinct request(s) denied by the profile"
            )
        return (
            PhaseResult(
                "verify",
                variant,
                ok=not failures,
                failures=failures,
                hosts=hosts_seen(entries),
                requests=summarize_requests(entries),
                denials=denied,
                extra={"profile": asdict(profile)},
            ),
            profile,
        )

    def block(self, profile: Profile) -> PhaseResult:
        (self.project / "copilot-egress-block.py").write_text(
            BLOCK_PROBE_SOURCE, encoding="utf-8"
        )
        result_file = self.project / "copilot-egress-block.json"
        result_file.unlink(missing_ok=True)
        run, entries = self._session(
            f"block-{profile.name}",
            render_squid_conf("verify", profile),
            agent="bash",
            prompt="python3 copilot-egress-block.py",
        )
        failures = []
        client = []
        try:
            client = json.loads(result_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            failures.append(f"block probes did not run (session exit {run.returncode})")
        leaks = github_leaks(entries)
        if leaks:
            failures.append(f"{len(leaks)} GitHub transaction(s) were not denied")
        if not [e for e in entries if e.host in GITHUB_REPO_HOSTS]:
            failures.append("the fixture saw no GitHub probe traffic")
        return PhaseResult(
            "block",
            profile.name,
            ok=not failures,
            failures=failures,
            hosts=hosts_seen(entries),
            denials=denials(entries),
            extra={"client": client, "leaks": leaks},
        )


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


def render_markdown(results: list[PhaseResult], winner: str | None) -> str:
    lines = ["# Copilot CLI egress report", ""]
    lines.append(
        f"Verified profile: **{winner}**"
        if winner
        else "Verified profile: **none passed**"
    )
    lines.append("")
    for result in results:
        status = "PASS" if result.ok else "FAIL"
        lines += [f"## {result.phase} ({result.profile}): {status}", ""]
        lines += [f"- {failure}" for failure in result.failures]
        if result.failures:
            lines.append("")
        if result.hosts:
            lines += ["Hosts: " + ", ".join(f"`{h}`" for h in result.hosts), ""]
        for key, value in result.extra.items():
            if key in ("profile", "client", "leaks") or not value:
                continue
            lines.append(f"{key}: " + ", ".join(f"`{v}`" for v in value))
        if any(
            result.extra.get(k)
            for k in result.extra
            if k not in ("profile", "client", "leaks")
        ):
            lines.append("")
        if result.requests:
            lines += [
                "| Method | Host | Path | Count | Codes |",
                "|---|---|---|---|---|",
            ]
            lines += [
                f"| {r['method']} | {r['host']} | `{r['path']}` | {r['count']} | "
                f"{', '.join(map(str, r['codes']))} |"
                for r in result.requests
            ]
            lines.append("")
        if result.denials:
            lines += [
                "Denied:",
                "",
                "| Method | Host | Path | Cause | Count | Expected |",
                "|---|---|---|---|---|---|",
            ]
            lines += [
                f"| {d['method']} | {d['host']} | `{d['path']}` | {d['cause']} | "
                f"{d['count']} | {'yes' if is_expected_denial(d) else 'no'} |"
                for d in result.denials
            ]
            lines.append("")
        if result.extra.get("client"):
            lines += ["Client view:", ""]
            lines += [
                f"- `{c['probe']}`: {c['client']}" for c in result.extra["client"]
            ]
            lines.append("")
    return "\n".join(lines)


def write_report(
    report_dir: Path, results: list[PhaseResult], winner: str | None
) -> None:
    payload = {
        "winner": winner,
        "profile_hosts": list(egress_policy.COPILOT_HOSTS),
        "candidate_api_paths": list(egress_policy.COPILOT_API_PATHS),
        "results": [asdict(r) for r in results],
    }
    (report_dir / "copilot-egress-report.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    (report_dir / "copilot-egress-report.md").write_text(
        render_markdown(results, winner), encoding="utf-8"
    )


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    runtime = container_cli.select_runtime(args.runtime)
    if runtime == container_cli.CHROOT:
        raise SystemExit("select apple-container, docker, or podman")
    token, source = copilot_auth.resolve_token(PERSONAL_HOST)
    if source == copilot_auth.SOURCE_GH:
        print(
            copilot_auth.scope_warning(copilot_auth.default_pass_entry(PERSONAL_HOST))
        )
    secrets = [token]

    temp_root = ROOT / ".project-sandbox"
    temp_root.mkdir(exist_ok=True)
    report_dir = args.report_dir or Path(
        tempfile.mkdtemp(prefix="copilot-egress-report.", dir=temp_root)
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    project = Path(tempfile.mkdtemp(prefix="e2e-copilot-egress.", dir=temp_root))
    (project / "README.md").write_text("# copilot egress e2e\n", encoding="utf-8")
    workdir = Path(tempfile.mkdtemp(prefix="copilot-egress-fixture."))
    workdir.chmod(0o700)

    suite = Suite(args, runtime, project, workdir, report_dir, secrets)
    results: list[PhaseResult] = []
    winner: Profile | None = None
    try:
        suite.prepare()
        if "capture" in args.phases:
            print("RUN:  capture", flush=True)
            results.append(suite.capture())
        if "verify" in args.phases:
            for variant in args.variants:
                print(f"RUN:  verify ({variant})", flush=True)
                result, profile = suite.verify(variant)
                results.append(result)
                if result.ok:
                    winner = profile
                    break
        if "block" in args.phases:
            if winner is None:
                results.append(
                    PhaseResult(
                        "block",
                        "-",
                        ok=False,
                        failures=["no verified profile to probe"],
                    )
                )
            else:
                print(f"RUN:  block ({winner.name})", flush=True)
                results.append(suite.block(winner))
    finally:
        write_report(report_dir, results, winner.name if winner else None)
        shutil.rmtree(workdir, ignore_errors=True)  # includes the fixture CA key
        if args.keep:
            print(f"Test project kept for inspection: {project}")
        else:
            shutil.rmtree(project, ignore_errors=True)

    for result in results:
        status = "PASS" if result.ok else "FAIL"
        print(f"{status}: {result.phase} ({result.profile})")
        for failure in result.failures:
            print(f"      {redact(failure, secrets)}")
    print(f"Report: {report_dir / 'copilot-egress-report.md'}")
    return 0 if suite_passed(results) else 1


def suite_passed(results: list[PhaseResult]) -> bool:
    """Every non-verify phase passed, and the last verify variant tried passed.

    Variants are tried in order until one passes, so earlier variant failures
    are findings (recorded in the report), not suite failures.
    """
    if not results:
        return False
    verify = [r for r in results if r.phase == "verify"]
    others = [r for r in results if r.phase != "verify"]
    return all(r.ok for r in others) and (not verify or verify[-1].ok)


if __name__ == "__main__":
    raise SystemExit(main())
