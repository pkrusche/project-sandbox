"""Ownership-labelled private networks of the Colima egress sidecar."""

import json
import sys
from pathlib import Path
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import colima_network as net
from project_sandbox.container_cli import colima_runtime


class _Result:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _record(
    spec: net.NetworkSpec,
    *,
    profile="work",
    owner=net.OWNER,
    role=None,
    internal=None,
    driver="bridge",
    options=None,
    containers=None,
) -> dict:
    labels = {net.PROFILE_LABEL: profile, net.ROLE_LABEL: role or spec.role}
    if owner is not None:
        labels[net.OWNER_LABEL] = owner
    return {
        "Name": spec.name,
        "Driver": driver,
        "Internal": spec.internal if internal is None else internal,
        "Labels": labels,
        "Options": dict(spec.options) if options is None else options,
        "Containers": {f"id-{name}": {"Name": name} for name in (containers or ())},
    }


class FakeDocker:
    """Answers network inspect/create from a dict of existing networks."""

    def __init__(
        self,
        existing: dict[str, dict] | None = None,
        *,
        create_rc: int = 0,
        create_race: bool = False,
        engine: str = "29.5.2",
    ) -> None:
        self.networks = dict(existing or {})
        self.calls: list[list[str]] = []
        self.create_rc = create_rc
        self.create_race = create_race
        self.engine = engine

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        if "version" in argv:
            return _Result(0, self.engine + "\n")
        if "disconnect" in argv:
            return _Result(0)
        if argv[-2:-1] == ["rm"]:
            self.networks.pop(argv[-1], None)
            return _Result(0)
        if "inspect" in argv:
            name = argv[argv.index("inspect") + 1]
            if name not in self.networks:
                return _Result(1, stderr=f"Error: No such network: {name}")
            return _Result(0, json.dumps(self.networks[name]))
        if "create" in argv:
            name = argv[-1]
            spec = next(s for s in net.SPECS if s.name == name)
            if self.create_race:
                # Another launch created it between our inspect and create.
                self.networks[name] = _record(spec)
                return _Result(1, stderr="network with name already exists")
            if self.create_rc:
                return _Result(self.create_rc, stderr="permission denied")
            self.networks[name] = _record(spec)
            return _Result(0, "id\n")
        raise AssertionError(f"unexpected call {argv}")

    def created(self) -> list[str]:
        return [c[-1] for c in self.calls if "create" in c]


class EnsureTests(TestCase):
    runtime = colima_runtime("work")

    def test_creates_both_networks_with_labels_and_internal_only_for_sandbox(
        self,
    ) -> None:
        docker = FakeDocker()
        net.ensure(self.runtime, docker)
        self.assertEqual(docker.created(), [net.UPLINK_NETWORK, net.SANDBOX_NETWORK])
        creates = {c[-1]: c for c in docker.calls if "create" in c}
        uplink, sandbox = creates[net.UPLINK_NETWORK], creates[net.SANDBOX_NETWORK]
        for argv in (uplink, sandbox):
            self.assertEqual(argv[:3], ["docker", "--context", "colima-work"])
            self.assertIn("bridge", argv)
            self.assertIn(f"{net.OWNER_LABEL}={net.OWNER}", argv)
            self.assertIn(f"{net.PROFILE_LABEL}=work", argv)
        self.assertNotIn("--internal", uplink)
        self.assertIn("--internal", sandbox)
        self.assertIn(f"{net.ROLE_LABEL}=sandbox", sandbox)

    def test_reuses_valid_networks_without_creating(self) -> None:
        docker = FakeDocker({s.name: _record(s) for s in net.SPECS})
        net.ensure(self.runtime, docker)
        self.assertEqual(docker.created(), [])

    def test_creates_only_the_missing_network(self) -> None:
        uplink = net.SPECS[0]
        docker = FakeDocker({uplink.name: _record(uplink)})
        net.ensure(self.runtime, docker)
        self.assertEqual(docker.created(), [net.SANDBOX_NETWORK])

    def test_losing_a_create_race_validates_the_winner(self) -> None:
        net.ensure(self.runtime, FakeDocker(create_race=True))

    def test_create_failure_is_reported(self) -> None:
        with self.assertRaises(net.NetworkError) as ctx:
            net.ensure(self.runtime, FakeDocker(create_rc=1))
        self.assertIn("permission denied", str(ctx.exception))

    def test_foreign_networks_are_refused_and_never_removed(self) -> None:
        sandbox = net.SPECS[1]
        cases = {
            "no owner label": _record(sandbox, owner=None),
            "someone else's label": _record(sandbox, owner="other-tool"),
            "another profile": _record(sandbox, profile="other"),
            "wrong role": _record(sandbox, role="egress-uplink"),
            "not internal": _record(sandbox, internal=False),
            "wrong driver": _record(sandbox, driver="macvlan"),
        }
        for name, record in cases.items():
            with self.subTest(name):
                docker = FakeDocker(
                    {net.UPLINK_NETWORK: _record(net.SPECS[0]), sandbox.name: record}
                )
                with self.assertRaises(net.NetworkError) as ctx:
                    net.ensure(self.runtime, docker)
                self.assertIn(sandbox.name, str(ctx.exception))
                self.assertFalse([c for c in docker.calls if "rm" in c])

    def test_uplink_must_not_be_internal(self) -> None:
        uplink = net.SPECS[0]
        docker = FakeDocker({uplink.name: _record(uplink, internal=True)})
        with self.assertRaises(net.NetworkError):
            net.ensure(self.runtime, docker)

    def test_listing_is_not_a_prune(self) -> None:
        docker = FakeDocker()
        net.ensure(self.runtime, docker)
        for call in docker.calls:
            self.assertNotIn("prune", call)
            self.assertNotIn("rm", call)


class AttachmentTests(TestCase):
    def container(self, **networks) -> dict:
        return {"NetworkSettings": {"Networks": networks}}

    def test_attachment_reads_the_sandbox_address_and_aliases(self) -> None:
        record = self.container(
            **{
                net.UPLINK_NETWORK: {"IPAddress": "172.18.0.2", "Aliases": []},
                net.SANDBOX_NETWORK: {
                    "IPAddress": "172.19.0.2",
                    "Aliases": ["egress", "abc123"],
                },
            }
        )
        attached = net.attachment(record)
        assert attached is not None
        self.assertEqual(attached.address, "172.19.0.2")
        self.assertIn("egress", attached.aliases)
        self.assertTrue(net.attached_to_both(record))

    def test_missing_attachments_are_detected(self) -> None:
        only_uplink = self.container(
            **{net.UPLINK_NETWORK: {"IPAddress": "172.18.0.2", "Aliases": []}}
        )
        self.assertIsNone(net.attachment(only_uplink))
        self.assertFalse(net.attached_to_both(only_uplink))
        no_alias = self.container(
            **{
                net.UPLINK_NETWORK: {"IPAddress": "172.18.0.2"},
                net.SANDBOX_NETWORK: {"IPAddress": "172.19.0.2", "Aliases": ["abc"]},
            }
        )
        self.assertFalse(net.attached_to_both(no_alias))
        self.assertIsNone(net.attachment({}))

    def test_connect_argv_names_alias_network_and_container(self) -> None:
        argv = net.connect_argv(
            colima_runtime("work"), net.SANDBOX_NETWORK, "c", "egress"
        )
        self.assertEqual(
            argv,
            [
                "docker",
                "--context",
                "colima-work",
                "network",
                "connect",
                "--alias",
                "egress",
                net.SANDBOX_NETWORK,
                "c",
            ],
        )


class GatewayModeTests(TestCase):
    """isolated-sidecar-network tasks 2.1/2.2: no VM address on psbx-sandbox."""

    runtime = colima_runtime("work")
    uplink, sandbox = net.SPECS

    def test_argv_uplink_unchanged_sandbox_gains_only_the_option(self) -> None:
        labels = [
            "--label",
            f"{net.OWNER_LABEL}={net.OWNER}",
            "--label",
            f"{net.PROFILE_LABEL}=work",
        ]
        base = ["docker", "--context", "colima-work", "network", "create"]
        self.assertEqual(
            net.create_argv(self.runtime, self.uplink),
            [*base, "--driver", "bridge", *labels, "--label",
             f"{net.ROLE_LABEL}=egress-uplink", net.UPLINK_NETWORK],
        )  # fmt: skip
        self.assertEqual(
            net.create_argv(self.runtime, self.sandbox),
            [*base, "--driver", "bridge", "--internal", "-o",
             "com.docker.network.bridge.gateway_mode_ipv4=isolated", *labels,
             "--label", f"{net.ROLE_LABEL}=sandbox", net.SANDBOX_NETWORK],
        )  # fmt: skip

    def test_missing_option_is_legacy_and_a_different_one_is_foreign(self) -> None:
        with self.assertRaises(net.LegacyNetwork) as ctx:
            net.validate(self.runtime, _record(self.sandbox, options={}), self.sandbox)
        self.assertIn("gateway_mode_ipv4=isolated", str(ctx.exception))
        other = _record(self.sandbox, options={net.GATEWAY_MODE_OPTION: "nat"})
        with self.assertRaises(net.NetworkError) as ctx:
            net.validate(self.runtime, other, self.sandbox)
        self.assertNotIsInstance(ctx.exception, net.LegacyNetwork)
        self.assertIn("'nat'", str(ctx.exception))

    def test_a_foreign_network_without_the_option_is_not_replaced(self) -> None:
        record = _record(self.sandbox, owner=None, options={})
        docker = FakeDocker(
            {self.uplink.name: _record(self.uplink), self.sandbox.name: record}
        )
        with self.assertRaises(net.NetworkError) as ctx:
            net.ensure(self.runtime, docker)
        self.assertNotIsInstance(ctx.exception, net.LegacyNetwork)
        self.assertFalse([c for c in docker.calls if c[-2:-1] == ["rm"]])

    def test_old_or_unknown_engine_is_refused_before_creating(self) -> None:
        for engine in ("27.5.1", "", "not-a-version"):
            with self.subTest(engine=engine):
                docker = FakeDocker(
                    {self.uplink.name: _record(self.uplink)}, engine=engine
                )
                with self.assertRaises(net.NetworkError) as ctx:
                    net.ensure(self.runtime, docker)
                message = str(ctx.exception)
                self.assertIn("Docker Engine 28.0", message)
                self.assertIn("colima update --profile work", message)
                self.assertEqual(docker.created(), [])

    def test_engine_is_not_checked_when_the_network_is_reused(self) -> None:
        docker = FakeDocker({s.name: _record(s) for s in net.SPECS}, engine="27.0.0")
        net.ensure(self.runtime, docker)
        self.assertFalse([c for c in docker.calls if "version" in c])

    def test_create_failure_names_the_option(self) -> None:
        docker = FakeDocker({self.uplink.name: _record(self.uplink)}, create_rc=1)
        with self.assertRaises(net.NetworkError) as ctx:
            net.ensure(self.runtime, docker)
        self.assertIn("gateway_mode_ipv4=isolated", str(ctx.exception))

    def _legacy(self, *containers: str) -> FakeDocker:
        return FakeDocker(
            {
                self.uplink.name: _record(self.uplink),
                self.sandbox.name: _record(
                    self.sandbox, options={}, containers=containers
                ),
            }
        )

    def test_unattached_legacy_network_is_replaced(self) -> None:
        docker = self._legacy()
        self.assertTrue(net.is_legacy(self.runtime, docker))
        net.ensure(self.runtime, docker)
        self.assertIn(["docker", "--context", "colima-work", "network", "rm",
                       net.SANDBOX_NETWORK], docker.calls)  # fmt: skip
        self.assertEqual(docker.created(), [net.SANDBOX_NETWORK])
        self.assertFalse(net.is_legacy(self.runtime, docker))
        self.assertEqual(
            docker.networks[net.SANDBOX_NETWORK]["Options"],
            {net.GATEWAY_MODE_OPTION: net.GATEWAY_MODE},
        )

    def test_owned_sidecar_is_disconnected_before_the_replacement(self) -> None:
        docker = self._legacy("project-sandbox-egress")
        net.ensure(self.runtime, docker, owned=("project-sandbox-egress",))
        disconnect = next(i for i, c in enumerate(docker.calls) if "disconnect" in c)
        remove = next(i for i, c in enumerate(docker.calls) if c[-2:-1] == ["rm"])
        self.assertLess(disconnect, remove)
        self.assertEqual(docker.created(), [net.SANDBOX_NETWORK])

    def test_attached_sandboxes_block_the_replacement(self) -> None:
        docker = self._legacy("project-sandbox-egress", "psbx-myproj-1")
        with self.assertRaises(net.NetworkError) as ctx:
            net.ensure(self.runtime, docker, owned=("project-sandbox-egress",))
        message = str(ctx.exception)
        self.assertIn("psbx-myproj-1 is still attached", message)
        self.assertNotIn("project-sandbox-egress is", message)
        self.assertIn(
            "project-sandbox-egress restart --runtime colima --colima-profile work",
            message,
        )
        mutating = [c for c in docker.calls if {"rm", "disconnect", "create"} & set(c)]
        self.assertEqual(mutating, [])

    def test_is_legacy_is_false_for_absent_current_and_foreign(self) -> None:
        self.assertFalse(net.is_legacy(self.runtime, FakeDocker()))
        current = FakeDocker({self.sandbox.name: _record(self.sandbox)})
        self.assertFalse(net.is_legacy(self.runtime, current))
        foreign = FakeDocker(
            {self.sandbox.name: _record(self.sandbox, owner=None, options={})}
        )
        self.assertFalse(net.is_legacy(self.runtime, foreign))
