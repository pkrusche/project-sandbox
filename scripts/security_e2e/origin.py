"""Loopback-only controlled HTTP parent/origin, with a private fixture CA.

CONNECT terminates at this origin; it never forwards to an external service.
The origin deliberately accepts different TLS SNI and HTTP Host values, so
fronting probes measure the sidecar's boundary without relying on a provider.
"""

import http.server
import json
import ssl
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


class Origin:
    def __init__(
        self, directory: Path, *, listen="127.0.0.1", port=0, direct_tls=False
    ):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, "Security E2E origin")]
        )
        now = datetime.now(UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=None), critical=True
            )
            .add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.DNSName(n)
                        for n in (
                            "allowed.example.test",
                            "blocked.example.test",
                            "api.github.com",
                            "*.example.test",
                            "example.test",
                            "api.openai.com",
                            "api.anthropic.com",
                            "lfs.github.com",
                        )
                    ]
                ),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        self.ca = directory / "origin-ca.pem"
        self.ca.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        private = directory / "origin-key.pem"
        private.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        private.chmod(0o600)
        self.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.tls.load_cert_chain(self.ca, private)
        self.requests = []
        self.lock = threading.Lock()
        self.log = directory / "origin-requests.jsonl"
        origin = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_CONNECT(self):
                self.send_response(200, "Connection established")
                self.end_headers()
                self.wfile.flush()
                self.connection = origin.tls.wrap_socket(
                    self.connection, server_side=True
                )
                self.rfile = self.connection.makefile("rb")
                self.wfile = self.connection.makefile("wb")
                self.close_connection = False

            def do_GET(self):
                row = {
                    "host": self.headers.get("Host"),
                    "path": self.path,
                    "method": self.command,
                }
                with origin.lock:
                    origin.requests.append(row)
                    with origin.log.open("a") as stream:
                        stream.write(json.dumps(row) + "\n")
                body = json.dumps(row).encode()
                redirect = self.path.endswith("/redirect")
                self.send_response(302 if redirect else 200)
                if redirect:
                    self.send_header("Location", "https://blocked.example.test/stolen")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()

            do_POST = do_GET
            do_DELETE = do_GET

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer((listen, port), Handler)
        if direct_tls:
            self.server.socket = self.tls.wrap_socket(
                self.server.socket, server_side=True
            )
        self.server.daemon_threads = True
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


if __name__ == "__main__":
    import argparse
    import tempfile
    import time

    parser = argparse.ArgumentParser(
        description="Controlled TLS origin for deployment NAT64 probes; never forwards requests"
    )
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8443)
    args = parser.parse_args()
    with (
        tempfile.TemporaryDirectory(prefix="security-origin.") as directory,
        Origin(
            Path(directory), listen=args.listen, port=args.port, direct_tls=True
        ) as origin,
    ):
        print(
            f"Controlled TLS origin listening on {args.listen}:{origin.port}",
            flush=True,
        )
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
