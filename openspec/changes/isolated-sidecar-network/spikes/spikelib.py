"""Shared host-side helpers for the isolated-sidecar-network spikes (tasks 0.1–0.6).

The spike scripts (``spike_colima.py``, ``spike_apple.py``) build one small
Alpine image with Squid, Python and iproute2, create their own ``psbx-spike-*``
networks and containers, and never touch the project's real sidecar, its
``psbx-sandbox`` / ``psbx-egress-uplink`` networks, the Docker context, a Colima
VM's state or Apple ``container``'s DNS configuration. Each run writes a
Markdown report and the raw JSON to ``results/``; task 0.7 condenses them into
``spikes.md``.
"""

from __future__ import annotations

import base64
import datetime
import getpass
import ipaddress
import json
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(ROOT / "src"))

from project_sandbox import sidecar_image, squid_config

PROBE_SOURCE = (HERE / "probe.py").read_text()
PREFIX = "psbx-spike"
SPIKE_LABEL = "project-sandbox.spike=isolated-sidecar-network"
IMAGE = "psbx-spike-isolated:latest"
GATEWAY = f"{PREFIX}-gw"
PEER = f"{PREFIX}-peer"
BRIDGE_PEER = f"{PREFIX}-bridge"
UPLINK = f"{PREFIX}-uplink"
SANDBOX = f"{PREFIX}-sandbox"
CONTAINERS = (GATEWAY, PEER, BRIDGE_PEER)
# S2's gateway-mode variants of the internal network (Colima only).
SANDBOX_VARIANTS = (f"{PREFIX}-sandbox-isolated", f"{PREFIX}-sandbox-noip")
NETWORKS = (SANDBOX, *SANDBOX_VARIANTS, UPLINK)
PROXY_PORT = 3128
PEER_PORT = 8081
# A port nothing listens on, anywhere: shows refused-vs-timeout behaviour.
CLOSED_PORT = 47911
DOCKERFILE = f"FROM {sidecar_image.BASE_IMAGE}\nRUN apk add --no-cache squid python3 iproute2 curl\n"
SLEEP = ["sh", "-c", "trap 'exit 0' TERM INT; while :; do sleep 3600 & wait; done"]
MARKER = "psbx-spike-host-service"
STREAM_COUNT = 6
STREAM_INTERVAL = 1.0
STREAM_FIRST_DELAY = 0.5
# Outcomes that mean "no way through". A probe error is never one of them.
BLOCKED = {
    "timeout",
    "refused",
    "enetunreach",
    "ehostunreach",
    "resolve-failed",
    "eperm",
    "eacces",
    "econnreset",
    "eaddrnotavail",
    "no-answer",
}


class SpikeError(RuntimeError):
    """A step could not run; the message is shown and recorded."""


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


class Report:
    def __init__(self, runtime: str, out_dir: Path) -> None:
        self.runtime = runtime
        self.started = datetime.datetime.now(datetime.UTC)
        stamp = self.started.strftime("%Y%m%dT%H%M%SZ")
        out_dir.mkdir(parents=True, exist_ok=True)
        self.md_path = out_dir / f"{runtime}-{stamp}.md"
        self.json_path = out_dir / f"{runtime}-{stamp}.json"
        self.lines: list[str] = [
            f"# isolated-sidecar-network spikes: {runtime}",
            "",
            (
                f"Run {self.started.isoformat(timespec='seconds')} by "
                f"`{Path(sys.argv[0]).name} {shlex.join(sys.argv[1:])}`."
            ),
            "",
        ]
        self.commands: list[dict] = []
        self.data: dict = {}
        self.verdicts: dict[str, dict] = {}

    # Markdown building -----------------------------------------------------
    def h(self, title: str, level: int = 2) -> None:
        self.lines += ["", "#" * level + " " + title, ""]
        print(f"\n{'=' * level} {title}", flush=True)

    def p(self, text: str) -> None:
        self.lines += [text, ""]
        print(text, flush=True)

    def code(self, text: str, lang: str = "") -> None:
        self.lines += [f"```{lang}", text.rstrip(), "```", ""]

    def table(self, headers: Sequence[str], rows: Iterable[Sequence[object]]) -> None:
        def cell(value: object) -> str:
            return str(value).replace("|", "\\|").replace("\n", " ")

        rows = list(rows)
        self.lines.append("| " + " | ".join(headers) + " |")
        self.lines.append("|" + "---|" * len(headers))
        for row in rows:
            self.lines.append("| " + " | ".join(cell(c) for c in row) + " |")
            print("  " + "  ".join(cell(c) for c in row), flush=True)
        self.lines.append("")

    def verdict(self, spike: str, value: str, reasons: Iterable[str] = ()) -> None:
        reasons = list(reasons)
        self.verdicts[spike] = {"verdict": value, "reasons": reasons}
        self.lines.append(f"**Suggested verdict for {spike}: {value}**")
        self.lines.append("")
        for reason in reasons:
            self.lines.append(f"- {reason}")
        self.lines.append("")
        print(f"\n>>> {spike}: {value}", flush=True)
        for reason in reasons:
            print(f"    - {reason}", flush=True)

    # Command log -------------------------------------------------------------
    def log(
        self, argv: Sequence[str], proc: subprocess.CompletedProcess, secs: float
    ) -> None:
        shown = ["<probe.py>" if a == PROBE_SOURCE else a for a in argv]
        self.commands.append(
            {
                "argv": shown,
                "rc": proc.returncode,
                "seconds": round(secs, 2),
                "stdout": _tail(proc.stdout),
                "stderr": _tail(proc.stderr),
            }
        )

    def write(self) -> None:
        summary = ["", "## Summary", ""]
        for spike, entry in self.verdicts.items():
            summary.append(f"- **{spike}: {entry['verdict']}**")
        log = ["", "## Command log", "", "<details><summary>Commands run</summary>", ""]
        for entry in self.commands:
            log.append(
                f"- `{shlex.join(entry['argv'])[:400]}` → rc {entry['rc']} ({entry['seconds']}s)"
            )
        log += ["", "</details>", ""]
        self.md_path.write_text(redact("\n".join(self.lines + summary + log) + "\n"))
        self.json_path.write_text(
            redact(
                json.dumps(
                    {
                        "runtime": self.runtime,
                        "started": self.started.isoformat(),
                        "verdicts": self.verdicts,
                        "data": self.data,
                        "commands": self.commands,
                    },
                    indent=2,
                    default=str,
                )
            )
            + "\n"
        )


def redact(text: str) -> str:
    """Hide the home directory and the user name; reports may be shared."""
    home = str(Path.home())
    text = text.replace(home, "~").replace(json.dumps(home)[1:-1], "~")
    user = getpass.getuser()
    return re.sub(rf"\b{re.escape(user)}\b", "<user>", text) if len(user) > 2 else text


def _tail(text: str | bytes | None, limit: int = 4000) -> str:
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    text = text or ""
    return text if len(text) <= limit else "…" + text[-limit:]


def sh(
    argv: Sequence[str],
    report: Report,
    *,
    timeout: float = 300,
    check: bool = False,
) -> subprocess.CompletedProcess:
    start = time.monotonic()
    try:
        proc = subprocess.run(
            list(argv), capture_output=True, check=False, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        proc = subprocess.CompletedProcess(
            argv, 124, _tail(exc.stdout), f"timed out after {timeout}s"
        )
    except FileNotFoundError:
        proc = subprocess.CompletedProcess(argv, 127, "", f"{argv[0]}: not found")
    report.log(argv, proc, time.monotonic() - start)
    if check and proc.returncode != 0:
        shown = ["<probe.py>" if a == PROBE_SOURCE else a for a in argv]
        raise SpikeError(
            f"`{shlex.join(shown)[:300]}` failed (rc {proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()[-800:]}"
        )
    return proc


# --------------------------------------------------------------------------
# Host test services (on the Mac)
# --------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:  # quiet
        pass

    def do_GET(self) -> None:
        self._route()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self._route()

    def _route(self) -> None:
        server: _Server = self.server  # type: ignore[assignment]
        server.requests.append(
            {
                "time": time.time(),
                "server": server.label,
                "client": self.client_address[0],
                "method": self.command,
                "target": self.path,
                "host": self.headers.get("Host"),
            }
        )
        path = self.path.split("?")[0]
        if path.endswith("/api/generate"):
            self._stream(
                "application/x-ndjson",
                lambda i: (
                    json.dumps(
                        {
                            "model": "spike",
                            "response": f"tok{i}",
                            "done": i == STREAM_COUNT - 1,
                        }
                    ).encode()
                    + b"\n"
                ),
            )
        elif path.endswith("/sse"):
            self._stream("text/event-stream", lambda i: f"data: event {i}\n\n".encode())
        else:
            body = json.dumps(
                {
                    "marker": MARKER,
                    "server": server.label,
                    "target": self.path,
                    "host": self.headers.get("Host"),
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def _stream(self, content_type: str, chunk) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.wfile.flush()
        for i in range(STREAM_COUNT):
            time.sleep(STREAM_FIRST_DELAY if i == 0 else STREAM_INTERVAL)
            data = chunk(i)
            self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], label: str, requests: list) -> None:
        self.label = label
        self.requests = requests
        super().__init__(address, _Handler)

    def handle_error(self, request, client_address) -> None:
        # TCP reachability probes connect and close without a request; record
        # the connection instead of printing a traceback.
        self.requests.append(
            {
                "time": time.time(),
                "server": self.label,
                "client": client_address[0],
                "method": "-",
                "target": "(connection without a request)",
                "host": None,
            }
        )


@dataclass
class HostServices:
    """Two test HTTP services on the Mac: one on 127.0.0.1, one on 0.0.0.0.

    Each serves a JSON marker on any path, a fake Ollama NDJSON stream on
    ``/api/generate`` and a one-event-per-second SSE stream on ``/sse``.
    """

    loopback_port: int
    any_port: int
    requests: list = field(default_factory=list)
    _servers: list = field(default_factory=list)

    def start(self) -> None:
        for host, port, label in (
            ("127.0.0.1", self.loopback_port, "loopback"),
            ("0.0.0.0", self.any_port, "any"),
        ):
            try:
                server = _Server((host, port), label, self.requests)
            except OSError as exc:
                self.stop()
                raise SpikeError(
                    f"cannot listen on {host}:{port} for the {label} test service: "
                    f"{exc.strerror}; pick another port"
                ) from exc
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self._servers.append(server)

    def stop(self) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()
        self._servers.clear()

    def requests_since(self, start: float) -> list[dict]:
        return [r for r in self.requests if r["time"] >= start]


def host_ollama_ok(model: str | None) -> bool:
    if not model:
        return False
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3) as r:
            return r.status == 200
    except OSError:
        return False


# --------------------------------------------------------------------------
# Squid
# --------------------------------------------------------------------------


def squid_conf(
    internal_ip: str, host_address: str, services: dict[str, int], extra: Sequence[str]
) -> str:
    """D3's layout: a forward port plus one accel port per service, kept apart.

    The forward port denies the project's real PRIVATE_DESTINATIONS, so the
    host-address checks show what the shipped policy would do.
    """
    lines = [
        "# isolated-sidecar-network spike config (D3)",
        "pid_filename /tmp/spike-squid.pid",
        "cache deny all",
        "cache_mem 8 MB",
        "max_filedescriptors 4096",
        "shutdown_lifetime 1 seconds",
        "dns_defnames off",
        "forwarded_for delete",
        "logformat spike %ts.%03tu %Ss/%>Hs %rm %ru lport=%>lp client=%>a peer=%<a hier=%Sh",
        "access_log stdio:/tmp/spike-access.log spike",
        "cache_log stdio:/tmp/spike-cache.log",
        f"http_port {internal_ip}:{PROXY_PORT} name=fwd",
        "acl fwd_port myportname fwd",
        "acl CONNECT method CONNECT",
        "acl SSL_ports port 443",
        "acl private_dst dst " + " ".join(squid_config.PRIVATE_DESTINATIONS),
    ]
    for name, port in services.items():
        lines += [
            (
                f"http_port {internal_ip}:{port} accel defaultsite={name}.service "
                f"no-vhost name=svc_{name}"
            ),
            (
                f"cache_peer {host_address} parent {port} 0 no-query no-digest "
                f"originserver name=peer_{name}"
            ),
            f"acl svc_{name} myportname svc_{name}",
            f"cache_peer_access peer_{name} allow svc_{name}",
            f"cache_peer_access peer_{name} deny all",
            f"never_direct allow svc_{name}",
        ]
    if services:
        names = " ".join(f"svc_{n}" for n in services)
        lines += [
            f"acl svc_ports myportname {names}",
            "http_access deny svc_ports CONNECT",
            "http_access allow svc_ports",
        ]
    lines += [
        "http_access deny fwd_port private_dst",
        "http_access deny fwd_port CONNECT !SSL_ports",
        "http_access allow fwd_port",
        "http_access deny all",
        *extra,
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Container engine
# --------------------------------------------------------------------------


class Engine:
    """Docker on a Colima context, or Apple ``container``."""

    def __init__(self, base: Sequence[str], *, apple: bool, report: Report) -> None:
        self.base = list(base)
        self.apple = apple
        self.report = report
        self.add_host = not apple  # Apple: decided by S4

    def run(self, *args: str, timeout: float = 300, check: bool = False):
        return sh([*self.base, *args], self.report, timeout=timeout, check=check)

    def exists(self, name: str, network: bool = False) -> bool:
        args = ("network", "inspect", name) if network else ("inspect", name)
        proc = self.run(*args, timeout=60)
        if proc.returncode != 0:
            return False
        out = proc.stdout.strip()
        return out not in ("", "[]", "null")

    def build(self) -> None:
        with tempfile.TemporaryDirectory(prefix="psbx-spike-build-") as tmp:
            (Path(tmp) / "Dockerfile").write_text(DOCKERFILE)
            self.run(
                "build",
                "-t",
                IMAGE,
                "-f",
                str(Path(tmp) / "Dockerfile"),
                tmp,
                timeout=1800,
                check=True,
            )

    def start(
        self,
        name: str,
        networks: Sequence[str],
        *,
        extra: Sequence[str] = (),
        command: Sequence[str] = SLEEP,
        check: bool = True,
    ):
        argv = ["run", "-d", "--name", name, "--label", SPIKE_LABEL]
        for network in networks:
            argv += ["--network", network]
        return self.run(*argv, *extra, IMAGE, *command, timeout=300, check=check)

    def probe(
        self,
        networks: Sequence[str],
        ops: list[dict],
        *,
        extra: Sequence[str] = (),
        timeout: float = 240,
    ) -> list[dict]:
        argv = ["run", "--rm", "--label", SPIKE_LABEL]
        for network in networks:
            argv += ["--network", network]
        proc = self.run(
            *argv,
            *extra,
            IMAGE,
            "python3",
            "-c",
            PROBE_SOURCE,
            json.dumps(ops),
            timeout=timeout,
        )
        return _parse_probe(proc, ops)

    def exec_probe(
        self, container: str, ops: list[dict], timeout: float = 240
    ) -> list[dict]:
        proc = self.run(
            "exec",
            container,
            "python3",
            "-c",
            PROBE_SOURCE,
            json.dumps(ops),
            timeout=timeout,
        )
        return _parse_probe(proc, ops)

    def exec(
        self, container: str, *argv: str, timeout: float = 120, check: bool = False
    ):
        return self.run("exec", container, *argv, timeout=timeout, check=check)

    def rm(self, name: str) -> None:
        self.run("rm", "-f", name, timeout=120)

    def network_rm(self, name: str) -> None:
        if self.apple:
            if self.run("network", "delete", name, timeout=120).returncode != 0:
                self.run("network", "rm", name, timeout=120)
        else:
            self.run("network", "rm", name, timeout=120)

    def image_rm(self) -> None:
        self.run("image", "delete" if self.apple else "rm", IMAGE, timeout=120)

    def leftovers(self) -> list[str]:
        found = [n for n in CONTAINERS if self.exists(n)]
        found += [f"network {n}" for n in NETWORKS if self.exists(n, network=True)]
        return found

    def cleanup(self, *, image: bool = False) -> None:
        for name in CONTAINERS:
            self.rm(name)
        for name in NETWORKS:
            self.network_rm(name)
        if image:
            self.image_rm()

    def pin_egress(self, address: str) -> tuple[list[str], list[dict]]:
        """Run args and setup ops that make ``egress`` resolve to ``address``."""
        if self.add_host:
            return ["--add-host", f"egress:{address}"], []
        return [], [
            {
                "kind": "sh",
                "cmd": ["sh", "-c", f"echo '{address} egress' >> /etc/hosts"],
            }
        ]

    def write_and_start_squid(
        self, container: str, conf: str
    ) -> subprocess.CompletedProcess:
        encoded = base64.b64encode(conf.encode()).decode()
        script = (
            f"echo {encoded} | base64 -d > /etc/squid/spike.conf"
            " && squid -k parse -f /etc/squid/spike.conf"
            " && squid -f /etc/squid/spike.conf"
        )
        return self.exec(container, "sh", "-c", script, timeout=120)


def _parse_probe(proc: subprocess.CompletedProcess, ops: list[dict]) -> list[dict]:
    lines = [line for line in (proc.stdout or "").splitlines() if line.startswith("[")]
    if proc.returncode == 0 and lines:
        try:
            return json.loads(lines[-1])
        except ValueError:
            pass
    detail = (proc.stderr or proc.stdout or "").strip()[-300:]
    return [
        {
            "op": op,
            "outcome": f"error: probe did not run (rc {proc.returncode}): {detail}",
        }
        for op in ops
    ]


# --------------------------------------------------------------------------
# Addresses
# --------------------------------------------------------------------------


def own_ipv4(probe_result: dict) -> list[ipaddress.IPv4Interface]:
    return [ipaddress.IPv4Interface(a["cidr"]) for a in probe_result.get("ipv4", [])]


def address_in(probe_result: dict, network: ipaddress.IPv4Network) -> str | None:
    for iface in own_ipv4(probe_result):
        if iface.ip in network:
            return str(iface.ip)
    return None


def default_gateway(routes: str) -> str | None:
    for line in routes.splitlines():
        parts = line.split()
        if parts[:1] == ["default"] and "via" in parts:
            return parts[parts.index("via") + 1]
    return None


def learn_subnet(engine: Engine, network: str, report: Report) -> dict:
    """Run a throwaway container on ``network`` alone and read its address."""
    result = engine.probe([network], [{"kind": "addrs"}])[0]
    if not result.get("ipv4"):
        raise SpikeError(f"could not read an IPv4 address on {network}: {result}")
    iface = own_ipv4(result)[0]
    info = {
        "subnet": iface.network,
        "probe_address": str(iface.ip),
        "default_gateway": default_gateway(result.get("routes4", "")),
        "first_host": str(next(iface.network.hosts())),
        "routes4": result.get("routes4", ""),
        "routes6": result.get("routes6", ""),
        "ipv6": result.get("ipv6", []),
    }
    report.data[f"subnet:{network}"] = info
    report.p(
        f"`{network}`: subnet {info['subnet']}, default route via "
        f"{info['default_gateway'] or 'none'}"
    )
    report.code(f"$ ip route\n{info['routes4']}\n$ ip -6 route\n{info['routes6']}")
    return info


# --------------------------------------------------------------------------
# Grading
# --------------------------------------------------------------------------


def is_blocked(result: dict) -> bool:
    kind = result["op"]["kind"]
    out = result.get("outcome", "")
    if kind == "udp_dns":
        return out in BLOCKED or (out == "answered" and result.get("answers", 0) == 0)
    if kind == "resolve":
        return out in BLOCKED
    return out in BLOCKED


def describe(result: dict) -> str:
    out = result.get("outcome", "")
    if out == "answered":
        return f"answered rcode={result.get('rcode')} answers={result.get('answers')}"
    if out == "resolved":
        return "resolved " + ", ".join(result.get("addresses", []))
    if out == "http":
        return f"HTTP {result.get('status')}"
    if out == "connected" and "address" in result:
        return f"connected ({result['address']})"
    return out


def containment_table(report: Report, results: list[dict]) -> list[str]:
    """Render probe rows; return the failures (anything that got through)."""
    rows, failures = [], []
    for result in results:
        op = result["op"]
        if op["kind"] in ("sh", "sleep", "read", "addrs"):
            continue
        target = op.get("label") or _target(op)
        ok = is_blocked(result)
        if not ok:
            failures.append(f"{target}: {describe(result)}")
        rows.append(
            (target, op["kind"], describe(result), "blocked" if ok else "**OPEN**")
        )
    report.table(("Probe", "Kind", "Outcome", "Result"), rows)
    return failures


def _target(op: dict) -> str:
    if op["kind"] == "tcp":
        host = op["host"]
        return f"[{host}]:{op['port']}" if ":" in host else f"{host}:{op['port']}"
    if op["kind"] == "udp_dns":
        return f"DNS {op.get('name', 'example.com')} @ {op['server']}"
    if op["kind"] == "resolve":
        return f"resolve {op['name']}"
    return op.get("url") or json.dumps(op)


def tcp(host: str, port: int, label: str | None = None, timeout: float = 3) -> dict:
    op = {"kind": "tcp", "host": host, "port": port, "timeout": timeout}
    if label:
        op["label"] = label
    return op


def udp_dns(server: str, label: str | None = None, name: str = "example.com") -> dict:
    op = {"kind": "udp_dns", "server": server, "name": name}
    if label:
        op["label"] = label
    return op


def tcp_dns(server: str, label: str, name: str = "example.com") -> dict:
    """A DNS query over TCP, graded like ``udp_dns`` (answers = leak)."""
    op = udp_dns(server, label, name=name)
    op["tcp"] = True
    return op


def grade_stream(result: dict, *, scheduled: bool = True) -> tuple[bool, str]:
    """Incremental if the first line arrives well before the end and, for the
    scheduled test streams, each line within a second of its schedule."""
    arrivals = result.get("arrivals") or []
    if result.get("outcome") != "http" or result.get("status") != 200:
        return False, f"{describe(result)}; {len(arrivals)} lines"
    expected = STREAM_COUNT if scheduled else 3
    if len(arrivals) < expected:
        return False, f"only {len(arrivals)} lines arrived"
    first, last = arrivals[0], arrivals[-1]
    detail = f"first line {first:.2f}s, last {last:.2f}s"
    ok = first < last / 2
    if scheduled:
        skew = max(
            abs((a - first) - i * STREAM_INTERVAL) for i, a in enumerate(arrivals)
        )
        detail += f", max skew {skew:.2f}s"
        ok = ok and skew < 1.0
    return ok, detail


def service_checks(
    report: Report,
    engine: Engine,
    *,
    networks: Sequence[str],
    gateway_ip: str,
    services: dict[str, int],
    host_label: str,
    host_address: str,
    host_name: str | None,
    ollama_model: str | None,
    hosts: HostServices,
    extra: Sequence[str] = (),
) -> list[str]:
    """S1/S5's sandbox side: services and streaming through the sidecar, plus
    the D3 separation checks. Returns the failures that block the spike."""
    pin_args, setup = engine.pin_egress(gateway_ip)
    loop, anyp = services["loopback"], services["any"]
    ops = [
        *setup,
        {
            "kind": "http",
            "url": f"http://egress:{loop}/api/tags",
            "label": "loopback service",
        },
        {
            "kind": "http",
            "url": f"http://egress:{anyp}/api/tags",
            "label": "0.0.0.0 service",
        },
        {
            "kind": "stream",
            "url": f"http://egress:{loop}/api/generate",
            "method": "POST",
            "body": '{"model":"spike","prompt":"hi","stream":true}',
            "label": "ndjson",
        },
        {
            "kind": "stream",
            "url": f"http://egress:{loop}/sse",
            "sse": True,
            "label": "sse",
        },
        {
            "kind": "raw",
            "host": "egress",
            "port": loop,
            "label": "pivot GET",
            "payload": "GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n"
            "Connection: close\r\n\r\n",
        },
        {
            "kind": "raw",
            "host": "egress",
            "port": loop,
            "label": "pivot CONNECT",
            "payload": "CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n",
        },
        tcp("egress", 22, "undeclared egress:22"),
        tcp("egress", PEER_PORT, f"undeclared egress:{PEER_PORT}"),
        {
            "kind": "http",
            "url": "http://example.com/",
            "proxy": f"http://egress:{PROXY_PORT}",
            "label": "forward proxy, public host",
        },
        {
            "kind": "http",
            "url": f"http://{host_address}:{loop}/",
            "label": f"forward proxy, host address {host_address}",
            "proxy": f"http://egress:{PROXY_PORT}",
        },
    ]
    if host_name:
        ops.append(
            {
                "kind": "http",
                "url": f"http://{host_name}:{loop}/",
                "label": f"forward proxy, {host_name}",
                "proxy": f"http://egress:{PROXY_PORT}",
            }
        )
    if ollama_model and "ollama" in services:
        ops.append(
            {
                "kind": "stream",
                "url": "http://egress:11434/api/generate",
                "method": "POST",
                "timeout": 120,
                "label": "real ollama",
                "body": json.dumps(
                    {
                        "model": ollama_model,
                        "stream": True,
                        "prompt": "Count from 1 to 30, one per line.",
                    }
                ),
            }
        )
    started = time.time()
    results = engine.probe(networks, ops, extra=[*pin_args, *extra])
    report.data["service_checks"] = results
    by = {r["op"].get("label"): r for r in results}
    failures: list[str] = []
    findings: list[str] = []
    rows = []

    def row(label: str, ok: bool, detail: str, blocking: bool = True) -> None:
        rows.append(
            (label, detail, "ok" if ok else ("**FAIL**" if blocking else "**FINDING**"))
        )
        if not ok:
            (failures if blocking else findings).append(f"{label}: {detail}")

    for label in ("loopback service", "0.0.0.0 service"):
        r = by[label]
        ok = (
            r.get("outcome") == "http"
            and r.get("status") == 200
            and MARKER in r.get("body", "")
        )
        row(f"{label} via egress", ok, describe(r))
    for label, scheduled in (("ndjson", True), ("sse", True), ("real ollama", False)):
        if label in by:
            ok, detail = grade_stream(by[label], scheduled=scheduled)
            row(f"stream: {label}", ok, detail)
    pivot = by["pivot GET"]
    reply = pivot.get("reply", "")
    pivot_ok = "Example Domain" not in reply and (
        MARKER in reply or not reply.startswith("HTTP/1.1 2")
    )
    row(
        "pivot: absolute-URI GET on a service port",
        pivot_ok,
        (reply.splitlines() or [describe(pivot)])[0]
        + (" (served by the host service)" if MARKER in reply else ""),
    )
    connect = by["pivot CONNECT"]
    status = (connect.get("reply", "").splitlines() or [describe(connect)])[0]
    row("pivot: CONNECT on a service port", " 200 " not in status, status)
    for label in ("undeclared egress:22", f"undeclared egress:{PEER_PORT}"):
        row(label, is_blocked(by[label]), describe(by[label]))
    public = by["forward proxy, public host"]
    row(
        "forward proxy reaches a public host",
        public.get("status") == 200,
        describe(public),
    )
    for r in results:
        label = r["op"].get("label", "")
        if label.startswith("forward proxy, host") or (
            host_name and label.endswith(host_name)
        ):
            denied = r.get("outcome") == "http" and r.get("status") == 403
            row(
                f"{label} denied",
                denied,
                describe(r)
                + (
                    " — reached the host service" if MARKER in r.get("body", "") else ""
                ),
                blocking=False,
            )
    report.table(("Check", "Observed", "Result"), rows)
    seen = hosts.requests_since(started)
    report.data["host_requests"] = seen
    if seen:
        report.p(
            "Requests the Mac test services saw (client address as seen by the host):"
        )
        report.table(
            ("Service", "Client", "Method", "Target", "Host header"),
            [
                (s["server"], s["client"], s["method"], s["target"], s["host"])
                for s in seen
            ],
        )
    for finding in findings:
        report.p(f"Finding (policy, not containment): {finding}")
    report.data["service_findings"] = findings
    return failures


def attribution_check(
    report: Report,
    engine: Engine,
    *,
    networks: Sequence[str],
    gateway: str,
    gateway_ip: str,
) -> None:
    """S6: two sandboxes through the forward proxy; compare Squid's client field."""
    pin_args, setup = engine.pin_egress(gateway_ip)
    addresses: dict[str, list[str]] = {}

    # Both sandboxes run at once (sequential --rm containers reuse one address),
    # and the marker is a path: Squid strips query strings from its log.
    def run(tag: str) -> None:
        ops = [
            *setup,
            {"kind": "addrs"},
            {"kind": "sleep", "seconds": 4},
            {
                "kind": "http",
                "url": f"http://example.com/spike-s6-{tag}",
                "proxy": f"http://egress:{PROXY_PORT}",
            },
            {"kind": "sleep", "seconds": 2},
        ]
        results = engine.probe(networks, ops, extra=pin_args)
        addr = next((r for r in results if r["op"]["kind"] == "addrs"), {})
        addresses[tag] = [str(i.ip) for i in own_ipv4(addr)]

    threads = [threading.Thread(target=run, args=(tag,)) for tag in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    log = engine.exec(gateway, "cat", "/tmp/spike-access.log").stdout
    rows = []
    clients = {}
    for tag in ("a", "b"):
        lines = [line for line in log.splitlines() if f"/spike-s6-{tag}" in line]
        client = next(
            (
                part.split("=", 1)[1]
                for line in lines
                for part in line.split()
                if part.startswith("client=")
            ),
            None,
        )
        clients[tag] = client
        rows.append(
            (
                f"sandbox {tag}",
                ", ".join(addresses[tag]),
                client or "(not logged)",
                lines[-1] if lines else "",
            )
        )
    report.table(("Sandbox", "Own address", "Squid client=", "Log line"), rows)
    distinct = (
        clients["a"]
        and clients["b"]
        and clients["a"] != clients["b"]
        and clients["a"] in addresses["a"]
        and clients["b"] in addresses["b"]
    )
    report.data["s6"] = {"addresses": addresses, "clients": clients, "log": log[-4000:]}
    report.verdict(
        "S6",
        "distinct per-sandbox addresses"
        if distinct
        else "addresses NOT distinct or not logged (informational)",
    )
    if not distinct:
        report.p("Access log tail:")
        report.code(log[-2000:])


def wait_for_squid(engine: Engine, gateway: str, ip: str, report: Report) -> None:
    for _ in range(20):
        result = engine.exec_probe(gateway, [tcp(ip, PROXY_PORT, timeout=1)])[0]
        if result.get("outcome") == "connected":
            return
        time.sleep(0.5)
    log = engine.exec(gateway, "cat", "/tmp/spike-cache.log").stdout
    report.code(log[-3000:])
    raise SpikeError(f"Squid did not start listening on {ip}:{PROXY_PORT}")
