#!/usr/bin/env python3
"""Opt-in remote proxy test using temporary, non-billable HTTP/TLS fixtures.

Requires a real container runtime, host openssl, and a non-loopback host address
reachable from that runtime. No existing proxy or gateway is used or stopped.
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

    def __init__(self, host, role, context=None, origin=None):
        self.address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
        self.role = role
        self.origin = origin
        self.observations: list[tuple[str, str, dict[str, str]]] = []
        super().__init__((host, 0), Fixture)
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


def check_round_trip(host: str, port: int) -> None:
    """Fail clearly when the host can connect to its own address but not exchange data."""
    connection = http.client.HTTPConnection(host, port, timeout=3)
    try:
        connection.request("GET", "/fixture")
        connection.getresponse().read()
    except (OSError, http.client.HTTPException) as exc:
        raise SystemExit(
            f"Local network traffic to fixture host {host} is blocked: TCP connects "
            f"but an HTTP request fails ({exc.__class__.__name__}: {exc}). On macOS, "
            "allow your terminal or IDE under System Settings > Privacy & Security > "
            "Local Network and restart it; otherwise check firewall or VPN filtering "
            "of LAN traffic, or pass a different --fixture-host."
        ) from None
    finally:
        connection.close()


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
        "basicConstraints=CA:FALSE\nsubjectAltName=DNS:origin.test,"
        + ",".join(f"IP:{host}" for host in hosts)
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


def run_scenario(args, root, context, scheme, gateway_host, name):
    project = root / name
    project.mkdir()
    shutil.copyfile(
        Path(__file__).parent / "fixtures/proxy-endpoints-audit.py",
        project / "audit.py",
    )
    with (
        FixtureServer("127.0.0.1", "origin", context) as origin,
        FixtureServer(
            args.fixture_host,
            "proxy",
            context if scheme == "https" else None,
            ("127.0.0.1", origin.server_port),
        ) as proxy,
        FixtureServer(
            gateway_host, "gateway", context if scheme == "https" else None
        ) as gateway,
        FixtureServer(args.fixture_host, "canary") as canary,
    ):

        def url(host, port):
            authority = f"[{host}]" if ":" in host else host
            return f"{scheme}://{authority}:{port}"

        proxy_url = url(args.fixture_host, proxy.server_port)
        gateway_url = url(gateway_host, gateway.server_port) + API_PATH
        # Prove the denied port really has a live listener before testing
        # denial, and that the host can exchange data with its own address.
        check_round_trip(args.fixture_host, canary.server_port)
        (project / "audit.json").write_text(
            json.dumps(
                {
                    "canary_host": args.fixture_host,
                    "canary_port": canary.server_port,
                    "gateway_url": gateway_url,
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
            f"RUN: {name}: {gateway_url} via direct routing, proxy {proxy_url}",
            flush=True,
        )
        subprocess.run(command, env=env, check=True)
        validate_observations(gateway.observations, proxy.observations)
        print(f"PASS: {name}", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime", required=True, choices=("docker", "podman", "apple-container")
    )
    parser.add_argument(
        "--fixture-host",
        type=fixture_address,
        help="Local fixture IP (default: outbound IPv4 source address).",
    )
    parser.add_argument(
        "--ipv6-host",
        type=fixture_address,
        help="Optional local native IPv6 address for an additional HTTPS scenario.",
    )
    parser.add_argument("--base-image", default="python:3.12-slim")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)
    if args.fixture_host is None:
        try:
            args.fixture_host = fixture_network.default_host()
        except ValueError as exc:
            parser.error(str(exc))
        print(f"Using proxy fixture host: {args.fixture_host}", flush=True)
    if args.ipv6_host and ipaddress.ip_address(args.ipv6_host).version != 6:
        parser.error("--ipv6-host requires an IPv6 address")
    binary = "container" if args.runtime == "apple-container" else args.runtime
    for required in (binary, "openssl"):
        if shutil.which(required) is None:
            parser.error(f"required executable unavailable: {required}")
    parent = Path(__file__).resolve().parents[1] / ".project-sandbox/e2e"
    parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="proxy-endpoints-", dir=parent))
    args.image_tag = f"project-sandbox-{root.name}:latest"
    (root / "empty-password-store").mkdir()
    success = False
    try:
        hosts = [args.fixture_host]
        scenarios = [
            ("http", args.fixture_host, "http"),
            ("https", args.fixture_host, "https"),
        ]
        if ipaddress.ip_address(args.fixture_host).version == 4:
            mapped = f"::ffff:{args.fixture_host}"
            hosts.append(mapped)
            scenarios.append(("http", mapped, "mapped-ipv4"))
        if args.ipv6_host:
            hosts.append(args.ipv6_host)
            scenarios.append(("https", args.ipv6_host, "ipv6"))
        context = certificates(root, hosts)
        for scheme, host, name in scenarios:
            run_scenario(args, root, context, scheme, host, name)
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
