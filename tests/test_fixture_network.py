import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from unittest import TestCase
from unittest.mock import patch

from project_sandbox import fixture_network


class FixtureNetworkTests(TestCase):
    def test_uses_os_selected_source_address(self):
        with patch.object(fixture_network.socket, "socket") as factory:
            probe = factory.return_value.__enter__.return_value
            probe.getsockname.return_value = ("192.168.1.20", 12345)
            self.assertEqual(fixture_network.default_host(), "192.168.1.20")
            probe.connect.assert_called_once_with(("192.0.2.1", 9))
            probe.send.assert_not_called()
            probe.sendto.assert_not_called()

    def test_detection_failure_explains_override(self):
        for address in ("127.0.0.1", "0.0.0.0", "224.0.0.1"):
            with (
                self.subTest(address=address),
                patch.object(fixture_network.socket, "socket") as factory,
            ):
                factory.return_value.__enter__.return_value.getsockname.return_value = (
                    address,
                    12345,
                )
                with self.assertRaisesRegex(ValueError, "--proxy-fixture-host"):
                    fixture_network.default_host()
        with patch.object(fixture_network.socket, "socket", side_effect=OSError):
            with self.assertRaisesRegex(ValueError, "--fixture-host"):
                fixture_network.default_host()


def completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


class RuntimeHostTests(TestCase):
    def test_macos_vm_runtimes_listen_on_loopback_behind_their_alias(self):
        for runtime, alias, address in (
            ("docker", "host.docker.internal", "192.168.65.254"),
            ("podman", "host.containers.internal", "192.168.127.254"),
        ):
            with self.subTest(runtime=runtime):
                calls = []

                def run(argv, calls=calls, answer=f"{address} STREAM {alias}\n", **_):
                    calls.append(argv)
                    return completed(answer)

                host = fixture_network.runtime_host(
                    runtime, "python:3.12-slim", platform="darwin", run=run
                )
                self.assertEqual(host.address, address)
                self.assertEqual(host.bind, "127.0.0.1")
                self.assertTrue(host.via_alias)
                self.assertEqual(
                    calls,
                    [
                        [
                            runtime,
                            "run",
                            "--rm",
                            "python:3.12-slim",
                            "getent",
                            "ahostsv4",
                            alias,
                        ]
                    ],
                )

    def test_other_platforms_keep_the_outbound_source_address(self):
        def run(*_args, **_kwargs):
            raise AssertionError("no container needed")

        for runtime, platform in (
            ("docker", "linux"),
            ("podman", "linux"),
            # Its remote-proxy relay runs on the host and needs a host route.
            ("apple-container", "darwin"),
        ):
            with (
                self.subTest(runtime=runtime, platform=platform),
                patch.object(fixture_network, "default_host", return_value="10.0.0.5"),
            ):
                host = fixture_network.runtime_host(
                    runtime, "python:3.12-slim", platform=platform, run=run
                )
                self.assertEqual((host.address, host.bind), ("10.0.0.5", "10.0.0.5"))
                self.assertFalse(host.via_alias)

    def test_unusable_alias_answers_explain_the_override(self):
        for result in (
            completed("", returncode=2, stderr="no such host"),
            completed(""),
            completed("127.0.0.1 STREAM host.docker.internal\n"),
            completed("0.0.0.0 STREAM host.docker.internal\n"),
            completed("not-an-address STREAM host.docker.internal\n"),
        ):
            with self.subTest(result=result):
                with self.assertRaisesRegex(ValueError, "--fixture-host"):
                    fixture_network.alias_address(
                        "docker", "python:3.12-slim", run=lambda *a, r=result, **k: r
                    )

    def test_runtime_failures_explain_the_override(self):
        for error in (OSError("no docker"), subprocess.TimeoutExpired("docker", 1)):
            with self.subTest(error=error):

                def run(*_args, error=error, **_kwargs):
                    raise error

                with self.assertRaisesRegex(ValueError, "--fixture-host"):
                    fixture_network.alias_address("docker", "img", run=run)


# Shape of ``container inspect`` output: networks are nested in ``status``.
INSPECT = (
    '[{"configuration": {"id": "x"}, "status": {"state": "running", "networks": '
    '[{"ipv4Address": "192.168.64.236/24", "ipv4Gateway": "192.168.64.1"}]}}]'
)
FLAT_INSPECT = (
    '[{"networks": [{"ipv4Address": "192.168.64.236/24"}], "status": "running"}]'
)


class AppleForwarderTests(TestCase):
    # Patched in per test (create=True) when inspect answers vary per call.
    inspect_answers: Iterator[str]

    def forwarder(self, *, inspect=INSPECT, connect=None, started=None):
        calls = []

        def run(argv, **_kwargs):
            calls.append(argv)
            if argv[1] == "run":
                return started or completed("[6/6] Starting container\nname\n")
            if argv[1] == "inspect":
                if inspect is None:
                    return completed(next(self.inspect_answers))
                return completed(inspect)
            return completed("forwarder log")

        connected = []

        def default_connect(address, timeout):
            connected.append(address)
            return socket.socket()

        clock = iter(range(1000))
        return (
            fixture_network.apple_forwarder(
                "python:3.12-slim",
                (4001, 4002),
                run=run,
                connect=connect or default_connect,
                timeout=3,
                clock=lambda: next(clock),
                sleep=lambda _seconds: None,
            ),
            calls,
            connected,
        )

    def test_relays_ports_to_the_apple_loopback_redirect_and_cleans_up(self):
        manager, calls, connected = self.forwarder()
        with manager as host:
            self.assertEqual((host.address, host.bind), ("192.168.64.236", "127.0.0.1"))
            self.assertTrue(host.via_alias)
            # Every relayed port is listening before fixtures are started.
            self.assertEqual(
                connected, [("192.168.64.236", 4001), ("192.168.64.236", 4002)]
            )
            self.assertEqual(len(calls), 2)
        run, name = calls[0], calls[0][calls[0].index("--name") + 1]
        self.assertTrue(name.startswith(fixture_network.FORWARDER_PREFIX))
        self.assertEqual(run[:3], ["container", "run", "--detach"])
        self.assertIn("io.project-sandbox.e2e-fixture=forwarder", run)
        self.assertEqual(
            run[run.index("python:3.12-slim") :],
            [
                "python:3.12-slim",
                "python3",
                "-c",
                fixture_network.FORWARDER_SCRIPT,
                "0.0.0.0",
                "203.0.113.113",
                "4001:4001",
                "4002:4002",
            ],
        )
        self.assertEqual(calls[1], ["container", "inspect", name])
        self.assertEqual(calls[-1], ["container", "delete", "--force", name])

    def test_reads_nested_and_flat_inspect_layouts(self):
        for inspect in (INSPECT, FLAT_INSPECT):
            with self.subTest(inspect=inspect):
                self.assertEqual(
                    fixture_network._container_address(inspect), "192.168.64.236"
                )

    def test_waits_for_the_network_to_attach(self):
        # Regression: inspect right after a detached run lists no address yet.
        answers = iter(['[{"networks": []}]', "[]", INSPECT])
        manager, calls, _ = self.forwarder(inspect=None)
        with patch.object(self, "inspect_answers", answers, create=True):
            with manager as host:
                self.assertEqual(host.address, "192.168.64.236")
        self.assertEqual(sum(argv[1] == "inspect" for argv in calls), 3)

    def test_failures_explain_the_override_and_remove_the_container(self):
        def refused(_address, timeout):
            raise ConnectionRefusedError("refused")

        for kwargs, expected in (
            ({"inspect": "[]"}, "address of fixture forwarder"),
            ({"inspect": '[{"networks": []}]'}, "address of fixture forwarder"),
            ({"connect": refused}, "did not listen on port 4001.*forwarder log"),
        ):
            with self.subTest(expected=expected):
                manager, calls, _ = self.forwarder(**kwargs)
                with self.assertRaisesRegex(ValueError, expected + ".*--fixture-host"):
                    with manager:
                        self.fail("forwarder must not be yielded")
                self.assertEqual(calls[-1][:3], ["container", "delete", "--force"])

    def test_start_failure_explains_the_override(self):
        manager, calls, _ = self.forwarder(
            started=completed(returncode=1, stderr="image not found")
        )
        with self.assertRaisesRegex(ValueError, "image not found.*--fixture-host"):
            with manager:
                self.fail("forwarder must not be yielded")
        self.assertEqual(len(calls), 1)

    def test_loopback_ports_are_distinct_and_free(self):
        ports = fixture_network.loopback_ports(3)
        self.assertEqual(len(set(ports)), 3)
        for port in ports:
            socket.create_server(("127.0.0.1", port)).close()

    def test_forwarder_script_relays_both_directions_with_half_close(self):
        # The same script runs in the container; here it relays host loopback.
        with socket.create_server(("127.0.0.1", 0)) as upstream:
            target = upstream.getsockname()[1]
            (listen,) = fixture_network.loopback_ports(1)
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    fixture_network.FORWARDER_SCRIPT,
                    "127.0.0.1",
                    "127.0.0.1",
                    f"{listen}:{target}",
                ],
                stdout=subprocess.PIPE,
                text=True,
            )
            assert process.stdout is not None
            try:
                self.assertEqual(process.stdout.readline().strip(), "ready")

                def echo():
                    connection, _ = upstream.accept()
                    with connection:
                        request = b""
                        while chunk := connection.recv(1024):
                            request += chunk
                        connection.sendall(b"reply:" + request)

                thread = threading.Thread(target=echo, daemon=True)
                thread.start()
                with socket.create_connection(
                    ("127.0.0.1", listen), timeout=5
                ) as client:
                    client.sendall(b"request")
                    client.shutdown(socket.SHUT_WR)
                    reply = b""
                    while chunk := client.recv(1024):
                        reply += chunk
                thread.join(timeout=5)
                self.assertEqual(reply, b"reply:request")
            finally:
                process.terminate()
                self.assertEqual(process.wait(timeout=5), 0)
