"""Apple container builds reach a VPN-only proxy through a host-loopback forwarder."""

import contextlib
import io
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
from pathlib import Path
from unittest import TestCase, skipUnless
from unittest.mock import patch

from project_sandbox import (
    build_network,
    build_proxy_forwarder,
    cli,
    container_cli,
    local_service_network,
)
from project_sandbox.git_identity import GitIdentity


class FakeProxy:
    """Upstream that answers each request with a prefixed echo, then closes."""

    def __init__(self, context: ssl.SSLContext | None = None) -> None:
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.context = context
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            if self.context is not None:
                try:
                    connection = self.context.wrap_socket(connection, server_side=True)
                except (OSError, ssl.SSLError):
                    connection.close()
                    continue
            with connection:
                request = b""
                while not request.endswith(b"\r\n\r\n"):
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    request += chunk
                connection.sendall(b"upstream:" + request)

    def close(self) -> None:
        self.listener.close()


def exchange(port: int, payload: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
        client.sendall(payload)
        reply = b""
        # A refused upstream closes the client with unread data, which can RST.
        with contextlib.suppress(ConnectionResetError):
            while chunk := client.recv(4096):
                reply += chunk
    return reply


def endpoint(url: str):
    return build_network.parse(url)


class ForwarderTests(TestCase):
    def test_relays_plain_proxy_traffic_and_uses_apple_localhost_url(self):
        upstream = FakeProxy()
        self.addCleanup(upstream.close)
        target = endpoint(f"http://127.0.0.1:{upstream.port}")
        with build_proxy_forwarder.BuildProxyForwarder(target) as forwarder:
            self.assertEqual(
                forwarder.sandbox_url,
                f"http://{local_service_network.APPLE_LOCALHOST_ADDRESS}:"
                f"{forwarder.port}",
            )
            request = b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n"
            self.assertEqual(exchange(forwarder.port, request), b"upstream:" + request)

    def test_stops_listening_on_close(self):
        target = endpoint("http://proxy.example:3128")
        forwarder = build_proxy_forwarder.BuildProxyForwarder(target)
        forwarder.start()
        port = forwarder.port
        forwarder.close()
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()

    def test_preferred_port_is_stable_and_busy_port_falls_back(self):
        target = endpoint("https://corp.proxy.example:3128")
        preferred = build_proxy_forwarder.preferred_port(target)
        self.assertEqual(preferred, build_proxy_forwarder.preferred_port(target))
        self.assertNotEqual(
            preferred,
            build_proxy_forwarder.preferred_port(endpoint("http://other.example")),
        )
        warnings: list[str] = []
        with socket.create_server(("127.0.0.1", 0)) as busy:
            with patch.object(
                build_proxy_forwarder,
                "preferred_port",
                return_value=busy.getsockname()[1],
            ):
                forwarder = build_proxy_forwarder.BuildProxyForwarder(
                    target, warn=warnings.append
                )
            self.addCleanup(forwarder.close)
            self.assertNotEqual(forwarder.port, busy.getsockname()[1])
        self.assertEqual(len(warnings), 1)
        self.assertIn("cache", warnings[0])

    def test_unreachable_upstream_closes_client_and_warns_once(self):
        with socket.create_server(("127.0.0.1", 0)) as reserved:
            closed_port = reserved.getsockname()[1]
        warnings: list[str] = []
        target = endpoint(f"http://127.0.0.1:{closed_port}")
        with build_proxy_forwarder.BuildProxyForwarder(
            target, warn=warnings.append
        ) as forwarder:
            for _ in range(2):
                self.assertEqual(
                    exchange(forwarder.port, b"GET / HTTP/1.1\r\n\r\n"), b""
                )
        self.assertEqual(len(warnings), 1)
        self.assertIn("could not connect", warnings[0])

    @skipUnless(shutil.which("openssl"), "openssl is required to create a test CA")
    def test_https_upstream_is_verified_with_build_cas(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert, key = Path(tmp, "cert.pem"), Path(tmp, "key.pem")
            subprocess.run(
                [
                    "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-days", "1", "-subj", "/CN=127.0.0.1",
                    "-addext", "subjectAltName=IP:127.0.0.1",
                    "-addext", "basicConstraints=critical,CA:TRUE",
                    "-addext", "keyUsage=critical,keyCertSign,digitalSignature",
                    "-keyout", str(key), "-out", str(cert),
                ],
                check=True,
                capture_output=True,
            )  # fmt: skip
            server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server.load_cert_chain(cert, key)
            pem = cert.read_bytes()
        upstream = FakeProxy(server)
        self.addCleanup(upstream.close)
        target = endpoint(f"https://127.0.0.1:{upstream.port}")
        request = b"GET http://deb.debian.org/ HTTP/1.1\r\n\r\n"

        with build_proxy_forwarder.BuildProxyForwarder(
            target, ca_certificates=(pem,)
        ) as forwarder:
            self.assertEqual(exchange(forwarder.port, request), b"upstream:" + request)

        warnings: list[str] = []
        with build_proxy_forwarder.BuildProxyForwarder(
            target, warn=warnings.append
        ) as forwarder:
            self.assertEqual(exchange(forwarder.port, request), b"")
        self.assertEqual(len(warnings), 1)
        self.assertIn("--build-ca-cert", warnings[0])


class CliForwardingTests(TestCase):
    def run_build(self, runtime, *extra):
        observed: dict[str, object] = {}

        def build_image(*, context_dir, **_kwargs):
            text = (context_dir / "Dockerfile").read_text()
            observed["dockerfile"] = text
            observed["devcontainer"] = (
                context_dir / "Dockerfile.devcontainer"
            ).read_text()
            port = next(
                (
                    int(line.split(":")[-1].rstrip('"'))
                    for line in text.splitlines()
                    if line.startswith("ENV http_proxy=")
                    and local_service_network.APPLE_LOCALHOST_ADDRESS in line
                ),
                None,
            )
            observed["port"] = port
            if port is not None:
                with socket.create_connection(("127.0.0.1", port), timeout=2):
                    observed["listening"] = True
            return 0

        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with (
                patch.object(cli.container_cli, "select_runtime", return_value=runtime),
                patch.object(
                    cli.container_cli, "ensure_system_started", return_value=0
                ),
                patch.object(
                    cli.container_cli, "host_build_identity", return_value=None
                ),
                patch.object(cli.container_cli, "image_exists", return_value=True),
                patch.object(cli.container_cli, "build_image", side_effect=build_image),
                patch.object(
                    cli, "read_identity", return_value=GitIdentity("Ada", "a@b.c")
                ),
                contextlib.redirect_stdout(out),
            ):
                rc = cli.main(
                    [
                        "--build-only",
                        "--force-build",
                        "--no-forward-credentials",
                        "--no-verify-dockerfile",
                        "--build-proxy",
                        "https://corp.proxy.example:3128",
                        *extra,
                        tmp,
                        "debian:bookworm",
                    ]
                )
        self.assertEqual(rc, 0)
        observed["output"] = out.getvalue()
        return observed

    def test_apple_container_build_uses_forwarder_only_during_build(self):
        observed = self.run_build(container_cli.APPLE_CONTAINER)
        port = build_proxy_forwarder.preferred_port(
            endpoint("https://corp.proxy.example:3128")
        )
        self.assertEqual(observed["port"], port)
        self.assertTrue(observed["listening"])
        for text in (observed["dockerfile"], observed["devcontainer"]):
            self.assertIn(f'ENV https_proxy="http://203.0.113.113:{port}"', text)
            self.assertNotIn("corp.proxy.example", text)
        self.assertIn("container system dns create", observed["output"])
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()

    def test_other_runtimes_use_the_proxy_verbatim(self):
        observed = self.run_build(container_cli.DOCKER)
        self.assertIsNone(observed["port"])
        self.assertIn(
            'ENV https_proxy="https://corp.proxy.example:3128"',
            observed["dockerfile"],
        )

    def test_dry_run_describes_forwarding_without_listening(self):
        target = "https://corp.proxy.example:3128"
        port = build_proxy_forwarder.preferred_port(endpoint(target))
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with (
                patch.object(
                    cli, "read_identity", return_value=GitIdentity("Ada", "a@b.c")
                ),
                contextlib.redirect_stdout(out),
            ):
                rc = cli.main(
                    [
                        "--dry-run",
                        "--build-only",
                        "--runtime",
                        "apple-container",
                        "--build-proxy",
                        target,
                        tmp,
                        "debian:bookworm",
                    ]
                )
            self.assertEqual(list(Path(tmp).iterdir()), [])
        self.assertEqual(rc, 0)
        self.assertIn(f"127.0.0.1:{port}", out.getvalue())
        self.assertNotIn("corp.proxy.example", out.getvalue())
