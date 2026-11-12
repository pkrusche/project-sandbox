#!/usr/bin/env python3
"""Opt-in end-to-end checks for the global --egress sidecar.

(egress-sidecar-macos task 9.1, design.md Decision 15; Linux runtimes:
egress-sidecar-linux task 5.1)

Runs real project-sandbox launches (agent: bash, no credentials, no billable
calls) against the runtime's global sidecar, managed with
project-sandbox-egress, and grades each check from both sides: what the client
inside the sandbox saw, and what the sidecar logged. The sandbox reaches the
sidecar's published endpoint (127.0.0.1:<port> on the host; on Docker Engine
the default bridge gateway, e.g. 172.17.0.1:<port>) as
http://host.docker.internal:<port>.

Checks (default: all):
  policy     one direct-mode sidecar session with in-sandbox probes:
               allowed-splice (and an HTTP request inside it), unlisted host
               (CONNECT and plain HTTP), IP literals (public, loopback,
               RFC 1918, link-local, private IPv6, metadata; plus a port-80
               sample), port, metadata by name (nip.io, sslip.io), SNI
               mismatch (foreign, unlisted, missing; with a control), raw
               tunnels (plaintext and SSH), direct-egress (public addresses
               and host ports that are not local services), dns,
               github-blocked, concurrency; plus the session-end denial count,
               session-input cleanup, the sidecar outliving the session and
               its endpoint answering on its one published address only
  intercept  a session with the Copilot profile on, so api.github.com is
             intercepted: the session CA is trusted, an allowed path reaches
             GitHub, a denied path and plain HTTP get labelled 403 pages, and
             a foreign or missing SNI and raw tunnels (plaintext, SSH) fail
  shared     two concurrent sandboxes on one sidecar, reuse across launches, a
             live `allow add` reaching both, and `down` cutting both off
  parent     sessions behind an intercepting fixture parent proxy with its own
             CA ([egress.upstream]): allowed traffic reaches the parent, denied
             traffic never does. On Apple container the parent is reached
             through the sidecar-owned host relay, which must survive the first
             session's process group being hung up (a closed terminal). On
             Docker Engine and rootless Podman the parent listens only on host
             loopback: it must not answer on any other host address, the
             sandbox must be refused on the parent's port (and on Docker Engine
             on the parent forwarder's), and on Docker Engine another container
             on the default bridge must be closed by the forwarder's peer
             check without the parent seeing it.
  local      a loopback-only host service (a fake agent gateway on
             127.0.0.1) reached through local-service forwarding, not through
             the sidecar

All state lives in a temporary XDG_CONFIG_HOME/XDG_STATE_HOME, never the
user's egress.toml. Only the user's `containers/` config directory is linked
into the temporary config home, so `podman` still finds its machine
connection. The global sidecar's name is fixed per runtime, so the
suite refuses to start while one exists (it would be replaced and stopped)
unless --replace-existing is given.

CONNECT denials pass only on zero bytes followed by a close or reset (design.md
Decision 15); a reply, a timeout, or not reaching the sidecar fails.
Plain-HTTP and intercepted denials pass only on a 403 carrying the expected
`project-sandbox-egress:` label.

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
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from project_sandbox import (
    container_cli,
    egress_log,
    egress_policy,
    egress_relay,
    egress_sidecar,
)

ROOT = Path(__file__).resolve().parents[1]
CHECKS = ("policy", "intercept", "shared", "parent", "local")
ALLOWED_HOST = "pypi.org"
SECOND_ALLOWED_HOST = "files.pythonhosted.org"
UNLISTED_HOST = "example.com"
# Allowed live (`allow add`) while the shared sandboxes run.
LIVE_HOST = "example.org"
# Allowlisted names that resolve to the metadata address (direct mode only).
METADATA_NAMES = ("169.254.169.254.nip.io", "169.254.169.254.sslip.io")
# IP-literal CONNECT targets on port 443, so the port rule cannot mask the
# ip-literal rule (Decision 15): probe name -> address.
IP_LITERALS = (
    ("ip-literal-public", "1.1.1.1"),
    ("ip-literal-loopback", "127.0.0.1"),
    ("ip-literal-loopback6", "::1"),
    ("ip-literal-rfc1918", "10.0.0.1"),
    ("ip-literal-link-local", "169.254.1.1"),
    ("ip-literal-metadata", "169.254.169.254"),
    ("ip-literal-private6", "fd00::1"),
)
# The port-80 sample: a plain-HTTP request to an IP literal gets the labelled page.
PORT80_LITERAL = "192.168.0.1"
# Intercepted only with the Copilot profile on (egress_policy.derive_bump_set).
INTERCEPTED_HOST = egress_policy.COPILOT_API_HOST
INTERCEPTED_ALLOWED_PATH = "/copilot_internal/user"
INTERCEPTED_DENIED_PATH = "/repos/octocat/Hello-World"
SSH_BANNER = "SSH-2.0-OpenSSH_9.6\r\n"


def _connect_target(host: str, port: int = 443) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


# Shared by the in-sandbox probes; stdlib only. Expects the globals PROXY
# (sidecar endpoint) and results (name -> {"ok", "detail"}).
PROBE_HELPERS = r'''
import socket, ssl, urllib.error, urllib.request


def record(name, ok, detail):
    results[name] = {"ok": bool(ok), "detail": str(detail)[:300]}


def _opener():
    proxy = f"http://{PROXY[0]}:{PROXY[1]}"
    return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))


def connect_reply(target, timeout=8):
    """Send CONNECT; return (bytes received, how the read ended).

    The end is "closed" (EOF), "reset" (RST) or "timeout"; a failure to reach
    the sidecar at all raises OSError.
    """
    with socket.create_connection(PROXY, timeout=timeout) as s:
        s.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
        data = b""
        try:
            while b"\r\n\r\n" not in data:
                chunk = s.recv(4096)
                if not chunk:
                    return data, "closed"
                data += chunk
        except ConnectionResetError:
            return data, "reset"
        except socket.timeout:
            return data, "timeout"
        return data, "reply"


def denied_connect(name, target):
    """Decision 15: a CONNECT denial is zero bytes, then a close or reset.

    A reply of any kind (a 200 or an error page), a timeout, or not reaching
    the sidecar fails: none of them shows the sidecar refusing the tunnel.
    """
    try:
        data, end = connect_reply(target)
    except OSError as exc:
        record(name, False, f"could not reach the sidecar: {type(exc).__name__}")
        return
    record(name, data == b"" and end in ("closed", "reset"), f"{end} after {data[:40]!r}")


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


def tls_via_tunnel(target, sni, verify=True, request=None):
    """Open a TLS session through a tunnel; with ``request``, send it and
    append the reply's status line to the outcome."""
    try:
        s, head = tunnel(target)
    except OSError as exc:
        return f"no tunnel: {type(exc).__name__}"
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
            outcome = f"handshake ok ({t.version()})"
            if request is not None:
                t.sendall(request)
                status = t.recv(64).split(b"\r\n", 1)[0]
                outcome += f"; reply {status!r}"
            return outcome
    except (ssl.SSLError, OSError) as exc:
        return f"handshake failed: {type(exc).__name__}"


def raw_tunnel(name, target, payload):
    """Non-TLS bytes through an established tunnel must get nothing back."""
    try:
        s, head = tunnel(target)
        s.sendall(payload)
        s.settimeout(6)
        try:
            reply = s.recv(4096)
        except (ConnectionResetError, socket.timeout):
            reply = b""
        s.close()
        record(name, head.startswith(b"HTTP/1.1 200") and reply == b"", f"head={head[:20]!r} reply={reply[:40]!r}")
    except OSError as exc:
        record(name, False, repr(exc))


def labelled_403(name, url, label):
    """Decision 15: a 403 whose body carries the label; any other answer fails."""
    try:
        _opener().open(url, timeout=15)
        record(name, False, "reached the origin")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        record(name, exc.code == 403 and f"project-sandbox-egress: {label}" in body, f"{exc.code} {body[:80]}")
    except Exception as exc:
        record(name, False, repr(exc))
'''

_PROBE_HEADER = r"""#!/usr/bin/env python3
import concurrent.futures, json, os, subprocess

# The sidecar endpoint, as the launch configured it (http://host:port).
_proxy = os.environ["HTTPS_PROXY"].removeprefix("http://").rstrip("/")
PROXY = (_proxy.rsplit(":", 1)[0], int(_proxy.rsplit(":", 1)[1]))
CFG = __CONFIG__
results = {}
"""

# Direct-mode policy session. Writes egress-probe.json.
PROBE_SOURCE = (
    _PROBE_HEADER
    + PROBE_HELPERS
    + r"""
ALLOWED, SECOND, UNLISTED = CFG["allowed"], CFG["second"], CFG["unlisted"]

# allowed splice: the default trust store verifies the origin (nothing bumped)
try:
    with urllib.request.urlopen(f"https://{ALLOWED}/simple/", timeout=20) as r:
        record("allowed-splice", r.status == 200, r.status)
except Exception as exc:
    record("allowed-splice", False, repr(exc))
inner = tls_via_tunnel(f"{ALLOWED}:443", ALLOWED,
                       request=f"GET /simple/ HTTP/1.1\r\nHost: {ALLOWED}\r\nConnection: close\r\n\r\n".encode())
record("allowed-splice-http", "reply b'HTTP/1.1 200" in inner, inner)

denied_connect("unlisted-connect", f"{UNLISTED}:443")
labelled_403("unlisted-http", f"http://{UNLISTED}/", "host-not-allowed")

for name, target in CFG["ip_literals"]:
    denied_connect(name, target)
labelled_403("ip-literal-port80", f"http://{CFG['port80_literal']}/", "ip-literal")
denied_connect("port", f"{ALLOWED}:8443")

# metadata by name: allowlisted but resolves to 169.254.169.254 -> no successful TLS
for metadata in CFG["metadata"]:
    outcome = tls_via_tunnel(f"{metadata}:443", metadata)
    record("metadata-name-" + metadata.removeprefix("169.254.169.254."), not outcome.startswith("handshake ok"), outcome)

ok = tls_via_tunnel(f"{ALLOWED}:443", ALLOWED)
mismatch = tls_via_tunnel(f"{ALLOWED}:443", SECOND, verify=False)
record("sni-mismatch", ok.startswith("handshake ok") and not mismatch.startswith("handshake ok"), f"control={ok}; mismatch={mismatch}")
unlisted_sni = tls_via_tunnel(f"{ALLOWED}:443", UNLISTED, verify=False)
record("sni-unlisted", ok.startswith("handshake ok") and not unlisted_sni.startswith("handshake ok"), f"control={ok}; unlisted={unlisted_sni}")
nosni = tls_via_tunnel(f"{ALLOWED}:443", None, verify=False)
record("no-sni", not nosni.startswith("handshake ok"), nosni)

raw_tunnel("raw-tunnel", f"{ALLOWED}:443", f"GET / HTTP/1.1\r\nHost: {ALLOWED}\r\n\r\n".encode())
raw_tunnel("raw-tunnel-ssh", f"{ALLOWED}:443", CFG["ssh_banner"].encode())

direct = []
for host, port in (("1.1.1.1", 443), ("2606:4700:4700::1111", 443)):
    try:
        socket.create_connection((host, port), timeout=4).close()
        direct.append(f"{host} OPEN")
    except OSError:
        pass
# Host ports that are neither the sidecar's nor this session's local services.
for port in (22, 11434, PROXY[1] + 1):
    try:
        socket.create_connection((PROXY[0], port), timeout=4).close()
        direct.append(f"host:{port} OPEN")
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

def concurrent_head(_i):
    try:
        return connect_reply(f"{ALLOWED}:443")[0]
    except OSError:
        return b""


with concurrent.futures.ThreadPoolExecutor(10) as pool:
    heads = list(pool.map(concurrent_head, range(10)))
record("concurrency", all(h.startswith(b"HTTP/1.1 200") for h in heads), sum(h.startswith(b"HTTP/1.1 200") for h in heads))

with open("egress-probe.json", "w", encoding="utf-8") as fh:
    json.dump(results, fh, indent=2)
print("EGRESS_PROBE_DONE")
"""
)

# Every probe the policy session must report, in report order.
PROBE_NAMES = (
    "allowed-splice",
    "allowed-splice-http",
    "unlisted-connect",
    "unlisted-http",
    *(name for name, _ in IP_LITERALS),
    "ip-literal-port80",
    "port",
    *("metadata-name-" + m.removeprefix("169.254.169.254.") for m in METADATA_NAMES),
    "sni-mismatch",
    "sni-unlisted",
    "no-sni",
    "raw-tunnel",
    "raw-tunnel-ssh",
    "direct-egress",
    "dns",
    "github-blocked",
    "concurrency",
)


def probe_source() -> str:
    """The policy session's probe with its configuration filled in."""
    config = {
        "allowed": ALLOWED_HOST,
        "second": SECOND_ALLOWED_HOST,
        "unlisted": UNLISTED_HOST,
        "metadata": list(METADATA_NAMES),
        "ip_literals": [(name, _connect_target(ip)) for name, ip in IP_LITERALS],
        "port80_literal": PORT80_LITERAL,
        "ssh_banner": SSH_BANNER,
    }
    return PROBE_SOURCE.replace("__CONFIG__", repr(config))


# Causes the sidecar must have logged for the policy probes (host -> cause).
EXPECTED_LOG_CAUSES = (
    (UNLISTED_HOST, "host-not-allowed"),
    *((ip, "ip-literal") for _, ip in IP_LITERALS),
    (PORT80_LITERAL, "ip-literal"),
    (ALLOWED_HOST, "port-not-allowed"),
    *((m, "private-address") for m in METADATA_NAMES),
    (ALLOWED_HOST, "sni-mismatch"),
    (ALLOWED_HOST, "non-tls-tunnel"),
    ("github.com", "host-not-allowed"),
)
# SNIs the sidecar must have terminated on the allowed host ("-" = no SNI).
EXPECTED_TERMINATIONS = (SECOND_ALLOWED_HOST, UNLISTED_HOST, "-")
# CONNECT targets that must never show an established tunnel in the log.
DENIED_TUNNEL_TARGETS = (
    _connect_target(UNLISTED_HOST),
    _connect_target("github.com"),
    *(_connect_target(ip) for _, ip in IP_LITERALS),
    *(_connect_target(m) for m in METADATA_NAMES),
)

# Intercepted-host session (Copilot profile on). Writes egress-intercept.json.
INTERCEPT_PROBE_SOURCE = (
    _PROBE_HEADER
    + PROBE_HELPERS
    + r"""
HOST, FOREIGN = CFG["host"], CFG["foreign_sni"]

# Matching control: the sidecar's certificate, trusted through the session CA.
control = tls_via_tunnel(f"{HOST}:443", HOST)
record("intercept:control", control.startswith("handshake ok"), control)

# An allowed path is bumped and re-originated to GitHub.
try:
    with _opener().open(f"https://{HOST}{CFG['allowed_path']}", timeout=20) as r:
        code, server = r.status, r.headers.get("Server", "")
except urllib.error.HTTPError as exc:
    code, server = exc.code, exc.headers.get("Server", "")
except Exception as exc:
    code, server = repr(exc), ""
record("intercept:allowed-path-reaches-origin", server == "github.com", f"{code} server={server}")

labelled_403("intercept:path-denied", f"https://{HOST}{CFG['denied_path']}", "path-not-allowed")
labelled_403("intercept:plain-http", f"http://{HOST}/", "plain-http-intercepted")

foreign = tls_via_tunnel(f"{HOST}:443", FOREIGN, verify=False)
record("intercept:sni-foreign", control.startswith("handshake ok") and not foreign.startswith("handshake ok"), f"control={control}; foreign={foreign}")
nosni = tls_via_tunnel(f"{HOST}:443", None, verify=False)
record("intercept:no-sni", not nosni.startswith("handshake ok"), nosni)

raw_tunnel("intercept:raw-tunnel", f"{HOST}:443", f"GET / HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
raw_tunnel("intercept:raw-tunnel-ssh", f"{HOST}:443", CFG["ssh_banner"].encode())

with open("egress-intercept.json", "w", encoding="utf-8") as fh:
    json.dump(results, fh, indent=2)
print("EGRESS_PROBE_DONE")
"""
)

INTERCEPT_PROBE_NAMES = (
    "intercept:control",
    "intercept:allowed-path-reaches-origin",
    "intercept:path-denied",
    "intercept:plain-http",
    "intercept:sni-foreign",
    "intercept:no-sni",
    "intercept:raw-tunnel",
    "intercept:raw-tunnel-ssh",
)


def intercept_probe_source() -> str:
    config = {
        "host": INTERCEPTED_HOST,
        "foreign_sni": ALLOWED_HOST,
        "allowed_path": INTERCEPTED_ALLOWED_PATH,
        "denied_path": INTERCEPTED_DENIED_PATH,
        "ssh_banner": SSH_BANNER,
    }
    return INTERCEPT_PROBE_SOURCE.replace("__CONFIG__", repr(config))


INTERCEPT_LOG_CAUSES = (
    (INTERCEPTED_HOST, "path-not-allowed"),
    (INTERCEPTED_HOST, "plain-http-intercepted"),
    (INTERCEPTED_HOST, "sni-mismatch"),
    (INTERCEPTED_HOST, "non-tls-tunnel"),
)

# Parent-chain probes: allowed through, unlisted reset locally.
PARENT_PROBE_SOURCE = r"""#!/usr/bin/env python3
import json, os, socket, urllib.request
_proxy = os.environ["HTTPS_PROXY"].removeprefix("http://").rstrip("/")
PROXY = (_proxy.rsplit(":", 1)[0], int(_proxy.rsplit(":", 1)[1]))
out = {}
try:
    with urllib.request.urlopen("https://__ALLOWED__/simple/", timeout=30) as r:
        out["allowed"] = r.status
except Exception as exc:
    out["allowed"] = repr(exc)
with socket.create_connection(PROXY, timeout=8) as s:
    s.sendall(b"CONNECT __UNLISTED__:443 HTTP/1.1\r\nHost: __UNLISTED__:443\r\n\r\n")
    try:
        out["unlisted"] = s.recv(64).decode(errors="replace")
    except OSError as exc:
        out["unlisted"] = type(exc).__name__
# Linux runs: the parent (and the Docker Engine parent forwarder) must be out of
# the sandbox's reach; any successful connect fails the check.
out["refused"] = {}
for host, port in __REFUSE__:
    try:
        socket.create_connection((host, port), timeout=5).close()
        out["refused"][f"{host}:{port}"] = "connected"
    except OSError as exc:
        out["refused"][f"{host}:{port}"] = type(exc).__name__
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

# Local-service probe: the fake gateway is on host loopback only; the bash
# session reaches it at $OPENAI_BASE_URL (host.docker.internal, NO_PROXY).
LOCAL_PROBE_SOURCE = r"""#!/usr/bin/env python3
import json, os, urllib.request
out = {"no_proxy": os.environ.get("NO_PROXY", "")}
try:
    with urllib.request.urlopen(os.environ["OPENAI_BASE_URL"].rstrip("/") + "/ok", timeout=15) as r:
        out["body"] = r.read().decode(errors="replace")
except Exception as exc:
    out["body"] = repr(exc)
json.dump(out, open("egress-local.json", "w"))
"""
LOCAL_SERVICE_BODY = "local-service-ok"
LOCAL_MODEL = "e2e-egress-model"


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
    p.add_argument(
        "--port",
        type=int,
        default=3129,
        help="[egress.sidecar] port for the suite's sidecar (default 3129)",
    )
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


def grade_probes(probes: dict, names: Sequence[str] = PROBE_NAMES) -> list[Result]:
    results = []
    for name in names:
        entry = probes.get(name)
        if entry is None:
            results.append(Result(name, False, "probe did not run"))
        else:
            results.append(Result(name, bool(entry.get("ok")), entry.get("detail", "")))
    return results


def grade_causes(
    text: str, expected: Sequence[tuple[str, str]], prefix: str = "log"
) -> list[Result]:
    """One result per expected (host, cause) denial in the sidecar log."""
    seen = {(d.target.split("/")[0], d.cause) for d in egress_log.denials(text)}
    return [
        Result(
            f"{prefix}:{cause}:{host}",
            (host, cause) in seen,
            "" if (host, cause) in seen else "not logged",
        )
        for host, cause in expected
    ]


def _sbx_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("SBX ")]


def grade_log(text: str) -> list[Result]:
    results = grade_causes(text, EXPECTED_LOG_CAUSES)
    lines = _sbx_lines(text)
    allowed = re.escape(ALLOWED_HOST)
    for sni in EXPECTED_TERMINATIONS:
        pattern = rf" CONNECT {allowed}:443 sni={re.escape(sni)} bump=terminate "
        found = any(re.search(pattern, line) for line in lines)
        results.append(
            Result(
                f"log:terminated:{ALLOWED_HOST}:sni={sni}",
                found,
                "" if found else "not logged",
            )
        )
    splice = rf"TCP_TUNNEL/200 CONNECT {allowed}:443 sni={allowed} bump=splice "
    results.append(
        Result(
            f"log:splice:{ALLOWED_HOST}",
            any(re.search(splice, line) for line in lines),
            "",
        )
    )
    leaked = [
        line
        for line in lines
        if any(f"TCP_TUNNEL/200 CONNECT {t} " in line for t in DENIED_TUNNEL_TARGETS)
    ]
    results.append(
        Result("log:no-tunnel-to-denied-hosts", not leaked, "; ".join(leaked[:2]))
    )
    return results


def grade_intercept_log(text: str) -> list[Result]:
    results = grade_causes(text, INTERCEPT_LOG_CAUSES, prefix="intercept:log")
    host = re.escape(INTERCEPTED_HOST)
    path = re.escape(INTERCEPTED_ALLOWED_PATH)
    bumped = [
        line
        for line in _sbx_lines(text)
        if re.search(rf" GET https://{host}{path} ", line) and "TCP_DENIED" not in line
    ]
    results.append(
        Result(
            f"intercept:log:bumped:{INTERCEPTED_HOST}{INTERCEPTED_ALLOWED_PATH}",
            bool(bumped),
            "" if bumped else "no allowed bumped request logged",
        )
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
            # Decision 15: zero bytes, then a close ("") or a reset.
            "parent:unlisted-reset-locally",
            probe.get("unlisted") in ("", "ConnectionResetError"),
            repr(probe.get("unlisted")),
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


def grade_parent_refusals(probe: dict) -> list[Result]:
    """Linux: the sandbox reaches neither the loopback parent nor the forwarder."""
    refused = probe.get("refused")
    if not isinstance(refused, dict) or not refused:
        return [Result("parent:sandbox-refused", False, "refusal probe did not run")]
    return [
        Result(f"parent:sandbox-refused:{target}", outcome != "connected", outcome)
        for target, outcome in refused.items()
    ]


FORWARDER_PEER_PROBE = r"""
import socket, sys
try:
    s = socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=5)
except OSError as exc:
    print("connect-failed", type(exc).__name__); raise SystemExit
s.settimeout(5)
s.sendall(b"GET http://forwarder-peer-check.invalid/ HTTP/1.0\r\n\r\n")
try:
    data = s.recv(64)
    print("closed" if data == b"" else "data:" + data.decode(errors="replace"))
except ConnectionResetError:
    print("closed")
except OSError as exc:
    print("error", type(exc).__name__)
"""


def _check_forwarder(args, runtime, strategy, parent_port, *, logs_cli) -> list[Result]:
    """Docker Engine: the parent forwarder is live and admits only the sidecar."""
    state_dir = egress_sidecar.layout(runtime).root
    control = egress_relay.RelayControl()
    forwarder = control.port(state_dir)
    results = [
        Result(
            "parent:forwarder-live",
            forwarder is not None,
            control.log_tail(state_dir),
        )
    ]
    if forwarder is None:
        return results
    status = json.loads(egress(args, "status", "--json").stdout or "{}")
    results.append(
        Result(
            "parent:forwarder-in-status",
            (status.get("relay") or {}).get("mode") == "forward"
            and (status.get("relay") or {}).get("port") == forwarder,
            str(status.get("relay")),
        )
    )
    # Another container on the default bridge: closed without an upstream connect.
    run = subprocess.run(
        [
            runtime.binary,
            "run",
            "--rm",
            args.base_image,
            "python3",
            "-c",
            FORWARDER_PEER_PROBE,
            strategy.address,
            str(forwarder),
        ],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
        timeout=120,
    )
    outcome = run.stdout.strip() or run.stderr.strip()[-200:]
    log = (state_dir / egress_relay.LOG_NAME).read_text(errors="replace")
    results.append(
        Result(
            "parent:forwarder-closes-other-peers",
            outcome == "closed" and "refused peer" in log,
            outcome,
        )
    )
    cli, name = logs_cli
    fixture_log = subprocess.run(
        [cli, "logs", name],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    results.append(
        Result(
            "parent:never-saw-other-peer",
            "forwarder-peer-check.invalid"
            not in fixture_log.stdout + fixture_log.stderr,
            "",
        )
    )
    return results


def grade_local(probe: dict, sidecar_log: str, port: int) -> list[Result]:
    body = str(probe.get("body", ""))
    return [
        Result(
            "local:loopback-service-reached",
            LOCAL_SERVICE_BODY in body,
            body[:200],
        ),
        Result(
            "local:bypasses-the-sidecar",
            "host.docker.internal" in probe.get("no_proxy", "").split(",")
            and f"host.docker.internal:{port}" not in sidecar_log,
            probe.get("no_proxy", ""),
        ),
    ]


def session_id_from(output: str) -> str | None:
    match = re.search(r"^Session: (\S+)$", output, re.MULTILINE)
    return match.group(1) if match else None


# --------------------------------------------------------------------------
# Runtime work
# --------------------------------------------------------------------------


# Set by main(): the [egress.sidecar] port every policy of this run uses.
PORT = 3129
# The sidecar's published address on the selected runtime (main()).
PUBLISH = "127.0.0.1"


def _write_policy(text: str) -> None:
    path = Path(os.environ["XDG_CONFIG_HOME"]) / "project-sandbox" / "egress.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _policy_text(
    *hosts: str, upstream: str = "", port: int | None = None, copilot: bool = False
) -> str:
    text = "[egress]\nallow = [" + ", ".join(json.dumps(h) for h in hosts) + "]\n"
    if upstream:
        text += upstream
    if copilot:
        text += "[egress.copilot]\nenabled = true\n"
    text += f"[egress.sidecar]\nport = {PORT if port is None else port}\n"
    return text


def egress(args, *command: str) -> subprocess.CompletedProcess:
    """Run project-sandbox-egress against the selected runtime.

    Bounded by ``--timeout`` (``up``/``restart`` may build the sidecar image):
    a wedged runtime call becomes a failed result instead of a stuck suite.
    """
    argv = [
        sys.executable,
        "-m",
        "project_sandbox.egress_cli",
        *command,
        "--runtime",
        args.runtime,
    ]
    try:
        return subprocess.run(
            argv,
            cwd=ROOT,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            timeout=args.timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            argv,
            124,
            stdout="",
            stderr=f"project-sandbox-egress {' '.join(command)} timed out after "
            f"{args.timeout}s",
        )


def _launch_argv(args, project: Path, prompt: str, log: Path, *extra: str) -> list[str]:
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
    return [*command, *extra, str(project), args.base_image]


def _launch(
    args, project: Path, prompt: str, log: Path, *extra: str, env=None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        _launch_argv(args, project, prompt, log, *extra),
        cwd=ROOT,
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
        env=env,
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


def container_names(runtime: container_cli.Runtime, text: str) -> list[str]:
    """Container names from ``ls --all``/``ps -a --format json`` output."""
    text = text.strip()
    try:
        records = json.loads(text) if text else []
    except ValueError:  # Docker: one JSON object per line
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(records, dict):
        records = [records]
    names = []
    for record in records:
        if runtime is container_cli.APPLE_CONTAINER:
            config = record.get("configuration") or {}
            name = record.get("id") or config.get("id")
        else:
            name = record.get("Names", record.get("Name"))
            name = name[0] if isinstance(name, list) and name else name
        if isinstance(name, str):
            names.append(name.lstrip("/"))
    return names


def _sidecar_count(runtime: container_cli.Runtime) -> int:
    argv = (
        [runtime.binary, "ls", "--all", "--format", "json"]
        if runtime is container_cli.APPLE_CONTAINER
        else [runtime.binary, "ps", "-a", "--format", "json"]
    )
    text = egress_sidecar.default_runner(argv).stdout or ""
    return sum(
        name.startswith(egress_sidecar.CONTAINER)
        for name in container_names(runtime, text)
    )


_INET_RE = re.compile(r"\binet (\d{1,3}(?:\.\d{1,3}){3})\b")


def host_ipv4_addresses(interfaces_text: str) -> list[str]:
    """Non-loopback IPv4 addresses from ``ifconfig`` or ``ip -o -4 addr`` output."""
    addresses: list[str] = []
    for address in _INET_RE.findall(interfaces_text):
        if not address.startswith("127.") and address not in addresses:
            addresses.append(address)
    return addresses


def _interfaces_text() -> str:
    """Interface listing from ``ifconfig`` (macOS) or ``ip`` (Linux); "" if neither."""
    for argv in (["ifconfig"], ["ip", "-o", "-4", "addr", "show"]):
        try:
            run = subprocess.run(
                argv, capture_output=True, text=True, check=False, timeout=10
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if run.returncode == 0 and run.stdout:
            return run.stdout
    return ""


def _answers(host: str, port: int) -> bool:
    import socket

    try:
        socket.create_connection((host, port), timeout=3).close()
    except OSError:
        return False
    return True


def _loopback_only(
    port: int,
    *,
    interfaces_text=_interfaces_text,
    answers=_answers,
    address: str = "127.0.0.1",
    name: str | None = None,
) -> Result:
    """The published port answers on ``address`` and on no other host address.

    ``address`` is 127.0.0.1, or Docker Engine's bridge gateway, in which case
    127.0.0.1 is one of the addresses that must not answer.

    The other addresses come from the interface list (LAN, VM bridges, VPNs
    such as Tailscale), not from resolving the hostname. Regression: the
    hostname often does not resolve (e.g. ``*.home`` on another network), and
    the lookup's exception threw away every result of the policy check.
    """
    name = name or (
        "policy:endpoint-loopback-only"
        if address == "127.0.0.1"
        else "policy:endpoint-gateway-only"
    )
    try:
        others = [a for a in host_ipv4_addresses(interfaces_text()) if a != address]
    except Exception as exc:  # noqa: BLE001 - report, never abort the check
        return Result(name, False, f"could not list host addresses: {exc!r}")
    if address != "127.0.0.1":
        others.insert(0, "127.0.0.1")
    if not others:
        return Result(name, False, "found no non-loopback host address to check")
    open_elsewhere = [a for a in others if answers(a, port)]
    return Result(
        name,
        answers(address, port) and not open_elsewhere,
        f"also open on {open_elsewhere}" if open_elsewhere else f"checked {others}",
    )


def _session_dir(runtime: container_cli.Runtime, session_id: str) -> Path:
    return egress_sidecar.layout(runtime).sessions / session_id.lower()


def check_policy(args, runtime, work: Path) -> list[Result]:
    _write_policy(_policy_text(ALLOWED_HOST, SECOND_ALLOWED_HOST, *METADATA_NAMES))
    project = _project(work, "policy", probe_source())
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
    results.append(_loopback_only(PORT, address=PUBLISH))
    results.append(
        Result(
            "policy:proxy-is-the-sidecar-endpoint",
            f"Egress sidecar: started {egress_sidecar.CONTAINER} at {PUBLISH}:{PORT}"
            in out
            or f"Egress sidecar: reusing {egress_sidecar.CONTAINER} at {PUBLISH}:{PORT}"
            in out,
            "",
        )
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


def check_intercept(args, runtime, work: Path) -> list[Result]:
    """Probes against the one intercepted host, which needs the Copilot profile.

    The profile changes the bump set and the CA, which only a restart applies;
    the check restores the direct policy (and restarts) afterwards.
    """
    _write_policy(_policy_text(ALLOWED_HOST, copilot=True))
    results: list[Result] = []
    try:
        restart = egress(args, "restart")
        results.append(
            Result("intercept:restart", restart.returncode == 0, restart.stderr[-300:])
        )
        project = _project(work, "intercept", intercept_probe_source())
        run = _launch(args, project, "python3 probe.py", work / "intercept.log")
        out = run.stdout + run.stderr
        (work / "intercept.out").write_text(out)
        results.append(Result("intercept:launch", run.returncode == 0, out[-400:]))
        results += grade_probes(
            _read_json(project / "egress-intercept.json"), INTERCEPT_PROBE_NAMES
        )
        results += grade_intercept_log(egress(args, "log").stdout)
    finally:
        _write_policy(_policy_text(ALLOWED_HOST))
        egress(args, "restart")
    return results


def _wait_files(
    paths: list[Path],
    timeout: float,
    procs: Sequence[subprocess.Popen] = (),
    *,
    interval: float = 1.0,
) -> bool:
    """Wait until every path exists.

    Gives up early once any of ``procs`` (the launches expected to write the
    files) has exited with a file still missing: it can no longer appear, and
    waiting out ``timeout`` only made a failed launch look like a hang.
    """
    deadline = time.monotonic() + timeout
    while True:
        if all(p.exists() for p in paths):
            return True
        if any(proc.poll() is not None for proc in procs):
            # One last look: the file may have landed just before the exit.
            return all(p.exists() for p in paths)
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


# How long a shared launch gets to tear down after its partner failed.
SHARED_STOP_GRACE = 30


def _launch_failure(log: Path, output: str, limit: int = 300) -> str:
    """The tail of a failed launch's run log, else of its CLI output.

    The CLI's own output ends with "Wrote N lines to <log>"; the runtime's
    error (e.g. docker's "No such image") is in that log.
    """
    try:
        text = log.read_text(errors="replace").strip()
    except OSError:
        text = ""
    return (text or output.strip())[-limit:]


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
    ready = False
    try:
        ready = _wait_files(
            [p / "shared-ready.json" for p in projects], args.timeout, procs
        )
        results.append(Result("shared:both-started", ready, ""))
        if ready:
            results += _shared_while_running(args, runtime, projects, procs)
    finally:
        for proc in procs:
            if not ready and proc.poll() is None:
                # The surviving probe waits for flag files only the shared
                # steps write; waiting out --timeout just hid the real failure.
                proc.terminate()
            try:
                out, _ = proc.communicate(
                    timeout=args.timeout if ready else SHARED_STOP_GRACE
                )
            except subprocess.TimeoutExpired:
                proc.kill()
                out, _ = proc.communicate()
            (work / f"shared-{proc.pid}.out").write_text(out or "")
    outputs = [(work / f"shared-{proc.pid}.out").read_text() for proc in procs]
    if not ready:
        results[0].detail = " | ".join(
            f"{project.name} exit {proc.returncode}: "
            + _launch_failure(work / f"{project.name}.log", out)
            for project, proc, out in zip(projects, procs, outputs)
        )
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


def _shared_while_running(
    args, runtime, projects: list[Path], procs: list[subprocess.Popen]
) -> list[Result]:
    """The shared steps that need both sandboxes up and stepping on flag files."""
    results = []
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
            "shared:endpoint",
            status.get("endpoint") == f"{PUBLISH}:{PORT}",
            str(status.get("endpoint")),
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
    _wait_files([p / "shared-live.json" for p in projects], 120, procs)
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
            "shared:down",
            down.returncode == 0 and "have lost egress" in down.stdout,
            down.stdout[-200:],
        )
    )
    for p in projects:
        (p / "down").write_text("")
    _wait_files([p / "shared-done.json" for p in projects], 120, procs)
    done = [_read_json(p / "shared-done.json") for p in projects]
    results.append(
        Result(
            "shared:down-cuts-both-off",
            all(d.get("after_down") not in (200, None) for d in done),
            str(done),
        )
    )
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
    relay = runtime is container_cli.APPLE_CONTAINER
    strategy = egress_sidecar.endpoint_strategy(runtime)
    linux = strategy.kind in (
        egress_sidecar.DOCKER_GATEWAY,
        egress_sidecar.PODMAN_PASTA,
    )
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
        refuse: list[tuple[str, int]] = []
        if linux:
            results.append(
                _loopback_only(port, address="127.0.0.1", name="parent:loopback-only")
            )
            refuse.append(("host.docker.internal", port))
        if strategy.kind == egress_sidecar.DOCKER_GATEWAY:
            results += _check_forwarder(
                args, runtime, strategy, port, logs_cli=(cli, name)
            )
            forwarder = egress_relay.RelayControl().port(
                egress_sidecar.layout(runtime).root
            )
            if forwarder is not None:
                refuse.append((strategy.address, forwarder))
        probe = (
            PARENT_PROBE_SOURCE.replace("__ALLOWED__", ALLOWED_HOST)
            .replace("__UNLISTED__", UNLISTED_HOST)
            .replace("__REFUSE__", repr(refuse))
        )
        first = _project(work, "parent-1", probe)
        second = _project(work, "parent-2", probe)
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
            control = egress_relay.RelayControl()
            results.append(
                Result(
                    "parent:relay-survives-first-session",
                    control.is_live(state_dir),
                    control.log_tail(state_dir),
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
    if linux:
        results += grade_parent_refusals(_read_json(first / "egress-parent.json"))
    # Back to direct mode for later checks.
    _write_policy(_policy_text(ALLOWED_HOST))
    egress(args, "restart")
    return results


class _FakeGateway(http.server.BaseHTTPRequestHandler):
    """An OpenAI-compatible model list and one marker path, nothing else."""

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            body = json.dumps({"data": [{"id": LOCAL_MODEL}]}).encode()
        elif self.path.rstrip("/").endswith("/ok"):
            body = LOCAL_SERVICE_BODY.encode()
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def check_local(args, runtime, work: Path) -> list[Result]:
    """A loopback-only host service reached through local-service forwarding."""
    _write_policy(_policy_text(ALLOWED_HOST))
    project = _project(work, "local", LOCAL_PROBE_SOURCE)
    # Keep the user's real gateway key (pass entry) out of the fake gateway.
    shims = work / "local-shims"
    shims.mkdir()
    (shims / "pass").write_text("#!/bin/sh\nexit 1\n")
    (shims / "pass").chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{shims}{os.pathsep}{os.environ.get('PATH', '')}",
        "E2E_EGRESS_FAKE_KEY": "e2e-not-a-secret",
    }
    with socketserver.TCPServer(("127.0.0.1", 0), _FakeGateway) as server:
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        run = _launch(
            args,
            project,
            "python3 probe.py",
            work / "local.log",
            "--agent-proxy",
            f"http://127.0.0.1:{port}/v1",
            "--agent-proxy-key-env",
            "E2E_EGRESS_FAKE_KEY",
            "--model",
            LOCAL_MODEL,
            env=env,
        )
        server.shutdown()
    out = run.stdout + run.stderr
    (work / "local.out").write_text(out)
    results = [Result("local:launch", run.returncode == 0, out[-400:])]
    sidecar_log = egress(args, "log").stdout
    results += [
        Result(r.name, r.ok, r.detail)
        for r in grade_local(
            _read_json(project / "egress-local.json"), sidecar_log, port
        )
    ]
    return results


def isolate_xdg(
    xdg: Path, environ=os.environ, links: Sequence[str] = ("containers",)
) -> None:
    """Point XDG_CONFIG_HOME/XDG_STATE_HOME at ``xdg``, keeping runtime config.

    Podman reads its machine connections and containers.conf from
    ``$XDG_CONFIG_HOME/containers``. Without that directory, `podman` on macOS
    falls back to a local socket that does not exist and every call fails, so
    the user's directory is linked in. ``links`` names the config directories
    linked this way (the copilot-egress suite adds ``gh`` for `gh auth token`).
    Egress policy and sidecar state stay isolated.
    """
    real_config = Path(environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    config = xdg / "config"
    config.mkdir(parents=True, exist_ok=True)
    for name in links:
        if (real_config / name).is_dir():
            (config / name).symlink_to(real_config / name)
    environ["XDG_CONFIG_HOME"] = str(config)
    environ["XDG_STATE_HOME"] = str(xdg / "state")


def main(argv: list[str] | None = None) -> int:
    global PORT, PUBLISH
    args = parse_args(argv)
    PORT = args.port
    runtime = container_cli.select_runtime(args.runtime)
    if runtime is container_cli.CHROOT:
        raise SystemExit("select apple-container, docker, or podman")
    args.runtime = runtime.name
    try:
        PUBLISH = egress_sidecar.endpoint_strategy(runtime).address
    except egress_sidecar.SidecarError as exc:
        raise SystemExit(str(exc)) from None
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
        down = egress(args, "down")  # a clean slate
        if down.returncode != 0:
            results.append(Result("setup:down", False, down.stderr[-200:]))
        for check in args.checks:
            print(f"RUN:  {check}", flush=True)
            try:
                results += {
                    "policy": check_policy,
                    "intercept": check_intercept,
                    "shared": check_shared,
                    "parent": check_parent,
                    "local": check_local,
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
