"""Exercise the opt-in harness without starting a runtime or changing host policy."""

import argparse
import errno
import importlib.util
import json
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import patch

from project_sandbox import agent_proxy, internet_proxy, proxy_endpoint

ROOT = Path(__file__).parents[1]


def load_script(path):
    spec = importlib.util.spec_from_file_location("proxy_endpoint_fixture", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProxyEndpointHarnessTests(unittest.TestCase):
    def test_image_tag_is_a_valid_docker_reference(self):
        # Regression: mkdtemp suffixes like "_v3nv9so" produced "-_", which
        # Docker rejects as an invalid reference format.
        checker = load_script(ROOT / "scripts/e2e-proxy-endpoints.py")
        component = r"[a-z0-9]+(?:(?:\.|_|__|-+)[a-z0-9]+)*"
        for _ in range(50):
            tag = checker.image_tag()
            self.assertRegex(tag, rf"^{component}:[A-Za-z0-9_][A-Za-z0-9_.-]*$")

    def test_audit_does_not_mistake_closed_or_unreachable_ports_for_denial(self):
        audit = load_script(ROOT / "scripts/fixtures/proxy-endpoints-audit.py")
        for error in (None, ConnectionRefusedError(), TimeoutError()):
            with (
                self.subTest(error=error),
                patch("socket.create_connection", side_effect=error),
                self.assertRaises(AssertionError),
            ):
                audit.assert_port_denied("192.0.2.1", 8080)
        with patch(
            "socket.create_connection",
            side_effect=OSError(errno.EHOSTUNREACH, "rejected"),
        ):
            audit.assert_port_denied("192.0.2.1", 8080)

    def test_fixture_addresses_require_remote_unicast_literals(self):
        checker = load_script(ROOT / "scripts/e2e-proxy-endpoints.py")
        for host in ("192.0.2.1", "2001:db8::1", "::ffff:192.0.2.1"):
            with self.subTest(host=host):
                self.assertTrue(checker.fixture_address(host))
        for host in (
            "localhost",
            "127.0.0.1",
            "::1",
            "0.0.0.0",
            "ff02::1",
            "fe80::1%eth0",
        ):
            with self.subTest(host=host), self.assertRaises(argparse.ArgumentTypeError):
                checker.fixture_address(host)

    def test_round_trip_check_explains_connect_only_local_network(self):
        checker = load_script(ROOT / "scripts/e2e-proxy-endpoints.py")
        with checker.FixtureServer("127.0.0.1", "canary") as canary:
            checker.check_round_trip("127.0.0.1", canary.server_port)
        # Accept TCP but drop the request, as macOS Local Network blocking does.
        with socket.create_server(("127.0.0.1", 0)) as listener:
            port = listener.getsockname()[1]

            def drop():
                connection, _ = listener.accept()
                connection.close()

            thread = threading.Thread(target=drop, daemon=True)
            thread.start()
            with self.assertRaises(SystemExit) as raised:
                checker.check_round_trip("127.0.0.1", port)
            thread.join(timeout=2)
        self.assertIn("Local Network", str(raised.exception))
        self.assertIn("127.0.0.1", str(raised.exception))

    def test_gateway_unavailable_error_includes_cause(self):
        with socket.create_server(("127.0.0.1", 0)) as listener:
            port = listener.getsockname()[1]

            def drop():
                connection, _ = listener.accept()
                connection.recv(65536)
                connection.close()

            thread = threading.Thread(target=drop, daemon=True)
            thread.start()
            with self.assertRaises(SystemExit) as raised:
                agent_proxy.discover_models(
                    f"http://127.0.0.1:{port}/v1", "key", timeout=3
                )
            thread.join(timeout=2)
        self.assertIn(
            "Agent proxy is unavailable (RemoteDisconnected)", str(raised.exception)
        )

    def test_observation_validation_rejects_missing_routes_and_key_leaks(self):
        checker = load_script(ROOT / "scripts/e2e-proxy-endpoints.py")
        gateway = [("GET", checker.API_PATH, {})] + [
            (
                "GET",
                checker.API_PATH + "/models",
                {
                    "x-proxy-e2e": client,
                    "authorization": "Bearer " + checker.KEY,
                },
            )
            for client in ("urllib", "curl", "node")
        ]
        proxy = [
            ("GET", "http://origin.test/fixture", {}),
            ("CONNECT", "origin.test:443", {}),
        ]
        checker.validate_observations(gateway, proxy)
        for bad_gateway, bad_proxy in (
            (gateway[1:], proxy),
            (gateway[:-1], proxy),
            (gateway, proxy[:1]),
            (
                gateway,
                [
                    *proxy,
                    (
                        "GET",
                        "http://origin.test/fixture",
                        {"authorization": "Bearer secret"},
                    ),
                ],
            ),
        ):
            with (
                self.subTest(gateway=bad_gateway, proxy=bad_proxy),
                self.assertRaises(AssertionError),
            ):
                checker.validate_observations(bad_gateway, bad_proxy)

    def test_runner_requires_builds(self):
        for options in (
            ["--no-build"],
            ["--proxy-fixture-host", "192.0.2.1", "--no-build"],
        ):
            with self.subTest(options=options):
                result = subprocess.run(
                    [
                        "bash",
                        str(ROOT / "scripts/run-e2e-tests.sh"),
                        "--only",
                        "proxy-endpoints",
                        "--runtime",
                        "docker",
                        *options,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("requires a real runtime", result.stdout)

    def test_runner_dispatches_only_when_explicitly_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_uv = root / "uv"
            log = root / "arguments"
            fake_uv.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$@" > "$PROXY_E2E_ARGUMENT_LOG"\n'
            )
            fake_uv.chmod(0o755)
            result = subprocess.run(
                [
                    "bash",
                    str(ROOT / "scripts/run-e2e-tests.sh"),
                    "--only",
                    "proxy-endpoints",
                    "--runtime",
                    "docker",
                    "--proxy-fixture-host",
                    "192.0.2.1",
                    "--proxy-fixture-ipv6-host",
                    "2001:db8::1",
                    "--proxy-fixture-hostname",
                    "fixture.example",
                ],
                env={
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "PROXY_E2E_ARGUMENT_LOG": str(log),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(
                log.read_text().splitlines(),
                [
                    "run",
                    "python",
                    str(ROOT / "scripts/e2e-proxy-endpoints.py"),
                    "--runtime",
                    "docker",
                    "--base-image",
                    "python:3.12-slim",
                    "--fixture-host",
                    "192.0.2.1",
                    "--ipv6-host",
                    "2001:db8::1",
                    "--fixture-hostname",
                    "fixture.example",
                ],
            )

    @unittest.skipUnless(shutil.which("openssl"), "openssl unavailable")
    def test_fixture_certificates_pass_strict_proxy_tls_verification(self):
        checker = load_script(ROOT / "scripts/e2e-proxy-endpoints.py")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = checker.certificates(root, ["127.0.0.1"])
            # The sandbox preflight verifies with this context, which is strict
            # on Python 3.13+.
            client = proxy_endpoint.tls_context(((root / "ca.pem").read_bytes(),))
            client.verify_flags |= ssl.VERIFY_X509_STRICT
            with (
                checker.FixtureServer("127.0.0.1", "proxy", context) as proxy,
                socket.create_connection(
                    ("127.0.0.1", proxy.server_port), timeout=5
                ) as connection,
                client.wrap_socket(connection, server_hostname="127.0.0.1"),
            ):
                pass

    @unittest.skipUnless(
        all(shutil.which(tool) for tool in ("node", "curl", "openssl")),
        "fixture clients unavailable",
    )
    def test_real_fixture_clients_and_mapped_ipv4_bypass(self):
        if "--use-env-proxy" not in subprocess.check_output(
            ["node", "--help"], text=True
        ):
            self.skipTest("Node environment proxy support unavailable")
        checker = load_script(ROOT / "scripts/e2e-proxy-endpoints.py")
        audit = load_script(ROOT / "scripts/fixtures/proxy-endpoints-audit.py")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = checker.certificates(root, ["127.0.0.1"])
            # IPv4-mapped URLs use the real IPv4 listener, without requiring a
            # globally routed IPv6 interface on the development machine.
            with (
                checker.FixtureServer("127.0.0.1", "origin", context) as origin,
                checker.FixtureServer(
                    "127.0.0.1", "proxy", origin=("127.0.0.1", origin.server_port)
                ) as proxy,
                checker.FixtureServer("127.0.0.1", "gateway") as gateway,
                checker.FixtureServer("127.0.0.1", "canary") as canary,
            ):
                host = "::ffff:127.0.0.1"
                url = f"http://[{host}]:{gateway.server_port}" + checker.API_PATH
                env = internet_proxy.environment(
                    internet_proxy.parse(f"http://127.0.0.1:{proxy.server_port}"),
                    bypass_local_services=False,
                    bypass_hosts=(host,),
                )
                env = {
                    key: value.replace("host.docker.internal", "127.0.0.1")
                    for key, value in env.items()
                }
                env.update(
                    OPENAI_API_KEY=checker.KEY,
                    CURL_CA_BUNDLE=str(root / "ca.pem"),
                    NODE_EXTRA_CA_CERTS=str(root / "ca.pem"),
                )
                (root / "audit.json").write_text(
                    json.dumps(
                        {
                            "canary_host": "127.0.0.1",
                            "canary_port": canary.server_port,
                            "gateway_url": url,
                        }
                    )
                )
                original = socket.create_connection

                def connect(address, *args, **kwargs):
                    if address == ("127.0.0.1", canary.server_port):
                        raise OSError(
                            errno.EHOSTUNREACH, "unit-test firewall substitute"
                        )
                    return original(address, *args, **kwargs)

                # Only the firewall denial is substituted. All HTTP, TLS,
                # CONNECT, URL parsing, and proxy decisions use real clients.
                with (
                    chdir(root),
                    patch.dict(os.environ, env),
                    patch("socket.create_connection", side_effect=connect),
                ):
                    self.assertEqual(
                        agent_proxy.discover_models(url, checker.KEY), [checker.MODEL]
                    )
                    # Record the same literal path the final sandbox probe uses.
                    subprocess.run(
                        ["curl", "--globoff", "--noproxy", "*", "--silent", url],
                        check=True,
                        capture_output=True,
                    )
                    audit.main()
                checker.validate_observations(gateway.observations, proxy.observations)
                # Exercise the actual TLS proxy fixture without changing host
                # trust. The real sandbox gets these CAs through --ca-cert.
                with checker.FixtureServer(
                    "127.0.0.1", "proxy", context, ("127.0.0.1", origin.server_port)
                ) as tls_proxy:
                    for scheme in ("http", "https"):
                        result = subprocess.run(
                            [
                                "curl",
                                "--fail",
                                "--silent",
                                "--show-error",
                                "--max-time",
                                "5",
                                "--noproxy",
                                "",
                                "--proxy",
                                f"https://127.0.0.1:{tls_proxy.server_port}",
                                "--proxy-cacert",
                                str(root / "ca.pem"),
                                "--cacert",
                                str(root / "ca.pem"),
                                f"{scheme}://origin.test/fixture",
                            ],
                            capture_output=True,
                            text=True,
                            check=True,
                        )
                        self.assertEqual(
                            json.loads(result.stdout)["data"][0]["id"], checker.MODEL
                        )
