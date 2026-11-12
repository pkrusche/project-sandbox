#!/usr/bin/env python3
"""Opt-in remote proxy test using temporary, non-billable HTTP/TLS fixtures.

Requires a real container runtime, host openssl, and a non-loopback address the
runtime's containers reach the fixtures at. With Docker or Podman on macOS that
is the runtime's host-loopback alias address (the fixtures listen on 127.0.0.1);
with Apple container it is the address of a forwarder container that relays to
the fixtures on 127.0.0.1 through Apple's host-loopback redirect; otherwise, and
with an explicit --fixture-host, the fixtures listen on that address itself.
No existing proxy or gateway is used or stopped.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import http.client
import http.server
import ipaddress
import json
import os
import secrets
import select
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import unquote

from project_sandbox import agent_proxy, fixture_network

KEY = "project-sandbox-fixture-key-not-a-real-credential"
API_PATH = "/api/{tenant,other}/[v1]"
MODEL = "fixture-model"


class Fixture(http.server.BaseHTTPRequestHandler):
    """Answer only fixture traffic; never forward to arbitrary destinations."""

    @property
    def fixture_server(self) -> FixtureServer:
        assert isinstance(self.server, FixtureServer)
        return self.server

    def log_message(self, format: str, *args) -> None:
        pass

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            pass  # Startup transport probes intentionally close early.
        except OSError as exc:
            # macOS reports some early closes as ENOTCONN instead.
            if exc.errno != errno.ENOTCONN:
                raise

    def record(self):
        self.fixture_server.observations.append(
            (
                self.command,
                self.path,
                {name.lower(): value for name, value in self.headers.items()},
            )
        )

    tunnelled = False

    def do_GET(self):
        self.record()
        role = self.fixture_server.role
        status = 200
        if role == "gateway":
            if self.headers.get("Authorization") != f"Bearer {KEY}":
                status = 401
            elif unquote(self.path) != API_PATH + "/models":
                status = 404
        elif role == "proxy":
            if self.path != (
                "/fixture" if self.tunnelled else "http://origin.test/fixture"
            ):
                status = 403
        elif self.path != "/fixture":
            status = 404
        body = json.dumps({"data": [{"id": MODEL}]}).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_CONNECT(self):
        self.record()
        if self.fixture_server.role == "proxy" and self.path == "origin.test:80":
            # Newer Node versions tunnel plain HTTP targets through CONNECT.
            self.send_response(200)
            self.end_headers()
            self.tunnelled = True
            self.handle_one_request()
            return
        if self.fixture_server.role != "proxy" or self.path != "origin.test:443":
            self.send_error(403)
            return
        # The only tunnel target is our own temporary TLS origin.
        assert self.fixture_server.origin is not None
        with socket.create_connection(
            self.fixture_server.origin, timeout=3
        ) as upstream:
            self.send_response(200)
            self.end_headers()
            while True:
                readable, _, _ = select.select([self.connection, upstream], [], [], 5)
                if not readable:
                    return
                for source in readable:
                    try:
                        data = source.recv(65536)
                        if not data:
                            return
                        target = (
                            upstream if source is self.connection else self.connection
                        )
                        target.sendall(data)
                    except OSError:
                        return


class FixtureServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, host, role, context=None, origin=None, port=0):
        self.address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
        self.role = role
        self.origin = origin
        self.observations: list[tuple[str, str, dict[str, str]]] = []
        super().__init__((host, port), Fixture)
        if context is not None:
            self.socket = context.wrap_socket(self.socket, server_side=True)

    def __enter__(self):
        self.thread = threading.Thread(
            target=self.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=2)


def fixture_address(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
        endpoint = agent_proxy.parse(
            f"http://[{value}]" if ":" in value else f"http://{value}"
        )
        if endpoint.local:
            raise ValueError("loopback does not exercise remote routing")
    except (ValueError, SystemExit) as exc:
        raise argparse.ArgumentTypeError(
            "use a non-loopback, unscoped unicast IP address"
        ) from exc
    return str(address)


LAN_ADVICE = (
    "macOS Local Network privacy, security software, a firewall or a VPN may be "
    "filtering traffic to this machine's own LAN address. On macOS, omit "
    "--fixture-host to reach the fixtures through the runtime's host-loopback "
    "alias; otherwise pass a different --fixture-host."
)
FORWARDER_ADVICE = (
    "The forwarder container reaches the fixtures on host loopback through "
    "Apple container's host.docker.internal redirect to 203.0.113.113; set it up "
    "or recover a stale one as described in docs/apple-container-dns.md, or pass "
    "--fixture-host."
)


def check_round_trip(host: str, port: int, advice: str = LAN_ADVICE) -> None:
    """Fail clearly when TCP to the fixture address connects but data does not flow."""
    connection = http.client.HTTPConnection(host, port, timeout=3)
    try:
        connection.request("GET", "/fixture")
        connection.getresponse().read()
    except (OSError, http.client.HTTPException) as exc:
        raise SystemExit(
            f"Local network traffic to fixture host {host} is blocked: TCP connects "
            f"but an HTTP request fails ({exc.__class__.__name__}: {exc}). {advice}"
        ) from None
    finally:
        connection.close()


def host_blind(args) -> bool:
    """Whether only containers reach the fixture address (a runtime host alias).

    An Apple container forwarder also listens on another address than the
    fixtures, but the host reaches it like any remote address.
    """
    return args.fixture_bind != args.fixture_host and not args.fixture_ports


def san_type(host: str) -> str:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return "DNS"
    return "IP"


def certificates(root: Path, hosts: list[str]) -> ssl.SSLContext:
    def openssl(*args):
        subprocess.run(["openssl", *args], cwd=root, check=True, capture_output=True)

    openssl(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        "ca.key",
        "-out",
        "ca.pem",
        "-days",
        "1",
        "-subj",
        "/CN=Proxy E2E CA",
        "-addext",
        "basicConstraints=critical,CA:TRUE",
        # Python 3.13+ verifies with VERIFY_X509_STRICT, which rejects a CA
        # without keyUsage and a leaf without an authority key identifier.
        "-addext",
        "keyUsage=critical,keyCertSign,cRLSign",
        # The leaf's authority key identifier copies this value. Do not rely
        # on the host OpenSSL configuration to provide it.
        "-addext",
        "subjectKeyIdentifier=hash",
    )
    openssl(
        "req",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        "server.key",
        "-out",
        "server.csr",
        "-subj",
        "/CN=origin.test",
    )
    (root / "extensions").write_text(
        "basicConstraints=CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
        "subjectKeyIdentifier=hash\n"
        "authorityKeyIdentifier=keyid:always\n"
        "subjectAltName=DNS:origin.test,"
        + ",".join(f"{san_type(host)}:{host}" for host in hosts)
        + "\n"
    )
    openssl(
        "x509",
        "-req",
        "-in",
        "server.csr",
        "-CA",
        "ca.pem",
        "-CAkey",
        "ca.key",
        "-CAcreateserial",
        "-out",
        "server.pem",
        "-days",
        "1",
        "-extfile",
        "extensions",
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(root / "server.pem", root / "server.key")
    return context


def validate_observations(gateway, proxy):
    if not any(path == API_PATH for _, path, _ in gateway):
        raise AssertionError(
            "final gateway probe did not preserve the literal API path"
        )
    clients = {
        headers.get("x-proxy-e2e")
        for _, path, headers in gateway
        if unquote(path) == API_PATH + "/models"
        and headers.get("authorization") == f"Bearer {KEY}"
    }
    if not {"urllib", "curl", "node"} <= clients:
        raise AssertionError(f"missing direct authenticated gateway clients: {clients}")
    for method, path in (
        ("GET", "http://origin.test/fixture"),
        ("CONNECT", "origin.test:443"),
    ):
        if not any((m, p) == (method, path) for m, p, _ in proxy):
            raise AssertionError(f"proxy did not receive {method} {path}")
    if any(
        name.lower() in ("authorization", "proxy-authorization")
        for _, _, headers in proxy
        for name in headers
    ):
        raise AssertionError("gateway credentials reached the Internet proxy")


def resolves_to(hostname: str, address: str) -> bool:
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False
    return ipaddress.ip_address(address) in {
        ipaddress.ip_address(info[4][0]) for info in infos
    }


def run_scenario(args, root, context, scheme, gateway_host, name, url_host=None):
    """Bind fixtures to IPs; ``url_host`` names them in URLs (HTTPS needs DNS).

    Proxy URLs name ``args.fixture_host``; the proxy and canary listen on
    ``args.fixture_bind``, which is loopback when a runtime alias or an Apple
    container forwarder reaches them. The host cannot reach an alias address,
    so the CLI then skips its host-side proxy check, and the gateway, whose
    models the host discovers before launch, is a loopback gateway forwarded
    into the sandbox. A forwarder relays only ``args.fixture_ports``, so its
    fixtures listen on those ports, and the gateway stays a remote one.
    """
    ports = args.fixture_ports or {}
    behind_fixture_host = gateway_host in (
        args.fixture_host,
        f"::ffff:{args.fixture_host}",
    )
    alias = host_blind(args) and behind_fixture_host
    forwarded = bool(ports) and behind_fixture_host
    gateway_scheme = "http" if alias else scheme
    if alias:
        gateway_host, url_host_for_gateway = "127.0.0.1", None
    else:
        url_host_for_gateway = url_host
    gateway_bind = args.fixture_bind if forwarded else gateway_host
    project = root / name
    project.mkdir()
    shutil.copyfile(
        Path(__file__).parent / "fixtures/proxy-endpoints-audit.py",
        project / "audit.py",
    )
    with (
        FixtureServer("127.0.0.1", "origin", context) as origin,
        FixtureServer(
            args.fixture_bind,
            "proxy",
            context if scheme == "https" else None,
            ("127.0.0.1", origin.server_port),
            port=ports.get("proxy", 0),
        ) as proxy,
        FixtureServer(
            gateway_bind,
            "gateway",
            context if gateway_scheme == "https" else None,
            port=ports["gateway"] if forwarded else 0,
        ) as gateway,
        FixtureServer(
            args.fixture_bind, "canary", port=ports.get("canary", 0)
        ) as canary,
    ):

        def url(host, port, scheme=scheme):
            authority = f"[{host}]" if ":" in host else host
            return f"{scheme}://{authority}:{port}"

        proxy_url = url(url_host or args.fixture_host, proxy.server_port)
        gateway_url = (
            url(
                url_host_for_gateway or gateway_host,
                gateway.server_port,
                gateway_scheme,
            )
            + API_PATH
        )
        # Prove the denied port really has a live listener before testing
        # denial, and that the host can exchange data with the fixture address
        # (through the forwarder, if any).
        if host_blind(args):
            check_round_trip(args.fixture_bind, canary.server_port)
        else:
            check_round_trip(
                args.fixture_host,
                canary.server_port,
                FORWARDER_ADVICE if ports else LAN_ADVICE,
            )
        (project / "audit.json").write_text(
            json.dumps(
                {
                    "canary_host": args.fixture_host,
                    "canary_port": canary.server_port,
                    # A forwarded loopback gateway has a different address in
                    # the sandbox; the audit then uses OPENAI_BASE_URL.
                    "gateway_url": None if alias else gateway_url,
                }
            )
        )
        command = [
            sys.executable,
            "-m",
            "project_sandbox",
            "--runtime",
            args.runtime,
            "--agent",
            "bash",
            "--no-forward-credentials",
            "--internet-proxy",
            proxy_url,
            *(["--no-internet-proxy-preflight"] if host_blind(args) else []),
            "--agent-proxy",
            gateway_url,
            "--agent-proxy-key-env",
            "PROJECT_SANDBOX_E2E_GATEWAY_KEY",
            "--model",
            MODEL,
            "--ca-cert",
            str(root / "ca.pem"),
            "--image-tag",
            args.image_tag,
            "--prompt-text",
            "python3 audit.py",
            "--timeout",
            str(args.timeout),
            str(project),
            args.base_image,
        ]
        env = {
            **os.environ,
            "PROJECT_SANDBOX_E2E_GATEWAY_KEY": KEY,
            "PASSWORD_STORE_DIR": str(root / "empty-password-store"),
        }
        print(
            f"RUN: {name}: {gateway_url} via "
            f"{'local forwarding' if alias else 'direct routing'}, proxy {proxy_url}",
            flush=True,
        )
        subprocess.run(command, env=env, check=True)
        validate_observations(gateway.observations, proxy.observations)
        print(f"PASS: {name}", flush=True)


def image_tag() -> str:
    # mkdtemp suffixes may contain "_", which Docker rejects next to "-".
    return f"project-sandbox-proxy-endpoints-{secrets.token_hex(4)}:latest"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime", required=True, choices=("docker", "podman", "apple-container")
    )
    parser.add_argument(
        "--fixture-host",
        type=fixture_address,
        help=(
            "Local fixture IP the fixtures listen on and containers connect to "
            "(default: with Docker or Podman on macOS, the runtime's host-loopback "
            "alias address with fixtures on 127.0.0.1; with Apple container on "
            "macOS, a forwarder container's address with fixtures on 127.0.0.1; "
            "otherwise the outbound IPv4 source address)."
        ),
    )
    parser.add_argument(
        "--ipv6-host",
        type=fixture_address,
        help="Optional local native IPv6 address for an additional HTTP scenario.",
    )
    parser.add_argument(
        "--fixture-hostname",
        help=(
            "DNS name resolving to --fixture-host on the host and in the sandbox, "
            "e.g. 192-168-1-10.sslip.io. HTTPS endpoints must be named, so the "
            "HTTPS scenario is skipped without it."
        ),
    )
    parser.add_argument("--base-image", default="python:3.12-slim")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)
    if args.ipv6_host and ipaddress.ip_address(args.ipv6_host).version != 6:
        parser.error("--ipv6-host requires an IPv6 address")
    binary = "container" if args.runtime == "apple-container" else args.runtime
    for required in (binary, "openssl"):
        if shutil.which(required) is None:
            parser.error(f"required executable unavailable: {required}")
    with contextlib.ExitStack() as stack:
        args.fixture_ports = None
        if args.fixture_host is None:
            try:
                if args.runtime == "apple-container" and sys.platform == "darwin":
                    roles = ("proxy", "gateway", "canary")
                    ports = fixture_network.loopback_ports(len(roles))
                    args.fixture_ports = dict(zip(roles, ports, strict=True))
                    fixture = stack.enter_context(
                        fixture_network.apple_forwarder(args.base_image, ports)
                    )
                    listening = f" (forwarding to {fixture.bind})"
                else:
                    fixture = fixture_network.runtime_host(
                        args.runtime, args.base_image
                    )
                    listening = (
                        f" (listening on {fixture.bind})" if fixture.via_alias else ""
                    )
            except ValueError as exc:
                parser.error(str(exc))
            args.fixture_host, args.fixture_bind = fixture.address, fixture.bind
            print(
                f"Using proxy fixture host: {args.fixture_host}{listening}", flush=True
            )
        else:
            args.fixture_bind = args.fixture_host
        if args.fixture_hostname and not resolves_to(
            args.fixture_hostname, args.fixture_host
        ):
            parser.error(
                f"--fixture-hostname {args.fixture_hostname} does not resolve to "
                f"{args.fixture_host}"
            )
        return run_scenarios(args, binary)


def run_scenarios(args, binary):
    parent = Path(__file__).resolve().parents[1] / ".project-sandbox/e2e"
    parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="proxy-endpoints-", dir=parent))
    args.image_tag = image_tag()
    (root / "empty-password-store").mkdir()
    success = False
    try:
        hosts = [args.fixture_host]
        scenarios = [("http", args.fixture_host, "http", None)]
        if args.fixture_hostname:
            hosts.append(args.fixture_hostname)
            scenarios.append(
                ("https", args.fixture_host, "https", args.fixture_hostname)
            )
        else:
            print(
                "SKIP: https: pass --fixture-hostname; HTTPS endpoints must "
                "use a DNS name",
                flush=True,
            )
        if host_blind(args):
            print(
                "SKIP: mapped-ipv4: the gateway is a forwarded loopback gateway "
                "behind a runtime host alias; pass --fixture-host for remote "
                "gateway routing",
                flush=True,
            )
        elif ipaddress.ip_address(args.fixture_host).version == 4:
            mapped = f"::ffff:{args.fixture_host}"
            hosts.append(mapped)
            scenarios.append(("http", mapped, "mapped-ipv4", None))
        if args.ipv6_host:
            hosts.append(args.ipv6_host)
            scenarios.append(("http", args.ipv6_host, "ipv6", None))
        context = certificates(root, hosts)
        for scheme, host, name, url_host in scenarios:
            run_scenario(args, root, context, scheme, host, name, url_host)
        success = True
        return 0
    finally:
        if args.keep or not success:
            print(f"Test artifacts retained: {root}; image: {args.image_tag}")
        else:
            with contextlib.suppress(OSError):
                subprocess.run([binary, "image", "rm", args.image_tag], check=False)
            shutil.rmtree(root)


if __name__ == "__main__":
    raise SystemExit(main())
