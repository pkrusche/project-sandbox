#!/usr/bin/env python3
"""Opt-in end-to-end checks for the global --egress sidecar.

(add-squid-egress-sidecar 9.1; global-egress-sidecar-macos 9.1)

Runs real project-sandbox launches (agent: bash, no credentials, no billable
calls) against the runtime's global sidecar, managed with
project-sandbox-egress, and grades each check from both sides: what the client
inside the sandbox saw, and what the sidecar logged.

Checks (default: all):
  policy     one sidecar session with in-sandbox probes:
               allowed-splice, unlisted-connect, unlisted-http, ip-literal,
               port, metadata (literal, and a name resolving to it),
               sni-mismatch, no-sni, raw-tunnel, direct-egress, dns,
               github-blocked, concurrency; plus the session-end denial count,
               session-input cleanup and the sidecar outliving the session
  shared     two concurrent sandboxes on one sidecar, reuse across launches, a
             live `allow add` reaching both, and `down` cutting both off
  parent     sessions behind an intercepting fixture parent proxy with its own
             CA ([egress.upstream]): allowed traffic reaches the parent, denied
             traffic never does. On Apple container the parent is reached
             through the sidecar-owned host relay, which must survive the first
             session's process group being hung up (a closed terminal).
  forward    `forward add` of a loopback host listener, refusal of peers
             outside the internal network, and `forward remove`

All state lives in a temporary XDG_CONFIG_HOME/XDG_STATE_HOME, never the
user's egress.toml. Only the user's `containers/` config directory is linked
into the temporary config home, so `podman` still finds its machine
connection. The global sidecar's name is fixed per runtime, so the
suite refuses to start while one exists (it would be replaced and stopped)
unless --replace-existing is given.

Needs network access, a container runtime and builds. The parent check builds
the Squid fixture from scripts/fixtures/copilot-egress (on Docker for Podman).
"""

from __future__ import annotations

import argparse
import http.server
import importlib.util
import json
import os
import re
import shutil
import signal
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from project_sandbox import (
    container_cli,
    egress_log,
    egress_relay,
    egress_sidecar,
)

ROOT = Path(__file__).resolve().parents[1]
CHECKS = ("policy", "shared", "parent", "forward")
ALLOWED_HOST = "pypi.org"
SECOND_ALLOWED_HOST = "files.pythonhosted.org"
UNLISTED_HOST = "example.com"
# Allowed live (`allow add`) while the shared sandboxes run.
LIVE_HOST = "example.org"
METADATA_NAME = "169.254.169.254.nip.io"

# Runs inside the sandbox; stdlib only. Writes egress-probe.json.
PROBE_SOURCE = r'''#!/usr/bin/env python3
import concurrent.futures, json, os, socket, ssl, subprocess, urllib.error, urllib.request

PROXY = ("egress-proxy", 3128)
ALLOWED, SECOND, UNLISTED, METADATA = __HOSTS__
results = {}


def record(name, ok, detail):
    results[name] = {"ok": bool(ok), "detail": str(detail)[:300]}


def connect_reply(target, timeout=8):
    """Send CONNECT; return the bytes received before close (b'' = reset/close)."""
    with socket.create_connection(PROXY, timeout=timeout) as s:
        s.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
        data = b""
        try:
            while b"\r\n\r\n" not in data:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        except (ConnectionResetError, socket.timeout):
            pass
        return data


def tunnel(target):
    s = socket.create_connection(PROXY, timeout=10)
    s.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = s.recv(4096)
        if not chunk:
            break
        head += chunk
    return s, head


def tls_via_tunnel(target, sni, verify=True):
    s, head = tunnel(target)
    if not head.startswith(b"HTTP/1.1 200"):
        s.close()
        return f"no tunnel: {head[:40]!r}"
    ctx = ssl.create_default_context()
    if not verify:
        # Grade on handshake completion only: a failure must come from the
        # sidecar terminating, not from certificate checks.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with ctx.wrap_socket(s, server_hostname=sni) as t:
            return f"handshake ok ({t.version()})"
    except (ssl.SSLError, OSError) as exc:
        return f"handshake failed: {type(exc).__name__}"


def denied_connect(name, target):
    try:
        data = connect_reply(target)
        record(name, not data.startswith(b"HTTP/1.1 200"), data[:40] or "no bytes (reset)")
    except OSError as exc:
        record(name, True, f"{type(exc).__name__}")


# allowed splice
try:
    with urllib.request.urlopen(f"https://{ALLOWED}/simple/", timeout=20) as r:
        record("allowed-splice", r.status == 200, r.status)
except Exception as exc:
    record("allowed-splice", False, repr(exc))

denied_connect("unlisted-connect", f"{UNLISTED}:443")
try:
    urllib.request.urlopen(f"http://{UNLISTED}/", timeout=10)
    record("unlisted-http", False, "reached the origin")
except urllib.error.HTTPError as exc:
    body = exc.read().decode(errors="replace")
    record("unlisted-http", exc.code == 403 and "project-sandbox-egress: host-not-allowed" in body, f"{exc.code} {body[:80]}")
except Exception as exc:
    record("unlisted-http", False, repr(exc))

for name, target in (("ip-literal-public", "1.1.1.1:443"), ("ip-literal-metadata", "169.254.169.254:443"),
                     ("ip-literal-loopback6", "[::1]:443"), ("port", f"{ALLOWED}:8443")):
    denied_connect(name, target)

# metadata by name: allowlisted but resolves to 169.254.169.254 -> no successful TLS
outcome = tls_via_tunnel(f"{METADATA}:443", METADATA)
record("metadata-name", not outcome.startswith("handshake ok"), outcome)

ok = tls_via_tunnel(f"{ALLOWED}:443", ALLOWED)
mismatch = tls_via_tunnel(f"{ALLOWED}:443", SECOND, verify=False)
record("sni-mismatch", ok.startswith("handshake ok") and not mismatch.startswith("handshake ok"), f"control={ok}; mismatch={mismatch}")
nosni = tls_via_tunnel(f"{ALLOWED}:443", None, verify=False)
record("no-sni", not nosni.startswith("handshake ok"), nosni)

try:
    s, head = tunnel(f"{ALLOWED}:443")
    s.sendall(f"GET / HTTP/1.1\r\nHost: {ALLOWED}\r\n\r\n".encode())
    s.settimeout(6)
    try:
        reply = s.recv(4096)
    except (ConnectionResetError, socket.timeout):
        reply = b""
    s.close()
    record("raw-tunnel", head.startswith(b"HTTP/1.1 200") and reply == b"", f"head={head[:20]!r} reply={reply[:40]!r}")
except OSError as exc:
    record("raw-tunnel", False, repr(exc))

direct = []
for host, port in (("1.1.1.1", 443), ("2606:4700:4700::1111", 443)):
    try:
        socket.create_connection((host, port), timeout=4).close()
        direct.append(f"{host} OPEN")
    except OSError:
        pass
try:
    socket.create_connection(("egress-proxy", 22), timeout=4).close()
    direct.append("sidecar:22 OPEN")
except OSError:
    pass
record("direct-egress", not direct, direct or "all blocked")
try:
    socket.getaddrinfo("example.org", 443)
    record("dns", False, "resolved")
except OSError as exc:
    record("dns", True, type(exc).__name__)

reached = []
for url in (f"https://github.com/octocat/Hello-World", "https://api.github.com/repos/octocat/Hello-World",
            "https://raw.githubusercontent.com/octocat/Hello-World/master/README",
            "https://codeload.github.com/octocat/Hello-World/tar.gz/refs/heads/master",
            "https://api.github.com/gists/public"):
    try:
        urllib.request.urlopen(url, timeout=10)
        reached.append(url)
    except urllib.error.HTTPError as exc:
        if exc.headers.get("Server", "").startswith("squid"):
            continue
        reached.append(f"{url} -> {exc.code}")
    except Exception:
        pass
git = subprocess.run(["git", "ls-remote", "https://github.com/octocat/Hello-World.git"],
                     capture_output=True, text=True, timeout=60, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
if git.returncode == 0:
    reached.append("git ls-remote succeeded")
record("github-blocked", not reached, reached or "all blocked")

with concurrent.futures.ThreadPoolExecutor(10) as pool:
    heads = list(pool.map(lambda _i: connect_reply(f"{ALLOWED}:443"), range(10)))
record("concurrency", all(h.startswith(b"HTTP/1.1 200") for h in heads), sum(h.startswith(b"HTTP/1.1 200") for h in heads))

with open("egress-probe.json", "w", encoding="utf-8") as fh:
    json.dump(results, fh, indent=2)
print("EGRESS_PROBE_DONE")
'''

# Causes the sidecar must have logged for the policy probes (host -> cause).
EXPECTED_LOG_CAUSES = (
    (UNLISTED_HOST, "host-not-allowed"),
    ("1.1.1.1", "ip-literal"),
    ("169.254.169.254", "ip-literal"),
    (ALLOWED_HOST, "port-not-allowed"),
    (METADATA_NAME, "private-address"),
    (ALLOWED_HOST, "sni-mismatch"),
    (ALLOWED_HOST, "non-tls-tunnel"),
    ("github.com", "host-not-allowed"),
)

# Parent-chain probes: allowed through, unlisted reset locally.
PARENT_PROBE_SOURCE = r"""#!/usr/bin/env python3
import json, socket, urllib.request
out = {}
try:
    with urllib.request.urlopen("https://__ALLOWED__/simple/", timeout=30) as r:
        out["allowed"] = r.status
except Exception as exc:
    out["allowed"] = repr(exc)
with socket.create_connection(("egress-proxy", 3128), timeout=8) as s:
    s.sendall(b"CONNECT __UNLISTED__:443 HTTP/1.1\r\nHost: __UNLISTED__:443\r\n\r\n")
    try:
        out["unlisted"] = s.recv(64).decode(errors="replace")
    except OSError as exc:
        out["unlisted"] = type(exc).__name__
json.dump(out, open("egress-parent.json", "w"))
"""


# Shared-sidecar probe: runs in two sandboxes at once, stepping on flag files
# the host writes into the project (the sandbox's /workspace).
SHARED_PROBE_SOURCE = r"""#!/usr/bin/env python3
import json, os, time, urllib.request

def get(url):
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as exc:
        return exc.code if "squid" not in exc.headers.get("Server", "") else repr(exc)
    except Exception as exc:
        return repr(exc)

def wait(name, timeout=300):
    deadline = time.time() + timeout
    while not os.path.exists(name) and time.time() < deadline:
        time.sleep(0.5)

out = {"before": get("https://__ALLOWED__/simple/"), "live_before": get("https://__LIVE__/")}
json.dump(out, open("shared-ready.json", "w"))
wait("go")
out["live_after"] = get("https://__LIVE__/")
json.dump(out, open("shared-live.json", "w"))
wait("down")
out["after_down"] = get("https://__ALLOWED__/simple/")
json.dump(out, open("shared-done.json", "w"))
"""


# --------------------------------------------------------------------------
# Arguments and grading (pure; unit tested)
# --------------------------------------------------------------------------


def _checks(value: str) -> tuple[str, ...]:
    items = tuple(i.strip() for i in value.split(",") if i.strip())
    if not items or any(i not in CHECKS for i in items):
        raise argparse.ArgumentTypeError(f"expected a subset of {','.join(CHECKS)}")
    return items


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
    p.add_argument("--image-tag", default="project-sandbox-e2e-egress-sidecar:latest")
    p.add_argument("--checks", type=_checks, default=CHECKS)
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--no-build", action="store_true")
    p.add_argument("--keep", action="store_true", help="keep the test project and logs")
    p.add_argument(
        "--replace-existing",
        action="store_true",
        help="run even though a global egress sidecar exists (it is replaced and stopped)",
    )
    return p


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return parser().parse_args(argv)


@dataclass
class Result:
    name: str
    ok: bool
    detail: str = ""


def grade_probes(probes: dict) -> list[Result]:
    expected = (
        "allowed-splice",
        "unlisted-connect",
        "unlisted-http",
        "ip-literal-public",
        "ip-literal-metadata",
        "ip-literal-loopback6",
        "port",
        "metadata-name",
        "sni-mismatch",
        "no-sni",
        "raw-tunnel",
        "direct-egress",
        "dns",
        "github-blocked",
        "concurrency",
    )
    results = []
    for name in expected:
        entry = probes.get(name)
        if entry is None:
            results.append(Result(name, False, "probe did not run"))
        else:
            results.append(Result(name, bool(entry.get("ok")), entry.get("detail", "")))
    return results


def grade_log(text: str) -> list[Result]:
    counts = egress_log.denials(text)
    seen = {(d.target.split("/")[0], d.cause) for d in counts}
    results = []
    for host, cause in EXPECTED_LOG_CAUSES:
        results.append(
            Result(
                f"log:{cause}:{host}",
                (host, cause) in seen,
                "" if (host, cause) in seen else "not logged",
            )
        )
    leaked = [
        line
        for line in text.splitlines()
        if line.startswith("SBX ")
        and re.search(r"TCP_TUNNEL/200 CONNECT (example\.com|github\.com):", line)
    ]
    results.append(
        Result("log:no-tunnel-to-denied-hosts", not leaked, "; ".join(leaked[:2]))
    )
    return results


def grade_parent(probe: dict, parent_log: str) -> list[Result]:
    return [
        Result(
            "parent:allowed-through",
            probe.get("allowed") == 200,
            str(probe.get("allowed")),
        ),
        Result(
            "parent:unlisted-reset-locally",
            not str(probe.get("unlisted", "")).startswith("HTTP/1.1 200"),
            str(probe.get("unlisted")),
        ),
        Result(
            "parent:saw-allowed-host",
            bool(
                re.search(
                    rf"(CONNECT |GET https?://){re.escape(ALLOWED_HOST)}", parent_log
                )
            ),
            "",
        ),
        Result("parent:never-saw-unlisted-host", UNLISTED_HOST not in parent_log, ""),
    ]


def session_id_from(output: str) -> str | None:
    match = re.search(r"^Session: (\S+)$", output, re.MULTILINE)
    return match.group(1) if match else None


# --------------------------------------------------------------------------
# Runtime work
# --------------------------------------------------------------------------


def _write_policy(text: str) -> None:
    path = Path(os.environ["XDG_CONFIG_HOME"]) / "project-sandbox" / "egress.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _policy_text(*hosts: str, upstream: str = "", forward: tuple[int, ...] = ()) -> str:
    text = "[egress]\nallow = [" + ", ".join(json.dumps(h) for h in hosts) + "]\n"
    if upstream:
        text += upstream
    if forward:
        text += f"[egress.sidecar]\nforward_ports = {list(forward)}\n"
    return text


def egress(args, *command: str) -> subprocess.CompletedProcess:
    """Run project-sandbox-egress against the selected runtime."""
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "project_sandbox.egress_cli",
            *command,
            "--runtime",
            args.runtime,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )


def _launch_argv(args, project: Path, prompt: str, log: Path) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "project_sandbox",
        "--runtime",
        args.runtime,
        "--image-tag",
        args.image_tag,
        "--agent",
        "bash",
        "--no-forward-credentials",
        "--egress",
        "sidecar",
        "--prompt-text",
        prompt,
        "--log",
        str(log),
        "--timeout",
        str(args.timeout),
    ]
    if args.no_build:
        command.append("--no-build")
    return [*command, str(project), args.base_image]


def _launch(args, project: Path, prompt: str, log: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        _launch_argv(args, project, prompt, log),
        cwd=ROOT,
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )


def _project(work: Path, name: str, probe: str) -> Path:
    project = work / name
    project.mkdir()
    (project / "README.md").write_text(f"# egress {name} e2e\n")
    (project / "probe.py").write_text(probe)
    return project


def _sidecar_running(runtime: container_cli.Runtime) -> bool:
    info = egress_sidecar.inspect(runtime, egress_sidecar.default_runner)
    return info is not None and info.running


def _sidecar_ip(runtime: container_cli.Runtime) -> str | None:
    lay = egress_sidecar.layout(runtime)
    state = egress_sidecar.read_state(lay)
    return state["sidecar_ip"] if state else None


def _sidecar_count(runtime: container_cli.Runtime) -> int:
    argv = (
        [runtime.binary, "ls", "--all", "--format", "json"]
        if runtime is container_cli.APPLE_CONTAINER
        else [runtime.binary, "ps", "-a", "--format", "json"]
    )
    text = egress_sidecar.default_runner(argv).stdout or ""
    names = [
        egress_sidecar._record_name(runtime, r) or ""
        for r in egress_sidecar._json_records(text)
    ]
    return sum(n.lstrip("/").startswith(egress_sidecar.CONTAINER) for n in names)


def _session_dir(runtime: container_cli.Runtime, session_id: str) -> Path:
    return egress_sidecar.layout(runtime).sessions / session_id.lower()


def check_policy(args, runtime, work: Path) -> list[Result]:
    hosts = (ALLOWED_HOST, SECOND_ALLOWED_HOST, UNLISTED_HOST, METADATA_NAME)
    _write_policy(_policy_text(ALLOWED_HOST, SECOND_ALLOWED_HOST, METADATA_NAME))
    project = _project(work, "policy", PROBE_SOURCE.replace("__HOSTS__", repr(hosts)))
    run = _launch(args, project, "python3 probe.py", work / "policy.log")
    out = run.stdout + run.stderr
    (work / "policy.out").write_text(out)
    results = [Result("policy:launch", run.returncode == 0, out[-400:])]
    try:
        probes = json.loads((project / "egress-probe.json").read_text())
    except (OSError, ValueError):
        probes = {}
    results += grade_probes(probes)
    results += grade_log(egress(args, "log").stdout)
    count = re.search(r"the sidecar denied (\d+) requests? during this session", out)
    results.append(
        Result(
            "policy:session-denial-count",
            bool(count and int(count.group(1)) > 0)
            and "project-sandbox-egress log --denied --since " in out,
            count.group(0) if count else "no denial summary",
        )
    )
    results.append(
        Result("policy:sidecar-outlives-session", _sidecar_running(runtime), "")
    )
    session_id = session_id_from(run.stdout)
    results.append(
        Result(
            "policy:session-inputs-removed",
            bool(session_id) and not _session_dir(runtime, session_id).exists(),
            session_id or "no 'Session:' line in output",
        )
    )
    return results


def _wait_files(paths: list[Path], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(p.exists() for p in paths):
            return True
        time.sleep(1)
    return False


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def check_shared(args, runtime, work: Path) -> list[Result]:
    _write_policy(_policy_text(ALLOWED_HOST))
    probe = SHARED_PROBE_SOURCE.replace("__ALLOWED__", ALLOWED_HOST).replace(
        "__LIVE__", LIVE_HOST
    )
    projects = [_project(work, f"shared-{n}", probe) for n in ("a", "b")]
    procs = [
        subprocess.Popen(
            _launch_argv(
                args, project, "python3 probe.py", work / f"{project.name}.log"
            ),
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        for project in projects
    ]
    results = []
    try:
        ready = _wait_files([p / "shared-ready.json" for p in projects], args.timeout)
        results.append(Result("shared:both-started", ready, ""))
        status = json.loads(egress(args, "status", "--json").stdout or "{}")
        results.append(
            Result(
                "shared:one-sidecar",
                _sidecar_count(runtime) == 1,
                f"{_sidecar_count(runtime)} sidecar containers",
            )
        )
        results.append(
            Result(
                "shared:both-attached",
                len(status.get("attached", [])) >= 2,
                str(status.get("attached")),
            )
        )
        before = [_read_json(p / "shared-ready.json") for p in projects]
        results.append(
            Result(
                "shared:allowed-before",
                all(b.get("before") == 200 for b in before),
                str(before),
            )
        )
        results.append(
            Result(
                "shared:live-host-denied-before",
                all(b.get("live_before") != 200 for b in before),
                str(before),
            )
        )
        added = egress(args, "allow", "add", LIVE_HOST)
        results.append(Result("shared:allow-add", added.returncode == 0, added.stderr))
        for p in projects:
            (p / "go").write_text("")
        _wait_files([p / "shared-live.json" for p in projects], 120)
        live = [_read_json(p / "shared-live.json") for p in projects]
        results.append(
            Result(
                "shared:allow-add-reaches-both",
                all(isinstance(r.get("live_after"), int) for r in live),
                str(live),
            )
        )
        results.append(
            Result(
                "shared:no-restart",
                _sidecar_running(runtime)
                and status.get("started_at")
                == json.loads(egress(args, "status", "--json").stdout or "{}").get(
                    "started_at"
                ),
                "",
            )
        )
        down = egress(args, "down")
        results.append(
            Result(
                "shared:down-names-attached",
                down.returncode == 0 and "lose egress" in down.stdout,
                down.stdout[-200:],
            )
        )
        for p in projects:
            (p / "down").write_text("")
        _wait_files([p / "shared-done.json" for p in projects], 120)
        done = [_read_json(p / "shared-done.json") for p in projects]
        results.append(
            Result(
                "shared:down-cuts-both-off",
                all(d.get("after_down") not in (200, None) for d in done),
                str(done),
            )
        )
    finally:
        for proc in procs:
            try:
                out, _ = proc.communicate(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, _ = proc.communicate()
            (work / f"shared-{proc.pid}.out").write_text(out or "")
    outputs = [(work / f"shared-{proc.pid}.out").read_text() for proc in procs]
    results.append(
        Result(
            "shared:reuse-across-launches",
            sum("Egress sidecar: reusing" in o for o in outputs) >= 1,
            " | ".join(o[-200:] for o in outputs),
        )
    )
    up = egress(args, "up")  # for the later checks
    results.append(Result("shared:up-after-down", up.returncode == 0, up.stderr[-200:]))
    return results


def _copilot_fixture():
    path = ROOT / "scripts" / "e2e-copilot-egress.py"
    spec = importlib.util.spec_from_file_location("e2e_copilot_egress_fixture", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def check_parent(args, runtime, work: Path) -> list[Result]:
    fixture = _copilot_fixture()
    cli = fixture.fixture_cli(runtime)
    image = fixture.fixture_image_tag()
    fixture_dir = work / "parent-fixture"
    fixture_dir.mkdir(mode=0o700)
    if (
        subprocess.run(
            [cli, "image", "inspect", image], capture_output=True, check=False
        ).returncode
        != 0
    ):
        fixture.checked(
            [cli, "build", "-t", image, str(fixture.FIXTURE_DIR)], timeout=1200
        )
    fixture.checked(
        [cli, "run", "--rm", "-v", f"{fixture_dir}:/fixture", image, "make-ca"]
    )
    (fixture_dir / "squid.conf").write_text(fixture.render_squid_conf("capture"))
    probe = PARENT_PROBE_SOURCE.replace("__ALLOWED__", ALLOWED_HOST).replace(
        "__UNLISTED__", UNLISTED_HOST
    )
    first, second = _project(work, "parent-1", probe), _project(work, "parent-2", probe)
    relay = runtime is container_cli.APPLE_CONTAINER
    name = f"sbx-e2e-egress-parent-{int(time.time())}"
    results: list[Result] = []
    with fixture.running_fixture(cli, image, fixture_dir, name) as (port, logs):
        # A name for loopback that is not "localhost": the CLI treats it as a
        # remote parent, so Apple container goes through the host relay.
        host = "127.0.0.1.nip.io" if relay else "127.0.0.1"
        _write_policy(
            _policy_text(
                ALLOWED_HOST,
                upstream=(
                    "[egress.upstream]\n"
                    f'url = "http://{host}:{port}"\n'
                    f'ca_cert = ["{fixture_dir / "ca" / "ca.pem"}"]\n'
                ),
            )
        )
        restart = egress(args, "restart")
        results.append(
            Result("parent:restart", restart.returncode == 0, restart.stderr[-300:])
        )
        # Session 1 in its own process group; hang the group up afterwards,
        # as a closed terminal would.
        proc = subprocess.Popen(
            _launch_argv(args, first, "python3 probe.py", work / "parent-1.log"),
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            start_new_session=True,
        )
        out1, _ = proc.communicate(timeout=args.timeout)
        try:
            os.killpg(proc.pid, signal.SIGHUP)
        except ProcessLookupError:
            pass
        results.append(Result("parent:launch", proc.returncode == 0, out1[-400:]))
        if relay:
            state_dir = egress_sidecar.layout(runtime).root
            results.append(
                Result(
                    "parent:relay-survives-first-session",
                    egress_relay.RelayControl().is_live(state_dir),
                    egress_relay.RelayControl.log_tail(state_dir),
                )
            )
        run2 = _launch(args, second, "python3 probe.py", work / "parent-2.log")
        results.append(
            Result(
                "parent:second-session",
                run2.returncode == 0
                and _read_json(second / "egress-parent.json").get("allowed") == 200,
                (run2.stdout + run2.stderr)[-300:],
            )
        )
    parent_log = "".join(logs)
    (work / "parent-fixture.log").write_text(parent_log)
    results += grade_parent(_read_json(first / "egress-parent.json"), parent_log)
    # Back to direct mode for later checks.
    _write_policy(_policy_text(ALLOWED_HOST))
    egress(args, "restart")
    return results


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


def _wget(runtime, image: str, url: str, *, network: str | None) -> str:
    argv = [runtime.binary, "run", "--rm"]
    if network:
        argv += ["--network", network]
    argv += ["--entrypoint", "sh", image, "-c", f"wget -q -T 8 -O - {url}"]
    return subprocess.run(argv, capture_output=True, text=True, check=False).stdout


def check_forward(args, runtime, work: Path) -> list[Result]:
    """Forward a loopback host service through the sidecar, live."""
    served = work / "served"
    served.mkdir()
    (served / "ok.txt").write_text("forwarded-ok\n")

    def handler(*a, **kw):
        return _Quiet(*a, directory=str(served), **kw)

    image = egress_sidecar.read_state(egress_sidecar.layout(runtime)) or {}
    image = image.get("image", "")
    with socketserver.TCPServer(("127.0.0.1", 0), handler) as server:
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        added = egress(args, "forward", "add", str(port))
        ip = _sidecar_ip(runtime)
        inside = _wget(
            runtime, image, f"http://{ip}:{port}/ok.txt", network=egress_sidecar.NETWORK
        )
        inspect = egress_sidecar.default_runner(
            egress_sidecar.container_inspect_argv(runtime)
        )
        default_ip = default_network_ip(runtime, inspect.stdout, egress_sidecar.NETWORK)
        outside = _wget(
            runtime, image, f"http://{default_ip}:{port}/ok.txt", network=None
        )
        listed = egress(args, "forward", "list").stdout
        removed = egress(args, "forward", "remove", str(port))
        after = _wget(
            runtime, image, f"http://{ip}:{port}/ok.txt", network=egress_sidecar.NETWORK
        )
        server.shutdown()
    return [
        Result("forward:add", added.returncode == 0, added.stderr[-200:]),
        Result("forward:listed-running", f"Running: {port}" in listed, listed),
        Result(
            "forward:internal-peer-reaches-host-service",
            "forwarded-ok" in inside,
            inside[-200:],
        ),
        Result(
            "forward:default-network-peer-refused",
            bool(default_ip) and "forwarded-ok" not in outside,
            f"sidecar default-network ip={default_ip}",
        ),
        Result(
            "forward:removed-live",
            removed.returncode == 0 and "forwarded-ok" not in after,
            removed.stderr[-200:],
        ),
    ]


def default_network_ip(runtime, inspect_json: str, internal_network: str) -> str | None:
    """The sidecar's address on its default (egress) network."""
    try:
        record = json.loads(inspect_json)[0]
        if runtime is container_cli.APPLE_CONTAINER:
            networks = {
                n["network"]: n["ipv4Address"] for n in record["status"]["networks"]
            }
        else:
            networks = {
                k: v["IPAddress"]
                for k, v in record["NetworkSettings"]["Networks"].items()
            }
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    for name, address in networks.items():
        if name != internal_network and address:
            return address.split("/")[0]
    return None


def isolate_xdg(xdg: Path, environ=os.environ) -> None:
    """Point XDG_CONFIG_HOME/XDG_STATE_HOME at ``xdg``, keeping runtime config.

    Podman reads its machine connections and containers.conf from
    ``$XDG_CONFIG_HOME/containers``. Without that directory, `podman` on macOS
    falls back to a local socket that does not exist and every call fails, so
    the user's directory is linked in. Egress policy and sidecar state stay
    isolated.
    """
    real_config = Path(environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    config = xdg / "config"
    config.mkdir(parents=True, exist_ok=True)
    if (real_config / "containers").is_dir():
        (config / "containers").symlink_to(real_config / "containers")
    environ["XDG_CONFIG_HOME"] = str(config)
    environ["XDG_STATE_HOME"] = str(xdg / "state")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    runtime = container_cli.select_runtime(args.runtime)
    if runtime is container_cli.CHROOT:
        raise SystemExit("select apple-container, docker, or podman")
    args.runtime = runtime.name
    if (
        egress_sidecar.inspect(runtime, egress_sidecar.default_runner) is not None
        and not args.replace_existing
    ):
        raise SystemExit(
            f"a global egress sidecar exists on {runtime.name}; this suite would "
            "replace and stop it. Run `project-sandbox-egress down` first, or pass "
            "--replace-existing."
        )
    temp_root = ROOT / ".project-sandbox"
    temp_root.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="e2e-egress-sidecar.", dir=temp_root))
    # Never the user's egress.toml or sidecar state.
    xdg = Path(tempfile.mkdtemp(prefix="e2e-egress-xdg."))
    isolate_xdg(xdg)
    results: list[Result] = []
    try:
        down = egress(args, "down")  # a clean slate (and legacy cleanup)
        if down.returncode != 0:
            results.append(Result("setup:down", False, down.stderr[-200:]))
        for check in args.checks:
            print(f"RUN:  {check}", flush=True)
            try:
                results += {
                    "policy": check_policy,
                    "shared": check_shared,
                    "parent": check_parent,
                    "forward": check_forward,
                }[check](args, runtime, work)
            except Exception as exc:  # noqa: BLE001 - report and continue with the next check
                results.append(Result(f"{check}:error", False, repr(exc)))
    finally:
        egress(args, "down")
        if args.keep:
            print(f"Kept: {work} (state: {xdg})")
        else:
            shutil.rmtree(work, ignore_errors=True)
            shutil.rmtree(xdg, ignore_errors=True)
    failed = [r for r in results if not r.ok]
    for result in results:
        print(
            f"{'PASS' if result.ok else 'FAIL'}: {result.name}"
            + (f" ({result.detail})" if not result.ok and result.detail else "")
        )
    print(f"{len(results) - len(failed)}/{len(results)} egress-sidecar checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
