import subprocess
import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import local_service_network as ollama_network
from project_sandbox.container_cli import APPLE_CONTAINER, CHROOT, DOCKER, PODMAN


class OllamaNetworkTests(TestCase):
    def test_socat_start_failure_names_the_requested_service(self) -> None:
        plan = ollama_network.ForwardingPlan(
            "linux-bridge-socat",
            endpoint="172.17.0.1",
            port=18080,
            label="Internet proxy",
        )
        with (
            patch.object(ollama_network.shutil, "which", return_value="/usr/bin/socat"),
            patch.object(
                ollama_network.subprocess,
                "Popen",
                side_effect=OSError("cannot execute"),
            ),
            self.assertRaisesRegex(SystemExit, "Internet proxy socat proxy"),
        ):
            plan.start()

    def test_linux_socat_preserves_ipv6_loopback_upstream(self) -> None:
        process = MagicMock()
        process.poll.return_value = None
        plan = ollama_network.ForwardingPlan(
            "linux-bridge-socat",
            endpoint="172.17.0.1",
            port=18080,
            label="Internet proxy",
            loopback_host="::1",
        )
        with (
            patch.object(ollama_network.shutil, "which", return_value="/usr/bin/socat"),
            patch.object(
                ollama_network.subprocess, "Popen", return_value=process
            ) as popen,
            patch.object(ollama_network.time, "sleep"),
        ):
            plan.start()
        self.assertEqual(popen.call_args.args[0][-1], "TCP6:[::1]:18080")

    def test_multiple_services_share_mapping_and_reject_duplicate_ports(self) -> None:
        plans = ollama_network.prepare_services(
            CHROOT,
            [
                ollama_network.LocalService("Internet proxy", 18080),
                ollama_network.LocalService("Ollama", 11434),
            ],
        )
        self.assertEqual(sum(plan.add_host is not None for plan in plans), 1)
        with self.assertRaisesRegex(SystemExit, "Duplicate local-service port"):
            ollama_network.prepare_services(
                CHROOT,
                [
                    ollama_network.LocalService("one", 18080),
                    ollama_network.LocalService("two", 18080),
                ],
            )

    def test_chroot_uses_shared_loopback_without_runtime_inspection(self) -> None:
        with patch.object(ollama_network, "_runtime_info") as runtime_info:
            plan = ollama_network.prepare(CHROOT)
        self.assertEqual(plan.strategy, "chroot-shared-loopback")
        self.assertEqual(plan.endpoint, "127.0.0.1")
        self.assertEqual(
            plan.add_host,
            "host.docker.internal:127.0.0.1",
        )
        runtime_info.assert_not_called()

    def test_apple_uses_configured_host_alias(self) -> None:
        plan = ollama_network.prepare(APPLE_CONTAINER)
        self.assertEqual(plan.strategy, "apple-configured-host-alias")
        self.assertEqual(plan.hostname, "host.docker.internal")

    def test_apple_setup_notice_includes_dns_and_restart_warning(self) -> None:
        notice = ollama_network.apple_setup_notice("Ollama")
        self.assertIn(
            "sudo container system dns create host.docker.internal --localhost 203.0.113.113",
            notice,
        )
        self.assertIn("might disable network connectivity", notice)
        self.assertIn("container system stop && container system start", notice)

    def test_rootless_podman_uses_native_alias(self) -> None:
        with patch.object(
            ollama_network,
            "_runtime_info",
            return_value={"host": {"security": {"rootless": True}}},
        ):
            plan = ollama_network.prepare(PODMAN)
        self.assertEqual(plan.strategy, "podman-native-host-alias")
        self.assertEqual(
            plan.add_host,
            "host.docker.internal:host-gateway",
        )

    def test_docker_desktop_uses_native_alias(self) -> None:
        with patch.object(
            ollama_network,
            "_runtime_info",
            return_value={"OperatingSystem": "Docker Desktop"},
        ):
            plan = ollama_network.prepare(DOCKER)
        self.assertEqual(plan.strategy, "docker-desktop-host-alias")

    def test_linux_bridge_rejects_public_gateway(self) -> None:
        with (
            patch.object(ollama_network, "_runtime_info", return_value={}),
            patch.object(ollama_network, "_bridge_gateway", return_value="8.8.8.8"),
        ):
            with self.assertRaisesRegex(SystemExit, "Unsafe"):
                ollama_network.prepare(DOCKER)

    def test_linux_bridge_plan_uses_exact_address(self) -> None:
        with (
            patch.object(ollama_network, "_runtime_info", return_value={}),
            patch.object(ollama_network, "_bridge_gateway", return_value="172.17.0.1"),
            patch.object(ollama_network, "_validate_bindable"),
        ):
            plan = ollama_network.prepare(DOCKER)
        self.assertEqual(plan.strategy, "linux-bridge-socat")
        self.assertEqual(plan.endpoint, "172.17.0.1")
        self.assertNotIn("0.0.0.0", plan.add_host or "")

    def test_bridge_proxy_requires_socat(self) -> None:
        plan = ollama_network.ForwardingPlan(
            "linux-bridge-socat", endpoint="172.17.0.1"
        )
        with patch.object(ollama_network.shutil, "which", return_value=None):
            with self.assertRaisesRegex(SystemExit, "requires socat"):
                plan.start()

    def test_bridge_proxy_argv_and_cleanup(self) -> None:
        proc = MagicMock()
        proc.poll.return_value = None
        with (
            patch.object(ollama_network.shutil, "which", return_value="/usr/bin/socat"),
            patch.object(
                ollama_network.subprocess, "Popen", return_value=proc
            ) as popen,
            patch.object(ollama_network.time, "sleep"),
        ):
            plan = ollama_network.ForwardingPlan(
                "linux-bridge-socat", endpoint="172.17.0.1"
            )
            plan.start()
            plan.close()
        argv = popen.call_args.args[0]
        self.assertIn("bind=172.17.0.1", argv[1])
        self.assertNotIn("0.0.0.0", " ".join(argv))
        proc.terminate.assert_called_once()
        proc.wait.assert_called()

    def test_immediate_proxy_failure_is_reported(self) -> None:
        proc = MagicMock()
        proc.poll.return_value = 1
        proc.stderr.read.return_value = "Address already in use"
        with (
            patch.object(ollama_network.shutil, "which", return_value="socat"),
            patch.object(ollama_network.subprocess, "Popen", return_value=proc),
            patch.object(ollama_network.time, "sleep"),
        ):
            plan = ollama_network.ForwardingPlan(
                "linux-bridge-socat", endpoint="172.17.0.1"
            )
            with self.assertRaisesRegex(SystemExit, "Address already in use"):
                plan.start()
            plan.close()
        proc.wait.assert_called_once_with(timeout=5)
        proc.terminate.assert_not_called()

    def test_cleanup_reaps_proxy_that_exited_independently(self) -> None:
        proc = MagicMock()
        proc.poll.return_value = 1
        plan = ollama_network.ForwardingPlan("linux-bridge-socat", proxy=proc)

        plan.close()

        proc.wait.assert_called_once_with(timeout=5)
        proc.terminate.assert_not_called()

    def test_context_cleanup_runs_when_session_raises(self) -> None:
        plan = ollama_network.ForwardingPlan("podman-native-host-alias")
        with patch.object(plan, "close") as close:
            with self.assertRaisesRegex(RuntimeError, "session failed"):
                with plan:
                    raise RuntimeError("session failed")
        close.assert_called_once()

    def test_occupied_bridge_port_is_reported(self) -> None:
        probe = MagicMock()
        probe.bind.side_effect = OSError("Address already in use")
        with patch.object(ollama_network.socket, "socket", return_value=probe):
            with self.assertRaisesRegex(SystemExit, "Address already in use"):
                ollama_network._validate_bindable("172.17.0.1")
        probe.close.assert_called_once()

    def test_dry_run_does_not_call_runtime(self) -> None:
        with patch.object(subprocess, "run") as run:
            plan = ollama_network.prepare(DOCKER, dry_run=True)
        self.assertEqual(plan.strategy, "runtime-probe-required")
        run.assert_not_called()
