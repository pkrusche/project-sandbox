"""The Colima private-network sidecar strategy: no published port, two networks."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_egress_sidecar import IMAGE, Base, FakeRuntime, _done, _policy

from project_sandbox import colima_network as net
from project_sandbox import egress_sidecar
from project_sandbox.container_cli import colima_runtime
from project_sandbox.egress_sidecar import (
    CONTAINER,
    DOCKER_PRIVATE_NETWORK,
    SidecarError,
    Strategy,
)

RUNTIME = colima_runtime("work")
PRIVATE = Strategy(DOCKER_PRIVATE_NETWORK, net.ALIAS)


class ColimaFake(FakeRuntime):
    """FakeRuntime plus the profile's two networks and the sidecar's attachments."""

    def __init__(self, runtime=RUNTIME, **kwargs) -> None:
        super().__init__(runtime, **kwargs)
        self.networks: dict[str, dict] = {}
        self.attached = False
        self.probe_ok = True
        self.sandbox_ip = "172.19.0.2"

    def kind(self, argv) -> str:
        # Drop `--context <name>` so the parent's argv classification applies.
        argv = list(argv)
        if argv[1:2] == ["--context"]:
            argv = [argv[0], *argv[3:]]
        return super().kind(argv)

    def _inspect(self) -> str:
        networks = {net.UPLINK_NETWORK: {"IPAddress": "172.18.0.2", "Aliases": []}}
        if self.attached:
            networks[net.SANDBOX_NETWORK] = {
                "IPAddress": self.sandbox_ip,
                "Aliases": [net.ALIAS, "abc123"],
            }
        return json.dumps(
            [
                {
                    "State": {"Running": self.container == "running"},
                    "Config": {"Labels": self.labels},
                    "NetworkSettings": {"Networks": networks},
                }
            ]
        )

    def __call__(self, argv):
        argv = list(argv)
        if "--entrypoint" in argv:  # the sandbox-network probe
            self.calls.append(argv)
            return _done(argv, 0 if self.probe_ok else 1)
        kind = self.kind(argv)
        if kind == "network-inspect":
            self.calls.append(argv)
            name = argv[argv.index("inspect") + 1]
            if name not in self.networks:
                return _done(argv, 1, err="No such network")
            return _done(argv, out=json.dumps(self.networks[name]))
        if kind == "network-create":
            self.calls.append(argv)
            labels = dict(
                a.split("=", 1) for i, a in enumerate(argv) if argv[i - 1] == "--label"
            )
            self.networks[argv[-1]] = {
                "Driver": "bridge",
                "Internal": "--internal" in argv,
                "Labels": labels,
            }
            return _done(argv, out="id\n")
        if kind == "network-connect":
            self.calls.append(argv)
            self.attached = True
            return _done(argv)
        if kind in ("run", "rm"):
            self.attached = False  # a (re)created container has no attachments
        return super().__call__(argv)


class PrivateNetworkBase(Base):
    runtime = RUNTIME
    fake: ColimaFake

    def setUp(self) -> None:
        super().setUp()
        self.fake = ColimaFake()
        self.strategy = PRIVATE

    def deps(self, **overrides):
        def no_host(*_a):
            raise AssertionError("a published-port check ran on the private network")

        self.mount_checks: list[list[Path]] = []

        def mounts_visible(_runtime, paths):
            self.mount_checks.append(list(paths))

        values = {
            "port_free": no_host,
            "host_probe": no_host,
            "mount_check": mounts_visible,
            **overrides,
        }
        return super().deps(**values)

    def runs(self) -> list[list[str]]:
        return [c for c in self.fake.calls if self.fake.kind(c) == "run" and "-d" in c]


class StrategySelectionTests(PrivateNetworkBase):
    def test_colima_selects_the_private_network_strategy(self) -> None:
        strategy = egress_sidecar.endpoint_strategy(RUNTIME)
        self.assertEqual(strategy, PRIVATE)
        self.assertIn("no published port", strategy.describe())

    def test_endpoint_is_the_fixed_alias_and_port(self) -> None:
        self.assertEqual(egress_sidecar.endpoint(3129, net.ALIAS), "egress:3128")
        self.assertEqual(egress_sidecar.endpoint(3129), "127.0.0.1:3129")
        self.assertEqual(
            egress_sidecar.describe_address(net.ALIAS), "the private in-VM network"
        )

    def test_recorded_strategy_round_trips(self) -> None:
        state = {"strategy": DOCKER_PRIVATE_NETWORK, "address": net.ALIAS}
        self.assertEqual(egress_sidecar.recorded_strategy(state), PRIVATE)

    def test_no_sandbox_forwarding_plan_is_needed(self) -> None:
        self.assertIsNone(egress_sidecar.sandbox_plan(PRIVATE, 3129))


class RunArgvTests(PrivateNetworkBase):
    def argv(self) -> list[str]:
        return egress_sidecar.sidecar_run_argv(
            RUNTIME,
            image=IMAGE,
            sidecar_dir=Path("/state/sidecar"),
            port=3129,
            memory="256M",
            cpus=1,
            restart_fingerprint="feedface",
            strategy=PRIVATE,
        )

    def test_publishes_nothing_and_starts_on_the_uplink_only(self) -> None:
        argv = self.argv()
        self.assertEqual(
            argv[:6], ["docker", "--context", "colima-work", "run", "-d", "--name"]
        )
        for forbidden in ("--publish", "-p", "--add-host"):
            self.assertNotIn(forbidden, argv)
        self.assertEqual(argv[argv.index("--network") + 1], net.UPLINK_NETWORK)
        self.assertNotIn(net.SANDBOX_NETWORK, argv)

    def test_the_sidecar_does_not_forward_between_its_networks(self) -> None:
        argv = self.argv()
        self.assertEqual(argv[argv.index("--sysctl") + 1], "net.ipv4.ip_forward=0")


class EnsureTests(PrivateNetworkBase):
    def test_cold_start_attaches_the_alias_before_readiness(self) -> None:
        sidecar = self.ensure()
        kinds = self.fake.kinds()
        self.assertTrue(sidecar.started)
        self.assertEqual(kinds.count("network-create"), 2)
        self.assertLess(kinds.index("network-create"), kinds.index("run"))
        self.assertLess(kinds.index("run"), kinds.index("network-connect"))
        self.assertLess(kinds.index("network-connect"), kinds.index("probe"))
        self.assertNotIn("--publish", self.runs()[0])
        self.assertEqual(sidecar.endpoint, "egress:3128")
        self.assertEqual(sidecar.strategy, PRIVATE)

    def test_connect_names_alias_network_and_container(self) -> None:
        self.ensure()
        connect = next(
            c for c in self.fake.calls if self.fake.kind(c) == "network-connect"
        )
        self.assertEqual(
            connect[3:],
            [
                "network",
                "connect",
                "--alias",
                net.ALIAS,
                net.SANDBOX_NETWORK,
                CONTAINER,
            ],
        )

    def test_the_private_path_is_checked_from_the_sandbox_network(self) -> None:
        self.ensure()
        probe = next(c for c in self.fake.calls if "--entrypoint" in c)
        self.assertIn(net.SANDBOX_NETWORK, probe)
        self.assertNotIn(net.UPLINK_NETWORK, probe)
        self.assertEqual(probe[probe.index("--entrypoint") + 1], "nc")
        self.assertEqual(probe[-2:], [net.ALIAS, str(net.PROXY_PORT)])

    def test_a_healthy_attached_sidecar_is_reused(self) -> None:
        self.ensure()
        second = self.ensure()
        self.assertFalse(second.started)
        self.assertEqual(len(self.runs()), 1)
        self.assertEqual(self.fake.kinds().count("network-create"), 2)

    def test_a_detached_sidecar_is_recreated_not_trusted(self) -> None:
        self.ensure()
        self.fake.attached = False  # e.g. `docker network disconnect`
        again = self.ensure()
        self.assertTrue(again.started)
        self.assertEqual(len(self.runs()), 2)
        self.assertTrue(self.fake.attached)

    def test_an_unreachable_alias_fails_the_start(self) -> None:
        self.fake.probe_ok = False
        with self.assertRaises(SidecarError) as ctx:
            self.ensure()
        self.assertIn("as egress on the psbx-sandbox network", str(ctx.exception))

    def test_a_foreign_network_stops_the_start_before_any_container(self) -> None:
        self.fake.networks[net.SANDBOX_NETWORK] = {
            "Driver": "bridge",
            "Internal": True,
            "Labels": {},
        }
        with self.assertRaises(SidecarError) as ctx:
            self.ensure()
        self.assertIn(net.SANDBOX_NETWORK, str(ctx.exception))
        self.assertEqual(self.runs(), [])
        self.assertNotIn("rm", self.fake.kinds())

    def test_a_loopback_parent_proxy_is_unsupported(self) -> None:
        policy = _policy(self.tmp, '[egress.upstream]\nurl = "http://127.0.0.1:3128"\n')
        with self.assertRaises(SidecarError) as ctx:
            self.ensure(policy)
        self.assertIn("not supported on Colima", str(ctx.exception))
        self.assertEqual(self.runs(), [])

    def test_a_network_reachable_parent_proxy_is_accepted(self) -> None:
        policy = _policy(
            self.tmp, '[egress.upstream]\nurl = "http://proxy.corp.example:3128"\n'
        )
        self.assertTrue(self.ensure(policy).started)


class SandboxAddressTests(PrivateNetworkBase):
    def test_reads_the_address_fresh_from_the_attached_sidecar(self) -> None:
        self.ensure()
        self.assertEqual(
            egress_sidecar.sandbox_address(RUNTIME, self.fake), "172.19.0.2"
        )
        self.fake.sandbox_ip = "172.19.0.9"  # recreated with another address
        self.assertEqual(
            egress_sidecar.sandbox_address(RUNTIME, self.fake), "172.19.0.9"
        )

    def test_a_detached_or_missing_sidecar_names_the_repair_command(self) -> None:
        with self.assertRaises(SidecarError) as ctx:
            egress_sidecar.sandbox_address(RUNTIME, self.fake)  # no container
        self.assertIn("project-sandbox-egress restart", str(ctx.exception))
        self.ensure()
        self.fake.attached = False
        with self.assertRaises(SidecarError):
            egress_sidecar.sandbox_address(RUNTIME, self.fake)


class DownTests(PrivateNetworkBase):
    def test_down_removes_the_container_but_keeps_both_networks(self) -> None:
        self.ensure()
        self.assertTrue(egress_sidecar.down(RUNTIME, root=self.root, deps=self.deps()))
        removed = [c[-1] for c in self.fake.calls if self.fake.kind(c) == "network-rm"]
        self.assertNotIn(net.UPLINK_NETWORK, removed)
        self.assertNotIn(net.SANDBOX_NETWORK, removed)
        self.assertEqual(
            set(self.fake.networks), {net.UPLINK_NETWORK, net.SANDBOX_NETWORK}
        )


class ProfileScopeTests(PrivateNetworkBase):
    other = colima_runtime("other")

    def test_each_profile_has_its_own_state_directory(self) -> None:
        a = egress_sidecar.layout(RUNTIME, self.root)
        b = egress_sidecar.layout(self.other, self.root)
        self.assertEqual(a.root.name, "colima-work")
        self.assertEqual(b.root.name, "colima-other")
        self.assertNotEqual(a.root, b.root)
        # Other runtimes keep their plain name, so existing state stays put.
        from project_sandbox.container_cli import DOCKER

        self.assertEqual(egress_sidecar.layout(DOCKER, self.root).root.name, "docker")

    def test_down_on_one_profile_leaves_the_other_profiles_state(self) -> None:
        self.ensure()
        fake_other = ColimaFake(self.other)
        deps_other = self.deps(runner=fake_other)
        egress_sidecar.ensure(
            self.other, _policy(self.tmp), image=IMAGE, root=self.root, deps=deps_other
        )
        other_state = egress_sidecar.layout(self.other, self.root).state_file
        self.assertTrue(other_state.is_file())
        egress_sidecar.down(RUNTIME, root=self.root, deps=self.deps())
        self.assertFalse(egress_sidecar.layout(RUNTIME, self.root).state_file.exists())
        self.assertTrue(other_state.is_file())

    def test_every_command_names_its_own_profile(self) -> None:
        self.ensure()
        for argv in self.fake.calls:
            self.assertEqual(argv[1:3], ["--context", "colima-work"], argv)

    def test_the_ca_is_named_for_the_profile(self) -> None:
        import subprocess

        from project_sandbox import session_ca

        self.ensure()
        ca = session_ca.load(egress_sidecar.layout(RUNTIME, self.root).sidecar)
        assert ca is not None
        subject = subprocess.run(
            ["openssl", "x509", "-noout", "-subject", "-in", str(ca.cert_path)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertIn("project-sandbox egress colima-work", subject)

    def test_the_configured_port_is_not_a_restart_input_on_colima(self) -> None:
        from project_sandbox.container_cli import DOCKER

        policy = _policy(self.tmp)
        colima = egress_sidecar.restart_inputs(RUNTIME, policy, image=IMAGE)
        docker = egress_sidecar.restart_inputs(DOCKER, policy, image=IMAGE)
        self.assertNotIn("port", colima)
        self.assertEqual(docker["port"], 3129)


class FixedPortTests(PrivateNetworkBase):
    custom = "[egress.sidecar]\nport = 3140\n"

    def test_a_non_default_port_is_an_error_naming_the_key(self) -> None:
        with self.assertRaises(SidecarError) as ctx:
            self.ensure(_policy(self.tmp, self.custom))
        message = str(ctx.exception)
        self.assertIn("[egress.sidecar] port = 3140", message)
        self.assertIn("not supported on Colima", message)
        self.assertEqual(self.fake.calls, [])  # nothing ran, not even a network

    def test_restart_rejects_it_too(self) -> None:
        with self.assertRaises(SidecarError):
            egress_sidecar.restart(
                RUNTIME,
                _policy(self.tmp, self.custom),
                image=IMAGE,
                root=self.root,
                deps=self.deps(),
            )
        self.assertEqual(self.fake.calls, [])

    def test_the_default_port_is_accepted_explicitly(self) -> None:
        self.assertTrue(
            self.ensure(_policy(self.tmp, "[egress.sidecar]\nport = 3129\n")).started
        )

    def test_other_runtimes_keep_honoring_the_port(self) -> None:
        from project_sandbox.container_cli import DOCKER

        policy = _policy(self.tmp, self.custom)
        egress_sidecar.check_port(DOCKER, policy)  # no error
        argv = egress_sidecar.sidecar_run_argv(
            DOCKER,
            image=IMAGE,
            sidecar_dir=Path("/s"),
            port=policy.port,
            memory="1g",
            cpus=1,
            restart_fingerprint="f",
        )
        self.assertIn("127.0.0.1:3140:3128", argv)


class EgressCliTests(PrivateNetworkBase):
    def test_the_egress_command_accepts_colima_and_a_profile(self) -> None:
        from project_sandbox import egress_cli

        self.assertIn("colima", egress_cli.RUNTIMES)
        args = egress_cli.build_parser().parse_args(
            ["status", "--runtime", "colima", "--colima-profile", "work"]
        )
        self.assertEqual((args.runtime, args.colima_profile), ("colima", "work"))
        nested = egress_cli.build_parser().parse_args(
            ["allow", "list", "--runtime", "colima", "--colima-profile", "work"]
        )
        self.assertEqual(nested.colima_profile, "work")

    def test_the_profile_flag_needs_the_colima_runtime(self) -> None:
        import contextlib
        import io

        from project_sandbox import egress_cli

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = egress_cli.main(
                ["status", "--runtime", "docker", "--colima-profile", "w"]
            )
        self.assertEqual(rc, 1)
        self.assertIn("--colima-profile requires --runtime colima", err.getvalue())


class StatusTests(PrivateNetworkBase):
    def report(self) -> dict:
        return egress_sidecar.status(
            RUNTIME, _policy(self.tmp), image=IMAGE, root=self.root, deps=self.deps()
        )

    def test_an_attached_sidecar_is_reported_healthy(self) -> None:
        self.ensure()
        report = self.report()
        self.assertTrue(report["attached"])
        self.assertEqual(report["would"], "reuse")
        self.assertEqual(report["runtime"], "colima-work")
        self.assertNotIn("NOT attached", "\n".join(egress_sidecar.status_lines(report)))

    def test_a_detached_sidecar_is_called_out(self) -> None:
        self.ensure()
        self.fake.attached = False
        report = self.report()
        self.assertIs(report["attached"], False)
        self.assertEqual(report["would"], "start")
        text = "\n".join(egress_sidecar.status_lines(report))
        self.assertIn("NOT attached to psbx-sandbox as 'egress'", text)

    def test_status_changes_nothing(self) -> None:
        self.ensure()
        before = len(self.fake.calls)
        self.report()
        mutating = {"run", "rm", "network-create", "network-connect", "reload"}
        new = {self.fake.kind(c) for c in self.fake.calls[before:]}
        self.assertFalse(new & mutating)


class SidecarMountCheckTests(PrivateNetworkBase):
    def test_the_sidecar_directory_is_checked_before_the_container_runs(self) -> None:
        self.ensure()
        lay = egress_sidecar.layout(RUNTIME, self.root)
        self.assertEqual(self.mount_checks, [[lay.sidecar]])

    def test_an_unexported_sidecar_directory_stops_the_start(self) -> None:
        def refuse(_runtime, paths):
            raise SystemExit(f"not visible: {paths[0]}")

        with self.assertRaises(SystemExit) as ctx:
            self.ensure(deps=self.deps(mount_check=refuse))
        self.assertIn("not visible", str(ctx.exception))
        self.assertEqual(self.runs(), [])

    def test_other_runtimes_never_ask_colima(self) -> None:
        from project_sandbox.container_cli import DOCKER

        asked: list[object] = []
        deps = Base.deps(
            self,
            runner=FakeRuntime(DOCKER),
            strategy=lambda _runtime: Strategy(),
            mount_check=lambda *_args: asked.append(1),
        )
        self.assertTrue(
            egress_sidecar.ensure(
                DOCKER, _policy(self.tmp), image=IMAGE, root=self.root, deps=deps
            ).started
        )
        self.assertEqual(asked, [])
