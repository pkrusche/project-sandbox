"""Controlled real TCP/TLS fixtures; no public services or container runtime."""

import contextlib
import http.server
import json
import os
import select
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

from project_sandbox import agent_proxy, dockerfile, firewall, internet_proxy


class Origin(http.server.BaseHTTPRequestHandler):
    requests: ClassVar[list[tuple[str, dict[str, str]]]] = []
    status = 200

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            pass  # Transport preflights intentionally close without an HTTP request.

    def log_message(self, format: str, *args) -> None:
        pass

    def do_GET(self):
        self.requests.append((self.path, dict(self.headers)))
        body = json.dumps({"data": [{"id": "fixture-model"}]}).encode()
        self.send_response(self.status)
        if self.status == 302:
            self.send_header("Location", "http://unrelated.invalid/stolen")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ForwardProxy(Origin):
    requests: ClassVar[list[tuple[str, dict[str, str]]]] = []
    status = 200
    allowed_ports: ClassVar[set[int]] = set()

    def do_CONNECT(self):
        self.requests.append((self.path, dict(self.headers)))
        host, port = self.path.rsplit(":", 1)
        if self.status != 200:
            self.send_error(self.status)
            return
        if (
            host not in ("localhost", "127.0.0.1")
            or int(port) not in self.allowed_ports
        ):
            self.send_error(403)
            return
        with socket.create_connection(("127.0.0.1", int(port)), timeout=3) as upstream:
            self.send_response(200)
            self.end_headers()
            sockets = [self.connection, upstream]
            while True:
                readable, _, _ = select.select(sockets, [], [], 3)
                if not readable:
                    break
                for source in readable:
                    try:
                        data = source.recv(65536)
                        if not data:
                            return
                        (
                            upstream if source is self.connection else self.connection
                        ).sendall(data)
                    except OSError:
                        return


@unittest.skipUnless(shutil.which("openssl"), "TLS fixtures require openssl")
class ProxyTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)

        def openssl(*args):
            subprocess.run(
                ["openssl", *args], cwd=cls.root, check=True, capture_output=True
            )

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
            "/CN=Fixture CA",
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
            "/CN=fixture.example",
        )
        (cls.root / "extensions").write_text(
            "basicConstraints=CA:FALSE\nsubjectAltName=DNS:fixture.example,DNS:localhost,IP:127.0.0.1\n"
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
        (cls.root / "index").touch()
        (cls.root / "serial").write_text("01\n")
        (cls.root / "ca.conf").write_text(
            "[ca]\ndefault_ca=fixture\n[fixture]\ndatabase=index\nserial=serial\nnew_certs_dir=.\ncertificate=ca.pem\nprivate_key=ca.key\ndefault_md=sha256\npolicy=policy\n[policy]\ncommonName=supplied\n"
        )
        openssl(
            "ca",
            "-batch",
            "-config",
            "ca.conf",
            "-in",
            "server.csr",
            "-out",
            "expired.pem",
            "-startdate",
            "20000101000000Z",
            "-enddate",
            "20010101000000Z",
            "-extfile",
            "extensions",
        )
        cls.certificates = dockerfile.read_ca_certificates([str(cls.root / "ca.pem")])

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        Origin.requests = []
        Origin.status = 200
        ForwardProxy.requests = []
        ForwardProxy.status = 200

    @contextlib.contextmanager
    def server(self, handler=Origin, *, tls=True, expired=False):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        if tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(
                self.root / ("expired.pem" if expired else "server.pem"),
                self.root / "server.key",
            )
            server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        thread.start()
        try:
            yield server.server_port
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    @contextlib.contextmanager
    def fixture_dns(self):
        original = socket.getaddrinfo

        def resolve(host, *args, **kwargs):
            if host in ("fixture.example", "wrong.example", "2001:db8::1"):
                host = "127.0.0.1"
            return original(host, *args, **kwargs)

        with patch("socket.getaddrinfo", side_effect=resolve):
            yield

    def test_private_ca_discovery_direct_despite_ambient_proxy(self):
        with (
            self.server() as port,
            self.fixture_dns(),
            patch.dict(
                os.environ,
                {
                    "HTTPS_PROXY": "http://127.0.0.1:1",
                    "https_proxy": "http://127.0.0.1:1",
                    "NO_PROXY": "",
                    "no_proxy": "",
                },
            ),
        ):
            self.assertEqual(
                agent_proxy.discover_models(
                    f"https://fixture.example:{port}/api/v1",
                    "private-key",
                    ca_certificates=self.certificates,
                ),
                ["fixture-model"],
            )
        self.assertEqual(Origin.requests[0][0], "/api/v1/models")
        self.assertEqual(Origin.requests[0][1]["Authorization"], "Bearer private-key")

    def test_certificate_failures_never_send_key(self):
        for host, trust, expired in (
            ("fixture.example", (), False),
            ("wrong.example", self.certificates, False),
            ("fixture.example", self.certificates, True),
        ):
            with (
                self.subTest(host=host, expired=expired),
                self.server(expired=expired) as port,
                self.fixture_dns(),
            ):
                with self.assertRaises(SystemExit) as error:
                    agent_proxy.discover_models(
                        f"https://{host}:{port}/v1",
                        "private-key",
                        ca_certificates=trust,
                    )
                self.assertNotIn("private-key", str(error.exception))
                self.assertEqual(Origin.requests, [])
                with self.assertRaisesRegex(
                    SystemExit, "TLS certificate verification failed"
                ):
                    internet_proxy.preflight(
                        internet_proxy.parse(f"https://{host}:{port}"),
                        ca_certificates=trust,
                    )

    def test_redirect_rejected_without_second_request(self):
        Origin.status = 302
        with (
            self.server() as port,
            self.fixture_dns(),
            self.assertRaisesRegex(SystemExit, "HTTP 302"),
        ):
            agent_proxy.discover_models(
                f"https://fixture.example:{port}/v1",
                "private-key",
                ca_certificates=self.certificates,
            )
        self.assertEqual(len(Origin.requests), 1)

    def test_preflight_needs_no_destination_or_http_request(self):
        for tls in (False, True):
            with (
                self.subTest(tls=tls),
                self.server(tls=tls) as port,
                self.fixture_dns(),
            ):
                config = internet_proxy.parse(
                    f"{'https' if tls else 'http'}://fixture.example:{port}"
                )
                internet_proxy.preflight(config, ca_certificates=self.certificates)
                self.assertEqual(Origin.requests, [])

    @unittest.skipUnless(shutil.which("curl"), "curl client is unavailable")
    def test_curl_http_and_connect_through_http_and_https_proxy(self):
        with (
            self.server() as origin_port,
            self.server(tls=False) as plain_port,
        ):
            ForwardProxy.allowed_ports = {origin_port, plain_port}
            for proxy_tls in (False, True):
                with (
                    self.subTest(proxy_tls=proxy_tls),
                    self.server(ForwardProxy, tls=proxy_tls) as proxy_port,
                ):
                    ForwardProxy.requests.clear()
                    proxy = (
                        f"{'https' if proxy_tls else 'http'}://localhost:{proxy_port}"
                    )
                    for target_scheme in ("http", "https"):
                        target_port = (
                            origin_port if target_scheme == "https" else plain_port
                        )
                        result = subprocess.run(
                            [
                                "curl",
                                "--silent",
                                "--show-error",
                                "--fail",
                                "--max-time",
                                "5",
                                "--noproxy",
                                "",
                                "--proxy",
                                proxy,
                                "--proxy-cacert",
                                str(self.root / "ca.pem"),
                                "--cacert",
                                str(self.root / "ca.pem"),
                                f"{target_scheme}://localhost:{target_port}/fixture",
                            ],
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertIn("fixture-model", result.stdout)
                    self.assertTrue(
                        any(
                            path.startswith("http://")
                            or path == f"localhost:{plain_port}"
                            for path, _ in ForwardProxy.requests
                        )
                    )
                    self.assertTrue(
                        any(
                            path == f"localhost:{origin_port}"
                            for path, _ in ForwardProxy.requests
                        )
                    )

    @unittest.skipUnless(shutil.which("curl"), "curl client is unavailable")
    def test_407_does_not_acquire_auth_or_fall_back(self):
        ForwardProxy.status = 407
        with self.server() as origin_port, self.server(ForwardProxy) as proxy_port:
            ForwardProxy.allowed_ports = {origin_port}
            result = subprocess.run(
                [
                    "curl",
                    "--silent",
                    "--show-error",
                    "--fail",
                    "--max-time",
                    "5",
                    "--noproxy",
                    "",
                    "--proxy",
                    f"https://localhost:{proxy_port}",
                    "--proxy-cacert",
                    str(self.root / "ca.pem"),
                    "--cacert",
                    str(self.root / "ca.pem"),
                    f"https://localhost:{origin_port}/fixture",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("407", result.stderr)
            self.assertEqual(Origin.requests, [])
            self.assertTrue(ForwardProxy.requests)
            for _, headers in ForwardProxy.requests:
                self.assertNotIn("Authorization", headers)
                self.assertNotIn("Proxy-Authorization", headers)

    @unittest.skipUnless(shutil.which("node"), "Node client is unavailable")
    def test_node_fetch_proxy_transport_and_direct_gateway_bypass(self):
        if "--use-env-proxy" not in subprocess.check_output(
            ["node", "--help"], text=True
        ):
            self.skipTest("Node environment proxy support is unavailable")
        with (
            self.server() as origin_port,
            self.server(tls=False) as plain_port,
        ):
            ForwardProxy.allowed_ports = {origin_port, plain_port}
            for tls in (False, True):
                with (
                    self.subTest(tls=tls),
                    self.server(ForwardProxy, tls=tls) as proxy_port,
                ):
                    ForwardProxy.requests.clear()
                    env = {
                        k: v
                        for k, v in os.environ.items()
                        if k.lower()
                        not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")
                    }
                    env.update(
                        NODE_USE_ENV_PROXY="1",
                        NODE_EXTRA_CA_CERTS=str(self.root / "ca.pem"),
                        HTTPS_PROXY=f"{'https' if tls else 'http'}://localhost:{proxy_port}",
                        HTTP_PROXY=f"{'https' if tls else 'http'}://localhost:{proxy_port}",
                    )
                    for scheme in ("http", "https"):
                        # Depending on the Node version, the HTTP target is either
                        # answered by the proxy or tunnelled to the plain origin;
                        # HTTPS always tunnels to the controlled TLS origin.
                        port = origin_port if scheme == "https" else plain_port
                        target = f"{scheme}://localhost:{port}/fixture"
                        result = subprocess.run(
                            [
                                "node",
                                "-e",
                                "fetch(process.argv[1]).then(async r => { if (!r.ok) process.exit(2); console.log(await r.text()); }).catch(e => { console.error(e); process.exit(1); })",
                                target,
                            ],
                            env=env,
                            capture_output=True,
                            text=True,
                            timeout=8,
                            check=False,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertIn("fixture-model", result.stdout)
                    self.assertTrue(
                        any(
                            path.startswith("http://")
                            or path == f"localhost:{plain_port}"
                            for path, _ in ForwardProxy.requests
                        )
                    )
                    self.assertTrue(
                        any(
                            path == f"localhost:{origin_port}"
                            for path, _ in ForwardProxy.requests
                        )
                    )
                    count = len(ForwardProxy.requests)
                    env["NO_PROXY"] = "localhost"
                    result = subprocess.run(
                        [
                            "node",
                            "-e",
                            "fetch(process.argv[1], {headers: {Authorization: 'Bearer gateway-key'}}).then(r=> {if(!r.ok) process.exit(2)}).catch(()=>process.exit(1))",
                            f"https://localhost:{origin_port}/v1/models",
                        ],
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=8,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(len(ForwardProxy.requests), count)
                    self.assertEqual(Origin.requests[-1][0], "/v1/models")
                    self.assertEqual(
                        Origin.requests[-1][1]["Authorization"], "Bearer gateway-key"
                    )
                    for _, headers in ForwardProxy.requests:
                        self.assertNotIn("Authorization", headers)

    def test_final_sandbox_tls_probe_rejects_missing_trust_after_host_success(self):
        with (
            self.server() as port,
            self.fixture_dns(),
            tempfile.TemporaryDirectory() as tmp,
        ):
            internet_proxy.preflight(
                internet_proxy.parse(f"https://fixture.example:{port}"),
                ca_certificates=self.certificates,
            )
            text = firewall.render(
                Path(tmp),
                extra_domains=[],
                policy=firewall.INTERNET_PROXY,
                local_destinations=[
                    firewall.TcpDestination(
                        "Internet proxy", "localhost", port, "https", local=False
                    )
                ],
            ).read_text()
            probe = (
                "# "
                + text.split("# Verify TCP or TLS without a public destination", 1)[
                    1
                ].split('echo "Firewall initialized."', 1)[0]
            )
            for trusted in (False, True):
                with self.subTest(trusted=trusted):
                    env = os.environ.copy()
                    if trusted:
                        env["SSL_CERT_FILE"] = str(self.root / "ca.pem")
                    else:
                        env.pop("SSL_CERT_FILE", None)
                    result = subprocess.run(
                        [
                            "bash",
                            "-eu",
                            "-c",
                            # The outer subprocess timeout bounds this fixture;
                            # GNU timeout is only required inside the container.
                            'timeout() { shift; "$@"; };\n'
                            f'python3() {{ {shlex.quote(sys.executable)} "$@"; }};\n'
                            + probe,
                        ],
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=8,
                        check=False,
                    )
                    self.assertEqual(result.returncode == 0, trusted, result.stderr)
                    if not trusted:
                        self.assertIn(
                            "TLS certificate verification failed", result.stderr
                        )
                        self.assertNotIn("sudo container", result.stderr)

    def test_python_ipv6_gateway_bypasses_internet_proxy(self):
        with (
            self.server(tls=False) as port,
            self.server(ForwardProxy, tls=False) as proxy_port,
            self.fixture_dns(),
        ):
            env = internet_proxy.environment(
                internet_proxy.parse(f"http://fixture.example:{proxy_port}"),
                bypass_local_services=False,
                bypass_hosts=("2001:db8::1",),
            )
            with patch.dict(os.environ, env):
                request = urllib.request.Request(
                    f"http://[2001:db8::1]:{port}/v1/models",
                    headers={"Authorization": "Bearer gateway-key"},
                )
                with urllib.request.build_opener().open(request, timeout=3) as reply:
                    self.assertEqual(reply.status, 200)
            self.assertEqual(ForwardProxy.requests, [])
            self.assertEqual(Origin.requests[0][0], "/v1/models")
            self.assertEqual(
                Origin.requests[0][1]["Authorization"], "Bearer gateway-key"
            )

    @unittest.skipUnless(shutil.which("curl"), "curl client is unavailable")
    def test_final_gateway_probe_preserves_path_and_bypasses_ambient_proxy(self):
        with self.server() as port, tempfile.TemporaryDirectory() as tmp:
            path = "/api/{tenant,other}/[v1]"
            url = f"https://localhost:{port}{path}"
            text = firewall.render(
                Path(tmp),
                extra_domains=[],
                agent_proxy_port=port,
                agent_proxy_hostname="localhost",
                agent_proxy_url=url,
            ).read_text()
            probe = (
                "# "
                + text.split("# Exercise the real sandbox-to-host path", 1)[1].split(
                    'echo "Firewall initialized."', 1
                )[0]
            )
            env = {
                **os.environ,
                "HTTPS_PROXY": "http://127.0.0.1:1",
                "https_proxy": "http://127.0.0.1:1",
                "CURL_CA_BUNDLE": str(self.root / "ca.pem"),
            }
            result = subprocess.run(
                ["bash", "-eu", "-c", probe],
                env=env,
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([request[0] for request in Origin.requests], [path])
