"""The global egress sidecar: argv, state, ensure/down/restart/reload, no runtime."""

import datetime as dt
import json
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from unittest import TestCase

from project_sandbox import egress_policy, egress_sidecar, session_ca
from project_sandbox.container_cli import APPLE_CONTAINER, DOCKER, PODMAN
from project_sandbox.egress_sidecar import (
    CONTAINER,
    FINGERPRINT_LABEL,
    NETWORK,
    Collaborators,
    SidecarError,
)

NOW = dt.datetime(2026, 11, 6, 12, 0, tzinfo=dt.UTC)
IMAGE = "project-sandbox-egress-sidecar:0123456789abcdef"
NETWORK_INSPECT = {
    "apple-container": [
        {
            "id": NETWORK,
            "status": {
                "ipv4Gateway": "192.168.128.1",
                "ipv4Subnet": "192.168.128.0/24",
                "ipv6Subnet": "fd15:92e5:3fd6:fd30::/64",
            },
        }
    ],
    "docker": [
        {"IPAM": {"Config": [{"Subnet": "172.18.0.0/16", "Gateway": "172.18.0.1"}]}}
    ],
    "podman": [{"subnets": [{"subnet": "10.89.0.0/24", "gateway": "10.89.0.1"}]}],
}
EXPECTED_IP = {
    "apple-container": "192.168.128.2",
    "docker": "172.18.0.2",
    "podman": "10.89.0.2",
}
EXPECTED_SUBNET = {
    "apple-container": "192.168.128.0/24",
    "docker": "172.18.0.0/16",
    "podman": "10.89.0.0/24",
}
READY_PAGE = (
    "HTTP/1.1 403 Forbidden\r\n\r\n<h1>project-sandbox-egress: host-not-allowed</h1>"
)


def _done(argv, rc=0, out="", err=""):
    return subprocess.CompletedProcess(argv, rc, out, err)


class FakeRuntime:
    """A container runtime with one sidecar and one network, recording argv."""

    def __init__(self, runtime, *, ready=True, attached=(), fail=(), run_delay=0.0):
        self.runtime = runtime
        self.calls: list[list[str]] = []
        self.ready = ready
        self.container: str | None = None  # None, "running" or "stopped"
        self.labels: dict[str, str] = {}
        self.network = False
        self.attached: list[str] = list(attached)
        self.fail = set(fail)
        self.forwarders: list[int] = []
        self.run_delay = run_delay
        self.legacy: dict[str, list] = {}
        self.log_text = "squid log line\n"
        self._lock = threading.Lock()

    def kinds(self) -> list[str]:
        return [self.kind(c) for c in self.calls]

    def kind(self, argv) -> str:
        args = argv[1:]
        if args[:1] == ["network"]:
            return "network-" + args[1]
        if args[:1] == ["exec"]:
            command = args[2:]
            if command[:1] == ["sbx-reload"]:
                return "reload"
            if command == ["sbx-forwarders", "--list"]:
                return "exec"  # read-only listing
            if command[:1] == ["sbx-forwarders"]:
                return "forwarders"
            if command[:1] == ["date"]:
                return "clock"
            if "nc -w" in " ".join(command):
                return "probe"
            return "exec"
        if args[:2] == ["rm", "-f"]:
            return "rm"
        if args[:2] == ["ps", "-a"] or args[:2] == ["ls", "--all"]:
            return "list-legacy"
        return args[0]

    def _inspect(self):
        rt = self.runtime.name
        ip = EXPECTED_IP[rt]
        if rt == "apple-container":
            record = {
                "configuration": {"id": CONTAINER, "labels": self.labels},
                "status": {
                    "state": self.container,
                    "networks": [
                        {"network": "default", "ipv4Address": "192.168.64.9/24"},
                        {"network": NETWORK, "ipv4Address": f"{ip}/24"},
                    ],
                },
            }
        else:
            record = {
                "State": {"Running": self.container == "running"},
                "Config": {"Labels": self.labels},
                "NetworkSettings": {"Networks": {NETWORK: {"IPAddress": ip}}},
            }
        return json.dumps([record])

    def __call__(self, argv):
        argv = list(argv)
        with self._lock:
            self.calls.append(argv)
            kind = self.kind(argv)
        if kind in self.fail:
            return _done(argv, 1, err=f"{kind} failed")
        if kind == "network-inspect":
            if not self.network:
                return _done(argv, 1, err="no such network")
            return _done(argv, out=json.dumps(NETWORK_INSPECT[self.runtime.name]))
        if kind == "network-create":
            self.network = True
            return _done(argv)
        if kind == "network-rm":
            if self.attached:
                return _done(argv, 1, err="network has active endpoints")
            self.network = False
            return _done(argv)
        if kind == "run":
            time.sleep(self.run_delay)
            self.container = "running"
            self.labels = {}
            for i, item in enumerate(argv):
                if item == "--label":
                    key, _, value = argv[i + 1].partition("=")
                    self.labels[key] = value
            return _done(argv, out="id\n")
        if kind == "inspect":
            if self.container is None:
                return _done(argv, 1, err="not found")
            return _done(argv, out=self._inspect())
        if kind == "rm":
            self.container = None
            return _done(argv)
        if kind in ("probe", "reload", "forwarders", "clock", "exec"):
            if self.container != "running":
                return _done(argv, 1, err="not running")
            if kind == "probe":
                return _done(argv, out=READY_PAGE if self.ready else "")
            if kind == "clock":
                return _done(argv, out="1000\n")
            if kind == "exec":  # running forwarders
                return _done(argv, out="".join(f"{p}\n" for p in self.forwarders))
            return _done(argv)
        if kind == "logs":
            return _done(argv, out=self.log_text)
        if kind in ("ps", "ls"):
            records = [{"Names": name} for name in self.attached]
            if self.runtime.name == "apple-container":
                records = [
                    {
                        "configuration": {
                            "id": name,
                            "networks": [{"network": NETWORK}],
                        }
                    }
                    for name in self.attached
                ]
            return _done(argv, out=json.dumps(records))
        if kind == "list-legacy":
            return _done(argv, out=json.dumps(self.legacy.get("container", [])))
        return _done(argv)


class FakeRelay:
    def __init__(self, *, live=False, port=None, bind=None):
        self.live = live
        self._port = port
        self.bind = bind  # port spawn() binds; default: the requested one
        self.spawned: list[int] = []
        self.stopped = 0

    def is_live(self, state_dir):
        return self.live

    def port(self, state_dir):
        return self._port if self.live else None

    def spawn(self, state_dir, *, upstream, port, ca_file=None):
        self.spawned.append(port)
        self.live = True
        self._port = self.bind or port
        return self._port

    def stop(self, state_dir):
        was = self.live
        self.live = False
        if was:
            self.stopped += 1
        return was


def _policy(tmp: Path, text: str = "") -> egress_policy.PolicyFile:
    path = tmp / "egress.toml"
    path.write_text(text)
    return egress_policy.load(path)


class Base(TestCase):
    runtime = DOCKER

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "egress"
        self.fake = FakeRuntime(self.runtime)
        self.relay = FakeRelay()
        self.info = {"OperatingSystem": "Docker Desktop"}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def deps(self, **overrides) -> Collaborators:
        values: dict[str, Any] = {
            "runner": self.fake,
            "relay": self.relay,
            "runtime_info": lambda _rt: self.info,
            "now": lambda: NOW,
            "sleep": lambda _s: None,
            "readiness_timeout": 0.0,
        }
        values.update(overrides)
        return Collaborators(**values)

    def ensure(self, policy=None, *, runtime=None, image=IMAGE, **kw):
        return egress_sidecar.ensure(
            runtime or self.runtime,
            policy or _policy(self.tmp),
            image=image,
            root=self.root,
            deps=kw.pop("deps", None) or self.deps(),
        )

    @property
    def lay(self):
        return egress_sidecar.layout(self.runtime, self.root)


class ArgvTests(TestCase):
    """Task 4.2: per-runtime argv for the shared network and the sidecar."""

    def _run(self, runtime, **kw):
        return egress_sidecar.sidecar_run_argv(
            runtime,
            image=IMAGE,
            sidecar_dir=Path("/state/egress/x/sidecar"),
            subnet="10.89.0.0/24",
            memory=kw.get("memory", "256M"),
            cpus=kw.get("cpus", 1),
            restart_fingerprint="feedface",
        )

    def test_network_create_per_runtime(self) -> None:
        self.assertEqual(
            egress_sidecar.network_create_argv(DOCKER),
            [
                "docker",
                "network",
                "create",
                "--internal",
                "--label",
                "project-sandbox.egress.role=network",
                "sbx-egress",
            ],
        )
        self.assertIn("--disable-dns", egress_sidecar.network_create_argv(PODMAN))
        apple = egress_sidecar.network_create_argv(APPLE_CONTAINER)
        self.assertEqual(apple[:4], ["container", "network", "create", "--internal"])
        self.assertNotIn("--disable-dns", apple)

    def test_run_limits_labels_and_mount(self) -> None:
        for runtime in (APPLE_CONTAINER, DOCKER, PODMAN):
            with self.subTest(runtime=runtime.name):
                argv = self._run(runtime, memory="512M", cpus=2)
                joined = " ".join(argv)
                self.assertEqual(
                    argv[:5], [runtime.binary, "run", "-d", "--name", CONTAINER]
                )
                self.assertIn("--memory 512M --cpus 2", joined)
                self.assertIn("project-sandbox.egress.role=sidecar", argv)
                self.assertIn(f"{FINGERPRINT_LABEL}=feedface", argv)
                self.assertIn(
                    "type=bind,source=/state/egress/x/sidecar,"
                    "target=/project-sandbox-egress,readonly",
                    argv,
                )
                self.assertIn("SBX_INTERNAL_SUBNET=10.89.0.0/24", argv)
                self.assertNotIn("SBX_FORWARD_PORTS", joined)
                self.assertEqual(argv[-1], IMAGE)

    def test_log_options_on_docker_and_podman_only(self) -> None:
        docker = " ".join(self._run(DOCKER))
        self.assertIn("--log-opt max-size=10m --log-opt max-file=3", docker)
        podman = " ".join(self._run(PODMAN))
        self.assertIn("--log-opt max-size=10m", podman)
        self.assertNotIn("max-file", podman)
        self.assertNotIn("--log-opt", self._run(APPLE_CONTAINER))

    def test_two_networks_on_apple_connect_elsewhere(self) -> None:
        apple = self._run(APPLE_CONTAINER)
        self.assertEqual(
            apple[-5:-1], ["--network", "default", "--network", "sbx-egress"]
        )
        self.assertIsNone(egress_sidecar.network_connect_argv(APPLE_CONTAINER))
        for runtime in (DOCKER, PODMAN):
            self.assertNotIn("--network", self._run(runtime))
            self.assertEqual(
                egress_sidecar.network_connect_argv(runtime),
                [runtime.binary, "network", "connect", NETWORK, CONTAINER],
            )

    def test_exec_and_logs(self) -> None:
        self.assertEqual(
            egress_sidecar.reload_argv(DOCKER),
            ["docker", "exec", CONTAINER, "sbx-reload"],
        )
        self.assertEqual(
            egress_sidecar.forwarders_argv(APPLE_CONTAINER),
            ["container", "exec", CONTAINER, "sbx-forwarders"],
        )
        self.assertEqual(
            egress_sidecar.logs_argv(DOCKER, since=1000.4, follow=True),
            ["docker", "logs", "--follow", "--since", "1000", CONTAINER],
        )
        self.assertEqual(
            egress_sidecar.logs_argv(APPLE_CONTAINER, since=1000),
            ["container", "logs", CONTAINER],
        )

    def test_forward_port_validation(self) -> None:
        self.assertEqual(
            egress_sidecar.validate_forward_ports([8787, 8787, 1024]), (1024, 8787)
        )
        for port in (80, 1023, 3128, 70000):
            with self.subTest(port=port), self.assertRaises(SidecarError):
                egress_sidecar.validate_forward_ports([port])


class ParseTests(TestCase):
    def test_subnet_ip_and_state_per_runtime(self) -> None:
        for runtime in (APPLE_CONTAINER, DOCKER, PODMAN):
            fake = FakeRuntime(runtime)
            fake.container = "running"
            fake.labels = {FINGERPRINT_LABEL: "x"}
            with self.subTest(runtime=runtime.name):
                self.assertEqual(
                    egress_sidecar.parse_network_subnet(
                        runtime, json.dumps(NETWORK_INSPECT[runtime.name])
                    ),
                    EXPECTED_SUBNET[runtime.name],
                )
                info = egress_sidecar.parse_container(runtime, fake._inspect())
                self.assertTrue(info.running)
                self.assertEqual(info.labels, {FINGERPRINT_LABEL: "x"})
                self.assertEqual(
                    egress_sidecar.parse_sidecar_ip(runtime, fake._inspect()),
                    EXPECTED_IP[runtime.name],
                )
                fake.container = "stopped"
                self.assertFalse(
                    egress_sidecar.parse_container(runtime, fake._inspect()).running
                )

    def test_attached_listing(self) -> None:
        for runtime in (APPLE_CONTAINER, DOCKER):
            fake = FakeRuntime(
                runtime, attached=["project-sandbox-b", "project-sandbox-a"]
            )
            with self.subTest(runtime=runtime.name):
                self.assertEqual(
                    egress_sidecar.attached(runtime, fake),
                    ["project-sandbox-a", "project-sandbox-b"],
                )
        # Apple lists every container; only those on sbx-egress count.
        text = json.dumps(
            [
                {
                    "configuration": {
                        "id": "other",
                        "networks": [{"network": "default"}],
                    }
                },
                {
                    "configuration": {
                        "id": CONTAINER,
                        "networks": [{"network": NETWORK}],
                    }
                },
            ]
        )
        self.assertEqual(egress_sidecar.parse_attached(APPLE_CONTAINER, text), [])


class StateTests(Base):
    """Task 4.1: state.json is a cache; missing or garbled means "needs up"."""

    def test_round_trip_and_tolerance(self) -> None:
        self.ensure()
        state = egress_sidecar.read_state(self.lay)
        assert state is not None
        self.assertEqual(state["sidecar_ip"], "172.18.0.2")
        self.assertEqual(state["subnet"], "172.18.0.0/16")
        for garbage in (
            "",
            "{",
            "[]",
            '{"version": 1}',
            json.dumps({**state, "sidecar_ip": "x"}),
        ):
            with self.subTest(garbage=garbage[:20]):
                self.lay.state_file.write_text(garbage)
                self.assertIsNone(egress_sidecar.read_state(self.lay))
        self.lay.state_file.unlink()
        self.assertIsNone(egress_sidecar.read_state(self.lay))

    def test_garbled_state_triggers_a_fresh_up(self) -> None:
        self.ensure()
        self.lay.state_file.write_text("{garbled")
        self.fake.calls.clear()
        sidecar = self.ensure()
        self.assertTrue(sidecar.started)
        self.assertIn("run", self.fake.kinds())
        self.assertIsNotNone(egress_sidecar.read_state(self.lay))

    def test_layout_is_per_runtime(self) -> None:
        sidecar = self.ensure()
        self.assertEqual(sidecar.layout.root, self.root / "docker")
        self.assertEqual(self.lay.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.lay.sidecar.stat().st_mode & 0o777, 0o700)
        key = self.lay.sidecar / session_ca.KEY_NAME
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        for name in ("squid.conf", "forwarders", "ca.pem", "ca-key.pem"):
            self.assertTrue((self.lay.sidecar / name).is_file(), name)


class EnsureTests(Base):
    """Task 4.3: absent, running-and-ready, stopped, unready, incomplete."""

    def test_absent_cold_start_order(self) -> None:
        sidecar = self.ensure()
        self.assertTrue(sidecar.started)
        kinds = [k for k in self.fake.kinds() if k not in ("list-legacy", "network-ls")]
        self.assertEqual(
            kinds,
            [
                "inspect",  # is there a sidecar?
                "rm",
                "network-inspect",
                "network-create",
                "network-inspect",
                "run",
                "network-connect",
                "inspect",
                "probe",
            ],
        )
        self.assertEqual(sidecar.sidecar_ip, "172.18.0.2")
        self.assertFalse(sidecar.interception)
        self.assertEqual(sidecar.ca.name_constraints, ("invalid",))

    def test_running_and_ready_is_reused_untouched(self) -> None:
        self.ensure()
        self.fake.calls.clear()
        sidecar = self.ensure()
        self.assertFalse(sidecar.started)
        mutating = {
            "run",
            "rm",
            "network-create",
            "network-rm",
            "network-connect",
            "reload",
        }
        self.assertFalse(mutating & set(self.fake.kinds()), self.fake.kinds())
        self.assertEqual(sidecar.messages, [])

    def test_existing_network_is_reused(self) -> None:
        self.fake.network = True
        self.ensure()
        self.assertNotIn("network-create", self.fake.kinds())

    def test_stopped_sidecar_is_replaced(self) -> None:
        self.ensure()
        self.fake.container = "stopped"
        self.fake.calls.clear()
        sidecar = self.ensure()
        self.assertTrue(sidecar.started)
        kinds = self.fake.kinds()
        self.assertLess(kinds.index("rm"), kinds.index("run"))
        self.assertNotIn("network-create", kinds)

    def test_unready_sidecar_is_replaced(self) -> None:
        self.ensure()
        self.fake.ready = False
        self.fake.calls.clear()
        with self.assertRaisesRegex(SidecarError, "not ready") as ctx:
            self.ensure()
        self.assertIn("squid log line", str(ctx.exception))  # the log tail
        self.assertIn("rm", self.fake.kinds())
        self.assertIn("run", self.fake.kinds())

    def test_incomplete_sidecar_dir_is_replaced(self) -> None:
        self.ensure()
        (self.lay.sidecar / "squid.conf").unlink()
        self.fake.calls.clear()
        self.assertTrue(self.ensure().started)

    def test_concurrent_ensure_issues_one_run(self) -> None:
        self.fake.run_delay = 0.2
        errors: list[BaseException] = []
        results = []

        def worker():
            try:
                results.append(self.ensure())
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.fake.kinds().count("run"), 1)
        self.assertEqual(sorted(r.started for r in results), [False, True])

    def test_ca_is_reused_across_cold_starts_while_valid(self) -> None:
        self.ensure()
        first = (self.lay.sidecar / "ca.pem").read_bytes()
        self.fake.container = "stopped"
        self.ensure()
        self.assertEqual((self.lay.sidecar / "ca.pem").read_bytes(), first)
        # One day left: rotated on the next start.
        self.fake.container = "stopped"
        self.ensure(deps=self.deps(now=lambda: NOW + dt.timedelta(days=6)))
        self.assertNotEqual((self.lay.sidecar / "ca.pem").read_bytes(), first)

    def test_copilot_profile_constrains_the_ca(self) -> None:
        sidecar = self.ensure(_policy(self.tmp, "[egress.copilot]\nenabled = true\n"))
        self.assertTrue(sidecar.copilot)
        self.assertTrue(sidecar.interception)
        self.assertEqual(sidecar.ca.name_constraints, ("github.com",))
        conf = (self.lay.sidecar / "squid.conf").read_text()
        self.assertIn("acl intercepted dstdomain api.github.com", conf)

    def test_upstream_ca_is_staged_for_the_sidecar(self) -> None:
        (self.tmp / "corp.pem").write_bytes(b"CORP PEM\n")
        policy = _policy(
            self.tmp,
            '[egress.upstream]\nurl = "https://proxy.corp.example"\nca_cert = ["corp.pem"]\n',
        )
        self.ensure(policy)
        self.assertEqual(
            (self.lay.sidecar / "upstream-ca.pem").read_bytes(), b"CORP PEM\n"
        )
        conf = (self.lay.sidecar / "squid.conf").read_text()
        self.assertIn("cache_peer proxy.corp.example parent 443", conf)
        self.assertIn("never_direct allow all", conf)
        self.assertEqual(self.relay.spawned, [])  # Docker dials the parent itself


class DownRestartTests(Base):
    """Task 4.4."""

    def test_down_removes_everything_when_idle(self) -> None:
        self.ensure()
        self.fake.calls.clear()
        removed = egress_sidecar.down(self.runtime, root=self.root, deps=self.deps())
        self.assertTrue(removed)
        self.assertIn("rm", self.fake.kinds())
        self.assertIn("network-rm", self.fake.kinds())
        self.assertFalse((self.lay.sidecar / session_ca.KEY_NAME).exists())
        self.assertFalse(self.lay.sidecar.exists())
        self.assertFalse(self.lay.state_file.exists())

    def test_down_tolerates_attached_sandboxes(self) -> None:
        self.ensure()
        self.fake.attached = ["project-sandbox-a"]
        removed = egress_sidecar.down(self.runtime, root=self.root, deps=self.deps())
        self.assertFalse(removed)  # network stays for the next up
        self.assertTrue(self.fake.network)
        self.assertIsNone(self.fake.container)
        self.assertFalse((self.lay.sidecar / session_ca.KEY_NAME).exists())

    def test_restart_keeps_a_valid_ca_unless_new_ca(self) -> None:
        self.ensure()
        first = (self.lay.sidecar / "ca.pem").read_bytes()
        egress_sidecar.restart(
            self.runtime,
            _policy(self.tmp),
            image=IMAGE,
            root=self.root,
            deps=self.deps(),
        )
        self.assertEqual((self.lay.sidecar / "ca.pem").read_bytes(), first)
        self.assertEqual(self.fake.kinds().count("run"), 2)
        egress_sidecar.restart(
            self.runtime,
            _policy(self.tmp),
            image=IMAGE,
            new_ca=True,
            root=self.root,
            deps=self.deps(),
        )
        self.assertNotEqual((self.lay.sidecar / "ca.pem").read_bytes(), first)

    def test_restart_rotates_an_expiring_ca(self) -> None:
        self.ensure()
        first = (self.lay.sidecar / "ca.pem").read_bytes()
        egress_sidecar.restart(
            self.runtime,
            _policy(self.tmp),
            image=IMAGE,
            root=self.root,
            deps=self.deps(now=lambda: NOW + dt.timedelta(days=6)),
        )
        self.assertNotEqual((self.lay.sidecar / "ca.pem").read_bytes(), first)


class DriftTests(Base):
    """Task 2.4 (fingerprints) and 4.5 (drift handling)."""

    def test_fingerprints_separate_reload_from_restart(self) -> None:
        base = _policy(self.tmp, '[egress]\nallow = ["pypi.org"]\n')
        conf = egress_sidecar.render_squid_conf(base, copilot=False, upstream=None)
        restart = egress_sidecar.restart_inputs(DOCKER, base, image=IMAGE)

        def fingerprints(policy, *, image=IMAGE, running_copilot=False):
            return (
                egress_sidecar.reload_fingerprint(
                    egress_sidecar.render_squid_conf(
                        policy, copilot=running_copilot, upstream=None
                    )
                ),
                egress_sidecar.fingerprint(
                    egress_sidecar.restart_inputs(DOCKER, policy, image=image)
                ),
            )

        reload_fp = egress_sidecar.reload_fingerprint(conf)
        restart_fp = egress_sidecar.fingerprint(restart)
        allow = _policy(self.tmp, '[egress]\nallow = ["pypi.org", "npmjs.org"]\n')
        self.assertEqual(fingerprints(allow)[1], restart_fp)
        self.assertNotEqual(fingerprints(allow)[0], reload_fp)
        for text in (
            '[egress]\nallow = ["pypi.org"]\n[egress.copilot]\nenabled = true\n',
            '[egress]\nallow = ["pypi.org"]\n[egress.upstream]\nurl = "http://p.example:3128"\n',
            '[egress]\nallow = ["pypi.org"]\n[egress.sidecar]\nmemory = "512M"\n',
            '[egress]\nallow = ["pypi.org"]\n[egress.sidecar]\ncpus = 2\n',
        ):
            with self.subTest(text=text):
                reload_now, restart_now = fingerprints(_policy(self.tmp, text))
                # Rendered against the running (old) upstream and Copilot setting.
                self.assertEqual(reload_now, reload_fp)
                self.assertNotEqual(restart_now, restart_fp)
        reload_now, restart_now = fingerprints(base, image="other:tag")
        self.assertEqual(reload_now, reload_fp)
        self.assertNotEqual(restart_now, restart_fp)

    def test_allowlist_change_reloads_without_restart(self) -> None:
        self.ensure(_policy(self.tmp, '[egress]\nallow = ["pypi.org"]\n'))
        self.fake.calls.clear()
        sidecar = self.ensure(
            _policy(self.tmp, '[egress]\nallow = ["pypi.org", "npmjs.org"]\n')
        )
        kinds = self.fake.kinds()
        self.assertIn("reload", kinds)
        self.assertNotIn("rm", kinds)
        self.assertNotIn("run", kinds)
        self.assertIn("npmjs.org", (self.lay.sidecar / "squid.conf").read_text())
        self.assertTrue(any("reloaded, no restart" in m for m in sidecar.messages))
        # Applied once: the next launch has nothing to do.
        self.fake.calls.clear()
        self.ensure(_policy(self.tmp, '[egress]\nallow = ["pypi.org", "npmjs.org"]\n'))
        self.assertNotIn("reload", self.fake.kinds())

    def test_restart_only_changes_are_named_and_not_applied(self) -> None:
        self.ensure()
        self.fake.calls.clear()
        sidecar = self.ensure(
            _policy(
                self.tmp,
                '[egress.copilot]\nenabled = true\n[egress.sidecar]\nmemory = "1G"\n',
            ),
            image="project-sandbox-egress-sidecar:newer",
        )
        kinds = self.fake.kinds()
        self.assertNotIn("run", kinds)
        self.assertNotIn("rm", kinds)
        self.assertNotIn("reload", kinds)
        warning = next(m for m in sidecar.messages if m.startswith("[W]"))
        for name in (
            "sidecar image",
            "[egress.copilot] enabled",
            "[egress.sidecar] memory",
        ):
            self.assertIn(name, warning)
        self.assertNotIn("[egress.upstream] url", warning)
        self.assertIn("project-sandbox-egress restart", warning)
        self.assertFalse(sidecar.copilot)  # still the running setting

    def test_reload_requires_a_running_sidecar(self) -> None:
        with self.assertRaisesRegex(SidecarError, "project-sandbox-egress up"):
            egress_sidecar.reload(
                self.runtime,
                _policy(self.tmp),
                image=IMAGE,
                root=self.root,
                deps=self.deps(),
            )
        self.assertNotIn("run", self.fake.kinds())


class ForwarderTests(Base):
    """Task 4.6."""

    def test_desktop_accepts_forwarders_and_reconciles_live(self) -> None:
        self.ensure(_policy(self.tmp, "[egress.sidecar]\nforward_ports = [8787]\n"))
        self.assertEqual((self.lay.sidecar / "forwarders").read_text(), "8787\n")
        self.fake.calls.clear()
        sidecar = self.ensure(
            _policy(self.tmp, "[egress.sidecar]\nforward_ports = [8787, 11434]\n")
        )
        kinds = self.fake.kinds()
        self.assertIn("forwarders", kinds)
        self.assertNotIn("run", kinds)
        self.assertEqual((self.lay.sidecar / "forwarders").read_text(), "8787\n11434\n")
        self.assertEqual(sidecar.forward_ports, (8787, 11434))

    def test_engine_with_forwarders_is_rejected_before_run(self) -> None:
        self.info = {"OperatingSystem": "Ubuntu 24.04 LTS"}
        with self.assertRaisesRegex(SidecarError, "not supported on this docker"):
            self.ensure(_policy(self.tmp, "[egress.sidecar]\nforward_ports = [8787]\n"))
        self.assertEqual(
            [k for k in self.fake.kinds() if k not in ("list-legacy", "network-ls")],
            ["inspect"],
        )

    def test_engine_without_forwarders_is_accepted(self) -> None:
        self.info = {"OperatingSystem": "Ubuntu 24.04 LTS"}
        self.assertTrue(self.ensure().started)

    def test_pasta_podman_rejected_machine_accepted(self) -> None:
        policy = _policy(self.tmp, "[egress.sidecar]\nforward_ports = [8787]\n")
        info = {"host": {"security": {"rootless": True}, "rootlessNetworkCmd": "pasta"}}
        with self.subTest("macOS podman machine"):
            self.assertTrue(
                egress_sidecar.forwarding_supported(PODMAN, info=lambda _rt: info)
                or __import__("sys").platform == "linux"
            )
        from unittest.mock import patch

        with patch("sys.platform", "linux"):
            self.assertFalse(
                egress_sidecar.forwarding_supported(PODMAN, info=lambda _rt: info)
            )
        self.assertTrue(egress_sidecar.forwarding_supported(APPLE_CONTAINER))
        del policy

    def test_engine_live_add_is_strict_in_reload(self) -> None:
        self.info = {"OperatingSystem": "Ubuntu 24.04 LTS"}
        self.ensure()
        policy = _policy(self.tmp, "[egress.sidecar]\nforward_ports = [8787]\n")
        with self.assertRaisesRegex(SidecarError, "not supported"):
            egress_sidecar.reload(
                self.runtime, policy, image=IMAGE, root=self.root, deps=self.deps()
            )
        # A launch only warns and keeps the running sidecar.
        sidecar = self.ensure(policy)
        self.assertTrue(any("not supported" in m for m in sidecar.messages))


class RelayIntegrationTests(Base):
    """Task 5.2 with a fake relay (Apple container, remote upstream)."""

    runtime = APPLE_CONTAINER
    UPSTREAM = '[egress.upstream]\nurl = "https://proxy.corp.example:8443"\n'

    def test_cold_start_spawns_relay_and_points_squid_at_it(self) -> None:
        sidecar = self.ensure(_policy(self.tmp, self.UPSTREAM))
        self.assertEqual(len(self.relay.spawned), 1)
        port = self.relay.spawned[0]
        self.assertEqual(sidecar.state["relay_port"], port)
        conf = (self.lay.sidecar / "squid.conf").read_text()
        self.assertIn(f"cache_peer host.docker.internal parent {port} ", conf)
        self.assertNotIn(" tls", conf.split("cache_peer", 1)[1].splitlines()[0])

    def test_dead_relay_is_respawned_on_the_recorded_port(self) -> None:
        self.ensure(_policy(self.tmp, self.UPSTREAM))
        port = self.relay.spawned[0]
        self.relay.live = False
        self.fake.calls.clear()
        sidecar = self.ensure(_policy(self.tmp, self.UPSTREAM))
        self.assertEqual(self.relay.spawned, [port, port])
        self.assertNotIn("reload", self.fake.kinds())
        self.assertNotIn("run", self.fake.kinds())
        self.assertTrue(
            any("Restarted the egress relay" in m for m in sidecar.messages)
        )

    def test_port_move_rerenders_and_reloads(self) -> None:
        self.ensure(_policy(self.tmp, self.UPSTREAM))
        self.relay.live = False
        self.relay.bind = 45678
        self.fake.calls.clear()
        sidecar = self.ensure(_policy(self.tmp, self.UPSTREAM))
        self.assertIn("reload", self.fake.kinds())
        self.assertNotIn("run", self.fake.kinds())
        self.assertEqual(sidecar.state["relay_port"], 45678)
        conf = (self.lay.sidecar / "squid.conf").read_text()
        self.assertIn("cache_peer host.docker.internal parent 45678 ", conf)
        # The restart fingerprint is unaffected by the move.
        self.assertFalse(any(m.startswith("[W]") for m in sidecar.messages))

    def test_down_kills_the_relay(self) -> None:
        self.ensure(_policy(self.tmp, self.UPSTREAM))
        egress_sidecar.down(self.runtime, root=self.root, deps=self.deps())
        self.assertEqual(self.relay.stopped, 1)
        self.assertFalse(self.relay.live)

    def test_no_relay_for_loopback_or_other_runtimes(self) -> None:
        self.ensure(
            _policy(self.tmp, '[egress.upstream]\nurl = "http://127.0.0.1:18080"\n')
        )
        self.assertEqual(self.relay.spawned, [])
        conf = (self.lay.sidecar / "squid.conf").read_text()
        self.assertIn("cache_peer host.docker.internal parent 18080 ", conf)
        self.assertFalse(
            egress_sidecar.needs_relay(DOCKER, "https://proxy.corp.example")
        )


class StatusTests(Base):
    def test_absent(self) -> None:
        report = egress_sidecar.status(
            self.runtime,
            _policy(self.tmp),
            image=IMAGE,
            root=self.root,
            deps=self.deps(),
        )
        self.assertFalse(report["exists"])
        self.assertEqual(report["would"], "start")
        self.assertIn(
            "State: not running", "\n".join(egress_sidecar.status_lines(report))
        )

    def test_status_is_read_only_and_reports_drift(self) -> None:
        self.ensure()
        self.fake.attached = ["project-sandbox-a"]
        self.fake.forwarders = []
        before = self.lay.state_file.read_bytes()
        self.fake.calls.clear()
        policy = _policy(
            self.tmp,
            '[egress]\nallow = ["pypi.org"]\n[egress.sidecar]\nforward_ports = [8787]\ncpus = 2\n',
        )
        report = egress_sidecar.status(
            self.runtime, policy, image=IMAGE, root=self.root, deps=self.deps()
        )
        mutating = {
            "run",
            "rm",
            "network-create",
            "network-rm",
            "network-connect",
            "reload",
            "forwarders",
        }
        self.assertFalse(mutating & set(self.fake.kinds()), self.fake.kinds())
        self.assertEqual(self.lay.state_file.read_bytes(), before)
        self.assertEqual(report["would"], "reuse")
        self.assertTrue(report["drift"]["reload"])
        self.assertTrue(report["drift"]["forwarders"])
        self.assertEqual(report["drift"]["restart"], ["[egress.sidecar] cpus"])
        self.assertEqual(report["attached"], ["project-sandbox-a"])
        text = "\n".join(egress_sidecar.status_lines(report))
        self.assertIn("forwarder configuration differs", text)
        self.assertIn("project-sandbox-egress reload", text)
        self.assertIn("Attached sandboxes: project-sandbox-a", text)
        json.dumps(report)  # --json


class SandboxInputTests(TestCase):
    def test_inputs_with_and_without_interception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for bump, expect_ca in (([], False), (["api.github.com"], True)):
                ca_dir = root / f"ca-{expect_ca}"
                ca_dir.mkdir()
                ca = session_ca.generate(ca_dir, bump, name="x")
                session = root / f"s-{expect_ca}"
                session.mkdir()
                upstream = root / "upstream.pem"
                upstream.write_bytes(b"UP")
                sandbox = egress_sidecar.write_sandbox_inputs(
                    session,
                    sidecar_ip="192.168.128.2",
                    forward_ports=[8787],
                    ca=ca,
                    upstream_ca=upstream,
                )
                with self.subTest(interception=expect_ca):
                    self.assertEqual(
                        (sandbox / "egress.conf").read_text(),
                        "sidecar_ip=192.168.128.2\nforward_ports=8787\n",
                    )
                    self.assertEqual((sandbox / "ca.pem").exists(), expect_ca)
                    self.assertEqual((sandbox / "upstream-ca.pem").read_bytes(), b"UP")
                    self.assertFalse((sandbox / "ca-key.pem").exists())
                    self.assertEqual(sandbox.stat().st_mode & 0o777, 0o755)

    def test_missing_forwarders(self) -> None:
        self.assertEqual(
            egress_sidecar.missing_forwarders([8787, 11434], [8787]), [11434]
        )
        self.assertEqual(egress_sidecar.missing_forwarders([], [8787]), [])


class CACheckTests(Base):
    """Task 6.4 helpers, with an injected clock."""

    def _sidecar(self, copilot: bool):
        text = "[egress.copilot]\nenabled = true\n" if copilot else ""
        return self.ensure(_policy(self.tmp, text))

    def test_warn_refuse_and_silence(self) -> None:
        sidecar = self._sidecar(copilot=True)
        self.assertEqual(
            egress_sidecar.check_ca(sidecar, now=NOW), egress_sidecar.CACheck()
        )
        soon = egress_sidecar.check_ca(
            sidecar, now=NOW + dt.timedelta(days=5, hours=18)
        )
        self.assertIn("expires in 30 hours", soon.warning or "")
        self.assertIn("project-sandbox-egress restart", soon.warning or "")
        self.assertIsNone(soon.error)
        expired = egress_sidecar.check_ca(sidecar, now=NOW + dt.timedelta(days=8))
        self.assertIn("expired", expired.error or "")

    def test_silent_when_nothing_is_intercepted(self) -> None:
        sidecar = self._sidecar(copilot=False)
        self.assertEqual(
            egress_sidecar.check_ca(sidecar, now=NOW + dt.timedelta(days=30)),
            egress_sidecar.CACheck(),
        )


class LegacyTests(TestCase):
    """Task 4.7: per-session resources of earlier versions."""

    SID_LIVE = "20261104t120000-aaaaaaaaaaaa"
    SID_DEAD = "20261104t120000-bbbbbbbbbbbb"

    def _listing(self, runtime, sids):
        legacy = egress_sidecar.LEGACY_LABEL
        if runtime.name == "apple-container":
            return [
                {
                    "configuration": {
                        "id": f"project-sandbox-egress-{sid}",
                        "labels": {legacy: sid},
                    }
                }
                for sid in sids
            ] + [{"configuration": {"id": "unrelated", "labels": {}}}]
        return [
            {"Names": [f"project-sandbox-egress-{sid}"], "Labels": f"{legacy}={sid}"}
            for sid in sids
        ]

    def test_removes_only_dead_sessions_and_keeps_runtime_dirs(self) -> None:
        for runtime in (APPLE_CONTAINER, DOCKER):
            with (
                self.subTest(runtime=runtime.name),
                tempfile.TemporaryDirectory() as tmp,
            ):
                root = Path(tmp)
                runtime_dir = root / runtime.name
                runtime_dir.mkdir()
                stale = root / self.SID_DEAD
                stale.mkdir()
                old = time.time() - 3600
                import os

                for path in (runtime_dir, stale):
                    os.utime(path, (old, old))
                listing = json.dumps(
                    self._listing(runtime, [self.SID_LIVE, self.SID_DEAD])
                )
                calls = []

                def runner(argv, calls=calls, listing=listing):
                    calls.append(list(argv))
                    if argv[1:2] == ["rm"] or argv[1:3] == ["network", "rm"]:
                        return _done(argv)
                    return _done(argv, out=listing)

                removed = egress_sidecar.cleanup_legacy(
                    runtime,
                    runner=runner,
                    root=root,
                    is_live=lambda sid: sid == self.SID_LIVE,
                )
                name = f"project-sandbox-egress-{self.SID_DEAD}"
                self.assertEqual(removed, [name, name])  # container, then network
                self.assertTrue(runtime_dir.exists())  # never a legacy directory
                self.assertFalse(stale.exists())
                if runtime.name == "docker":
                    self.assertIn(
                        f"label={egress_sidecar.LEGACY_LABEL}", " ".join(calls[0])
                    )

    def test_listing_failure_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            removed = egress_sidecar.cleanup_legacy(
                DOCKER, runner=lambda argv: _done(argv, 1), root=Path(tmp)
            )
        self.assertEqual(removed, [])

    def test_read_only_listing_for_doctor(self) -> None:
        listing = json.dumps(self._listing(DOCKER, [self.SID_DEAD]))
        calls = []

        def runner(argv):
            calls.append(list(argv))
            return _done(argv, out=listing)

        names = egress_sidecar.legacy_resources(DOCKER, runner=runner)
        self.assertEqual(len(names), 2)
        self.assertFalse([c for c in calls if "rm" in c])
