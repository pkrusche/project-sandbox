import subprocess
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
