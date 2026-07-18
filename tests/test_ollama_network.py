import socket
import subprocess
import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import ollama_network
from project_sandbox.container_cli import APPLE_CONTAINER, CHROOT, DOCKER, PODMAN


class OllamaNetworkTests(TestCase):
    def test_chroot_uses_shared_loopback_without_runtime_inspection(self) -> None:
        with patch.object(ollama_network, "_runtime_info") as runtime_info:
            plan = ollama_network.prepare(CHROOT)
        self.assertEqual(plan.strategy, "chroot-shared-loopback")
        self.assertEqual(plan.endpoint, "127.0.0.1")
        self.assertEqual(
            plan.add_host,
            "ollama.project-sandbox.internal:127.0.0.1",
        )
        runtime_info.assert_not_called()

    def test_apple_requires_preconfigured_dns_without_sudo(self) -> None:
        with patch.object(socket, "gethostbyname", side_effect=socket.gaierror):
            with self.assertRaisesRegex(SystemExit, "sudo container system dns create"):
                ollama_network.prepare(APPLE_CONTAINER)

    def test_apple_accepts_documentation_address(self) -> None:
        with patch.object(socket, "gethostbyname", return_value="203.0.113.113"):
            plan = ollama_network.prepare(APPLE_CONTAINER)
        self.assertEqual(plan.strategy, "apple-preconfigured-localhost-dns")
        self.assertEqual(plan.endpoint, "203.0.113.113")

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
            "ollama.project-sandbox.internal:host-gateway",
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
