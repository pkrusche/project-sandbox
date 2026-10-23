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

    def __init__(
        self, context: ssl.SSLContext | None = None, *, wait_for_eof: bool = False
    ) -> None:
        self.wait_for_eof = wait_for_eof
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
                # wait_for_eof answers only after the client's half-close.
                while self.wait_for_eof or not request.endswith(b"\r\n\r\n"):
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    request += chunk
                connection.sendall(b"upstream:" + request)

    def close(self) -> None:
        self.listener.close()


def exchange(port: int, payload: bytes, *, half_close: bool = False) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
        client.sendall(payload)
        if half_close:
            client.shutdown(socket.SHUT_WR)
        reply = b""
        # A refused upstream closes the client with unread data, which can RST.
        with contextlib.suppress(ConnectionResetError):
            while chunk := client.recv(4096):
                reply += chunk
    return reply


def _drain(connection: socket.socket, into: bytearray, size: int) -> None:
    while len(into) < size:
        chunk = connection.recv(1 << 16)
        if not chunk:
            return
        into += chunk


def relay_warnings(warnings: list[str]) -> list[str]:
    """Drop port fallback notices: without SO_REUSEADDR a recently used
    preferred port can still be in TIME_WAIT."""
    return [warning for warning in warnings if "random port" not in warning]


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
        self.assertIn("random port", warnings[0])
        # A fallback port no longer affects build inputs, so no cache warning.
        self.assertNotIn("cache", warnings[0])

    def test_listener_does_not_set_reuseaddr(self):
        # On BSD/macOS SO_REUSEADDR would allow binding 127.0.0.1:P while
        # another process listens on *:P.
        forwarder = build_proxy_forwarder.BuildProxyForwarder(
            endpoint("http://proxy.example:3128"), warn=lambda _message: None
        )
        self.addCleanup(forwarder.close)
        self.assertEqual(
            forwarder._listener.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR), 0
        )

    def test_wildcard_listener_on_preferred_port_forces_fallback(self):
        warnings: list[str] = []
        with socket.create_server(("0.0.0.0", 0), reuse_port=False) as busy:
            port = busy.getsockname()[1]
            with patch.object(
                build_proxy_forwarder, "preferred_port", return_value=port
            ):
                forwarder = build_proxy_forwarder.BuildProxyForwarder(
                    endpoint("http://proxy.example:3128"), warn=warnings.append
                )
            self.addCleanup(forwarder.close)
            self.assertNotEqual(forwarder.port, port)
        self.assertEqual(len(warnings), 1)

    def test_full_duplex_bulk_transfer_does_not_deadlock(self):
        # The proxy streams a large reply while it still reads, and the client
        # uploads far more than socket buffers hold before it reads. A relay
        # blocked writing to the client then never reads the upload.
        size = 8 * 1024 * 1024
        payload = bytes(range(256)) * (size // 256)
        with socket.create_server(("127.0.0.1", 0)) as upstream_listener:
            received_upstream = bytearray()

            def upstream() -> None:
                connection, _ = upstream_listener.accept()
                with connection:
                    reader = threading.Thread(
                        target=_drain, args=(connection, received_upstream, size)
                    )
                    reader.start()
                    connection.sendall(payload)
                    reader.join(timeout=30)

            server = threading.Thread(target=upstream, daemon=True)
            server.start()
            target = endpoint(f"http://127.0.0.1:{upstream_listener.getsockname()[1]}")
            with build_proxy_forwarder.BuildProxyForwarder(target) as forwarder:
                with socket.create_connection(
                    ("127.0.0.1", forwarder.port), timeout=30
                ) as client:
                    received_client = bytearray()
                    # The reader only starts after the full write, mirroring a
                    # peer that uploads before it reads.
                    writer_done = threading.Event()

                    def write() -> None:
                        client.sendall(payload)
                        writer_done.set()

                    writer = threading.Thread(target=write, daemon=True)
                    writer.start()
                    self.assertTrue(
                        writer_done.wait(30), "upload stalled: relay deadlocked"
                    )
                    _drain(client, received_client, size)
                # Closing the forwarder stops relays, so let the upload finish.
                server.join(timeout=30)
        self.assertEqual(bytes(received_client), payload)
        self.assertEqual(bytes(received_upstream), payload)

    def test_client_half_close_still_receives_plain_reply(self):
        upstream = FakeProxy(wait_for_eof=True)
        self.addCleanup(upstream.close)
        target = endpoint(f"http://127.0.0.1:{upstream.port}")
        request = b"GET http://deb.debian.org/ HTTP/1.0\r\n\r\n"
        with build_proxy_forwarder.BuildProxyForwarder(target) as forwarder:
            self.assertEqual(
                exchange(forwarder.port, request, half_close=True),
                b"upstream:" + request,
            )

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
        warnings = relay_warnings(warnings)
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
            # A client half-close must not drop the TLS upstream's reply.
            self.assertEqual(
                exchange(forwarder.port, request, half_close=True),
                b"upstream:" + request,
            )

        warnings: list[str] = []
        with build_proxy_forwarder.BuildProxyForwarder(
            target, warn=warnings.append
        ) as forwarder:
            self.assertEqual(exchange(forwarder.port, request), b"")
        warnings = relay_warnings(warnings)
        self.assertEqual(len(warnings), 1)
        self.assertIn("--build-ca-cert", warnings[0])


class CliForwardingTests(TestCase):
    def run_build(self, runtime, *extra, cache_hit=False):
        constructed: list[bool] = []
        observed: dict[str, object] = {"constructed": constructed, "build_args": None}
        lock_held = threading.Event()
        real_lock = cli.build_cache.build_lock
        real_forwarder = build_proxy_forwarder.BuildProxyForwarder

        @contextlib.contextmanager
        def build_lock(context_dir):
            with real_lock(context_dir):
                lock_held.set()
                try:
                    yield
                finally:
                    lock_held.clear()

        def forwarder(*args, **kwargs):
            constructed.append(lock_held.is_set())
            return real_forwarder(*args, **kwargs)

        def build_image(*, context_dir, build_args=(), **_kwargs):
            observed["dockerfile"] = (context_dir / "Dockerfile").read_text()
            observed["devcontainer"] = (
                context_dir / "Dockerfile.devcontainer"
            ).read_text()
            observed["build_args"] = list(build_args)
            values = dict(
                arg.split("=", 1) for arg in build_args if arg != "--build-arg"
            )
            observed["env"] = values
            url = values.get("http_proxy", "")
            port = (
                int(url.rsplit(":", 1)[1])
                if local_service_network.APPLE_LOCALHOST_ADDRESS in url
                else None
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
                patch.object(cli.build_cache, "build_lock", build_lock),
                patch.object(cli.build_cache, "is_cache_valid", return_value=cache_hit),
                patch.object(
                    cli.build_proxy_forwarder,
                    "BuildProxyForwarder",
                    side_effect=forwarder,
                ),
                patch.object(
                    cli, "read_identity", return_value=GitIdentity("Ada", "a@b.c")
                ),
                contextlib.redirect_stdout(out),
            ):
                rc = cli.main(
                    [
                        "--build-only",
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
        observed = self.run_build(
            container_cli.APPLE_CONTAINER, "--build-no-proxy", "mirror.corp"
        )
        port = build_proxy_forwarder.preferred_port(
            endpoint("https://corp.proxy.example:3128")
        )
        self.assertEqual(observed["constructed"], [True])  # Bound inside the lock.
        if "random port" not in observed["output"]:  # Not in TIME_WAIT.
            self.assertEqual(observed["port"], port)
        port = observed["port"]
        self.assertTrue(observed["listening"])
        self.assertEqual(
            observed["env"],
            build_network.environment(f"http://203.0.113.113:{port}", ("mirror.corp",)),
        )
        for text in (observed["dockerfile"], observed["devcontainer"]):
            self.assertNotIn("203.0.113.113", text)
            self.assertNotIn("corp.proxy.example", text)
            self.assertNotIn("_proxy", text)
        self.assertIn("container system dns create", observed["output"])
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()

    def test_cached_image_starts_no_forwarder(self):
        observed = self.run_build(container_cli.APPLE_CONTAINER, cache_hit=True)
        self.assertEqual(observed["constructed"], [])
        self.assertIsNone(observed["build_args"])
        self.assertIn("Reusing cached image", observed["output"])
        self.assertNotIn("container system dns create", observed["output"])

    def test_busy_port_does_not_change_rendered_dockerfile(self):
        port = build_proxy_forwarder.preferred_port(
            endpoint("https://corp.proxy.example:3128")
        )
        free = self.run_build(container_cli.APPLE_CONTAINER)
        try:
            busy = socket.create_server(("127.0.0.1", port))
        except OSError:
            self.skipTest("preferred port unavailable for this test")
        with busy:
            fallback = self.run_build(container_cli.APPLE_CONTAINER)
        self.assertNotEqual(fallback["port"], port)
        self.assertEqual(free["dockerfile"], fallback["dockerfile"])
        self.assertEqual(free["devcontainer"], fallback["devcontainer"])

    def test_other_runtimes_use_the_proxy_verbatim(self):
        observed = self.run_build(container_cli.DOCKER)
        self.assertEqual(observed["constructed"], [])
        self.assertIsNone(observed["port"])
        self.assertEqual(
            observed["env"],
            build_network.environment("https://corp.proxy.example:3128"),
        )
        self.assertNotIn("corp.proxy.example", observed["dockerfile"])

    def test_dry_run_describes_forwarding_without_listening(self):
        target = "https://corp.proxy.example:3128"
        port = build_proxy_forwarder.preferred_port(endpoint(target))
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with (
                patch.object(
                    cli, "read_identity", return_value=GitIdentity("Ada", "a@b.c")
                ),
                patch.object(
                    cli.build_proxy_forwarder,
                    "BuildProxyForwarder",
                    side_effect=AssertionError("dry-run must not listen"),
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
        self.assertIn("--build-arg 'http_proxy=<redacted>'", out.getvalue())
        self.assertNotIn("corp.proxy.example", out.getvalue())
