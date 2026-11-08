import os
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from forwarder_test_support import (
    BIND,
    Echo,
    requires_second_loopback,
    roundtrip,
    wait_for,
)

from project_sandbox import egress_relay, local_forwarder
from project_sandbox import local_service_network as ollama_network
from project_sandbox.container_cli import APPLE_CONTAINER, CHROOT, DOCKER, PODMAN


class OllamaNetworkTests(TestCase):
    def setUp(self) -> None:
        # Forwarder state lookups (read-only in prepare) stay out of $HOME.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_home = Path(tmp.name)
        env = patch.dict(os.environ, {"XDG_STATE_HOME": tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def test_bridge_plan_attaches_and_releases_a_lease_without_socat(self) -> None:
        lease = MagicMock()
        plan = ollama_network.ForwardingPlan(
            "linux-bridge-forwarder",
            endpoint="172.17.0.1",
            port=18080,
            label="Internet proxy",
            loopback_host="::1",
        )
        with (
            patch.object(local_forwarder, "attach", return_value=lease) as attach,
            patch.object(ollama_network.subprocess, "Popen") as popen,
        ):
            plan.start()
            plan.start()  # idempotent: one lease per plan
            plan.close()
            plan.close()
        attach.assert_called_once_with(
            "172.17.0.1", 18080, target_host="::1", label="Internet proxy"
        )
        popen.assert_not_called()  # no socat (or any other) child process
        lease.release.assert_called_once()
        self.assertIsNone(plan.lease)

    def test_bridge_attach_failure_propagates_without_a_lease(self) -> None:
        plan = ollama_network.ForwardingPlan(
            "linux-bridge-forwarder", endpoint="172.17.0.1", label="Agent proxy"
        )
        error = local_forwarder.ForwarderError("Cannot bind the Agent proxy proxy")
        with (
            patch.object(local_forwarder, "attach", side_effect=error),
            self.assertRaisesRegex(SystemExit, "Agent proxy"),
        ):
            plan.start()
        self.assertIsNone(plan.lease)
        plan.close()

    def test_no_parent_death_hook_remains(self) -> None:
        # The shared forwarder must outlive the session that spawned it.
        self.assertFalse(hasattr(ollama_network, "_die_with_parent_hook"))

    def test_multiple_services_share_mapping_and_reject_duplicate_ports(self) -> None:
        with patch.object(
            ollama_network, "prepare", wraps=ollama_network.prepare
        ) as prepare:
            plans = ollama_network.prepare_services(
                CHROOT,
                [
                    ollama_network.LocalService("Internet proxy", 18080),
                    ollama_network.LocalService("Ollama", 11434),
                ],
            )
        self.assertEqual(sum(plan.add_host is not None for plan in plans), 1)
        prepare.assert_called_once()
        with (
            patch.object(ollama_network, "prepare") as prepare,
            self.assertRaisesRegex(SystemExit, "Duplicate local-service port"),
        ):
            ollama_network.prepare_services(
                DOCKER,
                [
                    ollama_network.LocalService("one", 18080),
                    ollama_network.LocalService("two", 18080),
                ],
            )
        prepare.assert_not_called()

    def test_linux_services_share_one_discovered_bridge_endpoint(self) -> None:
        with (
            patch.object(ollama_network, "_runtime_info", return_value={}) as info,
            patch.object(
                ollama_network, "_bridge_gateway", return_value="172.17.0.1"
            ) as gateway,
            patch.object(ollama_network, "_validate_bindable") as bindable,
        ):
            plans = ollama_network.prepare_services(
                DOCKER,
                [
                    ollama_network.LocalService("Internet proxy", 18080),
                    ollama_network.LocalService("Agent proxy", 4000),
                    ollama_network.LocalService("Ollama", 11434),
                ],
            )

        info.assert_called_once()
        gateway.assert_called_once()
        self.assertEqual(bindable.call_count, 3)
        self.assertEqual({plan.endpoint for plan in plans}, {"172.17.0.1"})
        self.assertEqual(sum(plan.add_host is not None for plan in plans), 1)

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
        self.assertIn("can disrupt container Internet access", notice)
        self.assertIn("container system stop && container system start", notice)

    def test_apple_setup_notice_includes_stale_alias_recovery(self) -> None:
        # A registered alias can resolve without redirecting; re-running the
        # create command is a no-op, so the notice must name the delete step.
        notice = ollama_network.apple_setup_notice("Ollama")
        self.assertIn("sudo container system dns delete host.docker.internal", notice)
        self.assertIn("time out instead of being refused", notice)

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

    def _pasta_info(self) -> dict:
        return {
            "host": {"security": {"rootless": True}, "rootlessNetworkCmd": "pasta"},
            "version": {"Version": "5.4.2"},
        }

    def test_rootless_pasta_podman_forwards_planned_ports_to_loopback(self) -> None:
        # Regression: pasta copies the host address into the container and its
        # host-gateway reaches the LAN address, so loopback-only Ollama and
        # agent gateways were unreachable through host.docker.internal.
        with (
            patch.object(ollama_network.sys, "platform", "linux"),
            patch.object(
                ollama_network, "_runtime_info", return_value=self._pasta_info()
            ),
        ):
            plans = ollama_network.prepare_services(
                PODMAN,
                [
                    ollama_network.LocalService("Agent proxy", 4000),
                    ollama_network.LocalService("Ollama", 11434),
                ],
            )
        self.assertEqual(
            [plan.strategy for plan in plans], ["podman-pasta-forward"] * 2
        )
        self.assertEqual(
            [plan.add_host for plan in plans], ["host.docker.internal:127.0.0.1", None]
        )
        # Exactly the planned ports reach the host, over IPv4 only.
        self.assertEqual(
            ollama_network.network_argument(plans),
            "pasta:-a,198.18.0.2,-n,24,-g,198.18.0.1,-4,-T,4000:4000,-T,11434:11434",
        )
        for plan in plans:
            plan.start()  # no host-side forwarder process
            self.assertIsNone(plan.lease)

    def test_pasta_sessions_and_builds_get_their_own_address(self) -> None:
        # Regression: pasta copied the host's LAN address into the container,
        # so proxies on that address were unreachable from builds and sessions.
        self.assertEqual(
            ollama_network.network_argument([], pasta=True),
            "pasta:-a,198.18.0.2,-n,24,-g,198.18.0.1",
        )
        self.assertIsNone(ollama_network.network_argument([]))
        with patch.object(ollama_network, "_runtime_info") as info:
            self.assertFalse(ollama_network.uses_local_pasta(DOCKER))
        info.assert_not_called()

    def test_pasta_rejects_ipv6_loopback_services_in_any_order(self) -> None:
        services = [
            ollama_network.LocalService("Ollama", 11434),
            ollama_network.LocalService("Agent proxy", 4000, loopback_host="::1"),
        ]
        for ordered in (services, services[::-1]):
            with (
                self.subTest(first=ordered[0].label),
                patch.object(ollama_network.sys, "platform", "linux"),
                patch.object(
                    ollama_network, "_runtime_info", return_value=self._pasta_info()
                ),
                self.assertRaisesRegex(SystemExit, "Agent proxy listens on ::1"),
            ):
                ollama_network.prepare_services(PODMAN, ordered)

    def test_pasta_forwarding_requires_a_local_linux_podman(self) -> None:
        # podman machine runs pasta in a VM whose loopback is not this host's.
        with (
            patch.object(ollama_network.sys, "platform", "darwin"),
            patch.object(
                ollama_network, "_runtime_info", return_value=self._pasta_info()
            ),
        ):
            plan = ollama_network.prepare(PODMAN)
        self.assertEqual(plan.strategy, "podman-native-host-alias")
        self.assertIsNone(ollama_network.network_argument([plan]))

    def test_rootless_slirp_podman_keeps_native_alias(self) -> None:
        info = self._pasta_info()
        info["host"]["rootlessNetworkCmd"] = "slirp4netns"
        with (
            patch.object(ollama_network.sys, "platform", "linux"),
            patch.object(ollama_network, "_runtime_info", return_value=info),
        ):
            plan = ollama_network.prepare(PODMAN)
        self.assertEqual(plan.strategy, "podman-native-host-alias")

    def test_rootful_podman_never_falls_back_to_a_bridge_listener(self) -> None:
        for info in ({"host": {"security": {"rootless": False}}}, {}):
            with (
                self.subTest(info=info),
                patch.object(ollama_network, "_runtime_info", return_value=info),
                patch.object(ollama_network, "_bridge_gateway") as bridge,
                self.assertRaisesRegex(SystemExit, "rootless podman only"),
            ):
                ollama_network.prepare(PODMAN)
            bridge.assert_not_called()

    def test_old_podman_without_host_gateway_is_rejected(self) -> None:
        with patch.object(
            ollama_network,
            "_runtime_info",
            return_value={
                "host": {"security": {"rootless": True}},
                "version": {"Version": "4.3.1"},
            },
        ):
            with self.assertRaisesRegex(SystemExit, "host-gateway"):
                ollama_network.prepare(PODMAN)

    def test_new_podman_uses_native_alias(self) -> None:
        with patch.object(
            ollama_network,
            "_runtime_info",
            return_value={
                "host": {"security": {"rootless": True}},
                "version": {"Version": "5.2.1"},
            },
        ):
            plan = ollama_network.prepare(PODMAN)
        self.assertEqual(plan.strategy, "podman-native-host-alias")

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
        self.assertEqual(plan.strategy, "linux-bridge-forwarder")
        self.assertEqual(plan.endpoint, "172.17.0.1")
        self.assertNotIn("0.0.0.0", plan.add_host or "")

    def test_context_cleanup_runs_when_session_raises(self) -> None:
        plan = ollama_network.ForwardingPlan("podman-native-host-alias")
        with patch.object(plan, "close") as close:
            with self.assertRaisesRegex(RuntimeError, "session failed"):
                with plan:
                    raise RuntimeError("session failed")
        close.assert_called_once()

    @requires_second_loopback
    def test_foreign_listener_is_refused_early(self) -> None:
        with socket.create_server((BIND, 0)) as foreign:
            port = foreign.getsockname()[1]
            with self.assertRaisesRegex(
                SystemExit,
                rf"Cannot bind the Ollama proxy to {BIND}:{port}: another process",
            ):
                ollama_network._validate_bindable(BIND, port=port)
        self.assertFalse((self.state_home / "project-sandbox").exists())

    @requires_second_loopback
    def test_matching_live_forwarder_is_accepted(self) -> None:
        echo = Echo()
        self.addCleanup(echo.close)
        lease = local_forwarder.attach(BIND, echo.port, idle_poll=0.2)
        self.addCleanup(lease.release)
        ollama_network._validate_bindable(BIND, port=echo.port)
        with self.assertRaisesRegex(SystemExit, r"\[::1\]"):
            ollama_network._validate_bindable(BIND, port=echo.port, loopback_host="::1")
        lease.release()
        control = egress_relay.RelayControl(files=local_forwarder.FILES)
        directory = local_forwarder.state_dir(BIND, echo.port)
        self.assertTrue(
            wait_for(lambda: not control.is_live(directory)), "forwarder stayed"
        )

    @requires_second_loopback
    def test_two_prepared_sessions_share_one_ollama_forwarder(self) -> None:
        # Regression (share-local-service-forwarders): a second concurrent
        # session failed its bind check because the first session's socat
        # held <gateway>:11434.
        echo = Echo()
        self.addCleanup(echo.close)
        servers: list[threading.Thread] = []

        def fake_spawn(control, state_dir, **kwargs):
            # The forwarder, run in a thread instead of a detached process.
            thread = threading.Thread(
                target=local_forwarder.serve,
                args=(state_dir,),
                kwargs={
                    "bind": kwargs["bind"],
                    "port": kwargs["port"],
                    "target_host": kwargs["target_host"],
                    "target_port": kwargs["target_port"],
                    "idle_poll": 0.05,
                    "install_signals": False,
                },
                daemon=True,
            )
            thread.start()
            servers.append(thread)
            self.assertTrue(wait_for(lambda: control.port(state_dir) is not None))
            return control.port(state_dir)

        def session() -> list[ollama_network.ForwardingPlan]:
            return ollama_network.prepare_services(
                DOCKER, [ollama_network.LocalService("Ollama", echo.port)]
            )

        with (
            patch.object(ollama_network, "_runtime_info", return_value={}),
            patch.object(ollama_network, "_bridge_gateway", return_value=BIND),
            patch.object(ollama_network, "_validate_endpoint"),
            patch.object(egress_relay.RelayControl, "spawn_local_forward", fake_spawn),
        ):
            first = session()
            for plan in first:
                plan.start()
            second = session()  # prepared while the first session runs
            for plan in second:
                plan.start()
        self.assertEqual(len(servers), 1)  # one forwarder for both sessions
        self.assertEqual(
            [plan.strategy for plan in first + second],
            ["linux-bridge-forwarder"] * 2,
        )
        for plan in first:
            plan.close()  # the session that started the forwarder ends first
        self.assertEqual(roundtrip(echo.port), b"ping")
        for plan in second:
            plan.close()
        servers[0].join(5)
        self.assertFalse(servers[0].is_alive())

    def test_bind_probe_ignores_time_wait_but_not_live_listener(self) -> None:
        # Regression: a session that just closed connections on the bridge
        # port leaves TIME_WAIT sockets that socat's reuseaddr tolerates.
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        client = socket.create_connection(("127.0.0.1", port))
        accepted, _ = server.accept()
        accepted.close()
        with self.assertRaisesRegex(SystemExit, "Cannot bind"):
            ollama_network._validate_bindable("127.0.0.1", port=port)
        server.close()
        client.close()
        ollama_network._validate_bindable("127.0.0.1", port=port)

    def test_dry_run_does_not_call_runtime(self) -> None:
        with patch.object(subprocess, "run") as run:
            plan = ollama_network.prepare(DOCKER, dry_run=True)
        self.assertEqual(plan.strategy, "runtime-probe-required")
        run.assert_not_called()


class DescribeTests(TestCase):
    def test_bridge_forwarder_names_its_upstream_and_sharing(self) -> None:
        plan = ollama_network.ForwardingPlan(
            "linux-bridge-forwarder",
            endpoint="172.17.0.1",
            port=4000,
            label="Agent proxy",
            loopback_host="::1",
        )
        self.assertEqual(
            ollama_network.describe(plan),
            "Agent proxy forwarding strategy: linux-bridge-forwarder "
            "(172.17.0.1:4000 -> [::1]:4000, shared with concurrent sessions)",
        )
        self.assertEqual(
            ollama_network.describe(
                ollama_network.ForwardingPlan("docker-desktop-host-alias")
            ),
            "Ollama forwarding strategy: docker-desktop-host-alias",
        )
