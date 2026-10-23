"""Small shared fixtures for proxy transport and CLI tests."""

import contextlib
import socket
import ssl
import subprocess
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

from project_sandbox import cli
from project_sandbox.git_identity import GitIdentity


@contextlib.contextmanager
def mock_container_runtime(runtime):
    with (
        patch.object(cli.container_cli, "select_runtime", return_value=runtime),
        patch.object(cli.container_cli, "ensure_system_started", return_value=0),
        patch.object(cli.container_cli, "image_exists", return_value=True),
        patch.object(cli, "read_identity", return_value=GitIdentity("Ada", "a@b.c")),
    ):
        yield


@contextlib.contextmanager
def tls_server(*, hostname="127.0.0.1"):
    with tempfile.TemporaryDirectory() as tmp:
        cert, key = Path(tmp, "cert.pem"), Path(tmp, "key.pem")
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-subj",
                f"/CN={hostname}",
                "-addext",
                f"subjectAltName={'IP' if hostname == '127.0.0.1' else 'DNS'}:{hostname}",
                "-addext",
                "basicConstraints=critical,CA:TRUE",
                "-addext",
                "keyUsage=critical,keyCertSign,digitalSignature",
                "-keyout",
                str(key),
                "-out",
                str(cert),
            ],
            check=True,
            capture_output=True,
        )
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.load_cert_chain(cert, key)
        yield server, cert.read_bytes()


class FakeProxy:
    """Upstream that answers each request with a prefixed echo, then closes."""

    def __init__(
        self, context: ssl.SSLContext | None = None, *, wait_for_eof: bool = False
    ) -> None:
        self.wait_for_eof = wait_for_eof
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.context = context
        self.stop = threading.Event()
        self.listener.settimeout(0.1)
        self.connection = None
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self.stop.is_set():
            try:
                connection, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.connection = connection
            connection.settimeout(2)
            if self.context is not None:
                try:
                    connection = self.context.wrap_socket(connection, server_side=True)
                    self.connection = connection
                except (OSError, ssl.SSLError):
                    connection.close()
                    continue
            try:
                self._answer(connection)
            except OSError:
                pass

    def _answer(self, connection):
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
        self.stop.set()
        if self.connection is not None:
            with contextlib.suppress(OSError):
                self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
        self.listener.close()
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            raise AssertionError("fixture server did not stop")


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
