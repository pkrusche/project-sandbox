"""The global egress sidecar: argv, state, ensure/down/restart/reload, no runtime."""

import datetime as dt
import json
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from unittest import TestCase
from unittest.mock import patch

from project_sandbox import egress_policy, egress_sidecar, session_ca
from project_sandbox.container_cli import APPLE_CONTAINER, DOCKER, PODMAN
from project_sandbox.egress_sidecar import (
    CONTAINER,
    FINGERPRINT_LABEL,
    PORT_LABEL,
    ROLE_LABEL,
    Collaborators,
    SidecarError,
)

NOW = dt.datetime(2026, 11, 6, 12, 0, tzinfo=dt.UTC)
IMAGE = "project-sandbox-egress-sidecar:0123456789abcdef"
READY_PAGE = (
    "HTTP/1.1 403 Forbidden\r\n\r\n<h1>project-sandbox-egress: host-not-allowed</h1>"
)
# Argv kinds that change runtime state; read-only views must issue none.
MUTATING = {"run", "rm", "reload", "network-rm", "network-create", "network-connect"}


def _done(argv, rc=0, out="", err=""):
    return subprocess.CompletedProcess(argv, rc, out, err)


class FakeRuntime:
    """A container runtime with one sidecar container, recording argv."""

    def __init__(self, runtime, *, ready=True, fail=(), run_delay=0.0):
        self.runtime = runtime
        self.calls: list[list[str]] = []
        self.ready = ready
        self.container: str | None = None  # None, "running" or "stopped"
        self.labels: dict[str, str] = {}
        self.fail = set(fail)
        self.run_delay = run_delay
        self.log_text = "squid log line\n"
        self.ip = "172.17.0.2"
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
            if command[:1] == ["date"]:
                return "clock"
            if "nc -w" in " ".join(command):
                return "probe"
            return "exec"
        if args[:2] == ["rm", "-f"]:
            return "rm"
        return args[0]

    def _inspect(self):
        if self.runtime.name == "apple-container":
            record = {
                "configuration": {"id": CONTAINER, "labels": self.labels},
                "status": {"state": self.container},
            }
        else:
            record = {
                "State": {"Running": self.container == "running"},
                "Config": {"Labels": self.labels},
                "NetworkSettings": {"Networks": {"bridge": {"IPAddress": self.ip}}},
            }
        return json.dumps([record])

    def __call__(self, argv):
        argv = list(argv)
        with self._lock:
            self.calls.append(argv)
            kind = self.kind(argv)
        if kind in self.fail:
            return _done(argv, 1, err=f"{kind} failed")
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
        if kind in ("probe", "reload", "clock", "exec"):
            if self.container != "running":
                return _done(argv, 1, err="not running")
            if kind == "probe":
                return _done(argv, out=READY_PAGE if self.ready else "")
            if kind == "clock":
                return _done(argv, out="1000\n")
            return _done(argv)
        if kind == "logs":
            return _done(argv, out=self.log_text)
        if kind == "network-rm":
            return _done(argv, 1, err="network sbx-egress not found")
        return _done(argv)


class FakeRelay:
    def __init__(self, *, live=False, port=None, bind=None):
        self.live = live
        self._port = port
        self.bind = bind  # port spawn() binds; default: the requested one
        self.spawned: list[int] = []
        self.forwards: list[dict] = []
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

    def spawn_forward(self, state_dir, *, bind, peer, target_port, port=0):
        self.forwards.append(
            {"bind": bind, "peer": peer, "target_port": target_port, "port": port}
        )
        self.live = True
        self._port = self.bind or port or 40000 + len(self.forwards)
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
        self.taken: set[int] = set()
        self.host_probes: list[int] = []
        self.host_addresses: list[str] = []
        self.host_answers = True
        self.strategy = egress_sidecar.Strategy()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _host_probe(self, port: int, address: str) -> bool:
        self.host_probes.append(port)
        self.host_addresses.append(address)
        return self.host_answers

    def deps(self, **overrides) -> Collaborators:
        values: dict[str, Any] = {
            "runner": self.fake,
            "relay": self.relay,
            "now": lambda: NOW,
            "sleep": lambda _s: None,
            "readiness_timeout": 0.0,
            "port_free": lambda port, _address: port not in self.taken,
            "port_release_timeout": 0.0,
            "host_probe": self._host_probe,
            "strategy": lambda _rt: self.strategy,
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
            **kw,
        )

    @property
    def lay(self):
        return egress_sidecar.layout(self.runtime, self.root)


class ArgvTests(TestCase):
    """Task 4.2: per-runtime argv for the published sidecar."""

    def _run(self, runtime, **kw):
        return egress_sidecar.sidecar_run_argv(
            runtime,
            image=IMAGE,
            sidecar_dir=Path("/state/egress/x/sidecar"),
            port=kw.get("port", 3129),
            memory=kw.get("memory", "256M"),
            cpus=kw.get("cpus", 1),
            restart_fingerprint="feedface",
        )

    def test_run_limits_labels_publish_and_mount(self) -> None:
        for runtime in (APPLE_CONTAINER, DOCKER, PODMAN):
            with self.subTest(runtime=runtime.name):
                argv = self._run(runtime, memory="512M", cpus=2, port=3140)
                joined = " ".join(argv)
                self.assertEqual(
                    argv[:5], [runtime.binary, "run", "-d", "--name", CONTAINER]
                )
                self.assertIn("--memory 512M --cpus 2", joined)
                self.assertIn(f"{ROLE_LABEL}=sidecar", argv)
                self.assertIn(f"{FINGERPRINT_LABEL}=feedface", argv)
                self.assertIn(f"{PORT_LABEL}=3140", argv)
                self.assertIn("--publish 127.0.0.1:3140:3128", joined)
                self.assertIn(
                    "type=bind,source=/state/egress/x/sidecar,"
                    "target=/project-sandbox-egress,readonly",
                    argv,
                )
                self.assertEqual(argv[-1], IMAGE)

    def test_default_network_and_loopback_only(self) -> None:
        for runtime in (APPLE_CONTAINER, DOCKER, PODMAN):
            with self.subTest(runtime=runtime.name):
                argv = self._run(runtime)
                self.assertNotIn("--network", argv)
                publishes = [
                    argv[i + 1] for i, a in enumerate(argv) if a in ("--publish", "-p")
                ]
                self.assertEqual(publishes, ["127.0.0.1:3129:3128"])
                for forbidden in ("SBX_INTERNAL_SUBNET", "sbx-egress", "--env"):
                    self.assertNotIn(forbidden, " ".join(argv))

    def test_log_options_on_docker_and_podman_only(self) -> None:
        docker = " ".join(self._run(DOCKER))
        self.assertIn("--log-opt max-size=10m --log-opt max-file=3", docker)
        podman = " ".join(self._run(PODMAN))
        self.assertIn("--log-opt max-size=10m", podman)
        self.assertNotIn("max-file", podman)
        self.assertNotIn("--log-opt", self._run(APPLE_CONTAINER))

    def test_no_network_argv_anywhere(self) -> None:
        # Decision 1: no internal network; nothing creates or connects one.
        for name in ("network_create_argv", "network_connect_argv"):
            self.assertFalse(hasattr(egress_sidecar, name), name)

    def test_exec_and_logs(self) -> None:
        self.assertEqual(
            egress_sidecar.reload_argv(DOCKER),
            ["docker", "exec", CONTAINER, "sbx-reload"],
        )
        self.assertEqual(
            egress_sidecar.logs_argv(DOCKER, since=1000.4, follow=True),
            ["docker", "logs", "--follow", "--since", "1000", CONTAINER],
        )
        self.assertEqual(
            egress_sidecar.logs_argv(APPLE_CONTAINER, since=1000),
            ["container", "logs", CONTAINER],
        )


class ParseTests(TestCase):
    def test_container_state_labels_and_port(self) -> None:
        for runtime in (APPLE_CONTAINER, DOCKER, PODMAN):
            fake = FakeRuntime(runtime)
            fake.container = "running"
            fake.labels = {FINGERPRINT_LABEL: "x", PORT_LABEL: "3129"}
            with self.subTest(runtime=runtime.name):
                info = egress_sidecar.parse_container(runtime, fake._inspect())
                self.assertTrue(info.running)
                self.assertEqual(info.port, 3129)
                fake.container = "stopped"
                fake.labels = {}
                info = egress_sidecar.parse_container(runtime, fake._inspect())
                self.assertFalse(info.running)
                self.assertIsNone(info.port)

    def test_docker_label_string(self) -> None:
        info = egress_sidecar.parse_container(
            DOCKER,
            json.dumps(
                [{"State": {"Running": True}, "Config": {"Labels": f"{PORT_LABEL}=1"}}]
            ),
        )
        self.assertEqual(info.port, 1)

    def test_apple_unknown_id_is_absent(self) -> None:
        runner = lambda argv: _done(argv, out="[]")
        self.assertIsNone(egress_sidecar.inspect(APPLE_CONTAINER, runner))


class HostHelperTests(TestCase):
    def test_port_free_detects_a_listener(self) -> None:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            self.assertFalse(egress_sidecar.port_free(port))
        self.assertTrue(egress_sidecar.port_free(port))

    def test_port_free_detects_a_wildcard_listener(self) -> None:
        # On macOS a SO_REUSEADDR bind to 127.0.0.1 succeeds next to it.
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("0.0.0.0", 0))
            listener.listen()
            self.assertFalse(egress_sidecar.port_free(listener.getsockname()[1]))

    def test_port_free_ignores_time_wait_left_by_a_stopped_sidecar(self) -> None:
        # Regression: `down` with sandboxes connected, then `up`, reported the
        # port as in use until the server-side TIME_WAIT expired.
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            client = socket.create_connection(("127.0.0.1", port))
            conn, _ = listener.accept()
            conn.close()  # the server closes first, so its side enters TIME_WAIT
            client.recv(1)
            client.close()
        self.assertTrue(egress_sidecar.port_free(port))

    def test_probe_published_needs_the_readiness_page(self) -> None:
        def serve(listener, body: bytes) -> None:
            conn, _ = listener.accept()
            with conn:
                conn.recv(4096)
                conn.sendall(body)

        for body, expected in (
            (READY_PAGE.encode(), True),
            (b"HTTP/1.1 200 OK\r\n\r\n", False),
        ):
            with self.subTest(expected=expected), socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                thread = threading.Thread(target=serve, args=(listener, body))
                thread.start()
                port = listener.getsockname()[1]
                self.assertEqual(egress_sidecar.probe_published(port), expected)
                thread.join()
        self.assertFalse(egress_sidecar.probe_published(port, timeout=0.5))


class StateTests(Base):
    """Task 4.1: state.json is a cache; missing or garbled means "needs up"."""

    def test_round_trip_and_tolerance(self) -> None:
        self.ensure()
        state = egress_sidecar.read_state(self.lay)
        assert state is not None
        self.assertEqual(state["port"], 3129)
        for dropped in ("subnet", "sidecar_ip", "forward_ports"):
            self.assertNotIn(dropped, state)
        for garbage in (
            "",
            "{",
            "[]",
            '{"version": 2}',
            json.dumps({**state, "version": 1}),  # the squid-sidecar line
            json.dumps({**state, "port": "3129"}),
            json.dumps({**state, "port": 0}),
        ):
            with self.subTest(garbage=garbage[:30]):
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
        self.assertEqual(
            sorted(p.name for p in self.lay.sidecar.iterdir()),
            ["ca-key.pem", "ca.pem", "squid.conf"],
        )

    def test_only_the_restart_fingerprint_changes_with_the_port(self) -> None:
        base = _policy(self.tmp)
        moved = _policy(self.tmp, "[egress.sidecar]\nport = 3140\n")
        self.assertEqual(
            egress_sidecar.render_squid_conf(base, copilot=False, upstream=None),
            egress_sidecar.render_squid_conf(moved, copilot=False, upstream=None),
        )
        self.assertNotEqual(
            egress_sidecar.fingerprint(
                egress_sidecar.restart_inputs(DOCKER, base, image=IMAGE)
            ),
            egress_sidecar.fingerprint(
                egress_sidecar.restart_inputs(DOCKER, moved, image=IMAGE)
            ),
        )


class EnsureTests(Base):
    """Task 4.3: absent, running-and-ready, stopped, unready, incomplete."""

    def test_absent_cold_start_order(self) -> None:
        sidecar = self.ensure()
        self.assertTrue(sidecar.started)
        self.assertEqual(
            self.fake.kinds(),
            [
                "inspect",  # is there a sidecar?
                "rm",  # clears a stopped or leftover one
                "run",
                "probe",  # readiness inside the sidecar
            ],
        )
        self.assertEqual(self.host_probes, [3129])  # and on the published port
        self.assertEqual(sidecar.port, 3129)
        self.assertEqual(sidecar.endpoint, "127.0.0.1:3129")
        self.assertFalse(sidecar.interception)
        self.assertEqual(sidecar.ca.name_constraints, ("invalid",))

    def test_running_and_ready_is_reused_untouched(self) -> None:
        self.ensure()
        self.fake.calls.clear()
        sidecar = self.ensure()
        self.assertFalse(sidecar.started)
        self.assertFalse(MUTATING & set(self.fake.kinds()), self.fake.kinds())
        self.assertEqual(sidecar.messages, [])

    def test_stopped_sidecar_is_replaced(self) -> None:
        self.ensure()
        self.fake.container = "stopped"
        self.fake.calls.clear()
        sidecar = self.ensure()
        self.assertTrue(sidecar.started)
        kinds = self.fake.kinds()
        self.assertLess(kinds.index("rm"), kinds.index("run"))

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

    def test_taken_port_fails_before_run(self) -> None:
        self.taken = {3129}
        with self.assertRaisesRegex(SidecarError, r"\[egress\.sidecar\] port") as ctx:
            self.ensure()
        self.assertIn("127.0.0.1:3129 is already in use", str(ctx.exception))
        self.assertNotIn("run", self.fake.kinds())
        self.assertFalse((self.lay.sidecar / session_ca.KEY_NAME).exists())

    def test_port_released_by_the_old_sidecar_is_waited_for(self) -> None:
        self.ensure()
        self.fake.container = "stopped"
        checks = iter([False, False, True])
        clock = iter(range(100))
        sidecar = self.ensure(
            deps=self.deps(
                port_free=lambda _port, _address: next(checks),
                port_release_timeout=5.0,
                clock=lambda: next(clock),
            )
        )
        self.assertTrue(sidecar.started)

    def test_unreachable_published_port_is_an_error(self) -> None:
        self.host_answers = False
        with self.assertRaisesRegex(SidecarError, "does not answer on 127.0.0.1:3129"):
            self.ensure()

    def test_leftover_container_without_matching_state_is_replaced(self) -> None:
        # A squid-sidecar-line container: running and ready, but no port label.
        self.ensure()
        self.fake.labels.pop(PORT_LABEL)
        self.fake.calls.clear()
        self.assertTrue(self.ensure().started)
        self.assertLess(self.fake.kinds().index("rm"), self.fake.kinds().index("run"))

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
        sidecar = self.ensure(policy)
        self.assertEqual(
            (self.lay.sidecar / "upstream-ca.pem").read_bytes(), b"CORP PEM\n"
        )
        self.assertEqual(sidecar.upstream_ca, self.lay.sidecar / "upstream-ca.pem")
        conf = (self.lay.sidecar / "squid.conf").read_text()
        self.assertIn("cache_peer proxy.corp.example parent 443", conf)
        self.assertIn("never_direct allow all", conf)
        self.assertEqual(self.relay.spawned, [])  # Docker dials the parent itself

    def test_configured_port_is_published(self) -> None:
        sidecar = self.ensure(_policy(self.tmp, "[egress.sidecar]\nport = 3140\n"))
        run = next(c for c in self.fake.calls if self.fake.kind(c) == "run")
        self.assertIn("127.0.0.1:3140:3128", run)
        self.assertEqual(sidecar.port, 3140)
        self.assertEqual(self.host_probes, [3140])


class DownRestartTests(Base):
    """Task 4.3 (down, restart) and 4.5 (leftovers)."""

    def test_down_removes_everything(self) -> None:
        self.ensure()
        self.fake.calls.clear()
        existed = egress_sidecar.down(self.runtime, root=self.root, deps=self.deps())
        self.assertTrue(existed)
        self.assertIn("rm", self.fake.kinds())
        self.assertIsNone(self.fake.container)
        self.assertFalse((self.lay.sidecar / session_ca.KEY_NAME).exists())
        self.assertFalse(self.lay.sidecar.exists())
        self.assertFalse(self.lay.state_file.exists())

    def test_down_when_nothing_runs(self) -> None:
        existed = egress_sidecar.down(self.runtime, root=self.root, deps=self.deps())
        self.assertFalse(existed)

    def test_down_removes_a_leftover_network_best_effort(self) -> None:
        # The fake answers `network rm` with a failure; down still succeeds.
        self.ensure()
        egress_sidecar.down(self.runtime, root=self.root, deps=self.deps())
        rm = [c for c in self.fake.calls if self.fake.kind(c) == "network-rm"]
        self.assertEqual(rm, [["docker", "network", "rm", "sbx-egress"]])
        self.assertFalse(self.lay.state_file.exists())

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
        self.assertNotIn("network-rm", self.fake.kinds())
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

    def test_restart_applies_a_new_port(self) -> None:
        self.ensure()
        sidecar = egress_sidecar.restart(
            self.runtime,
            _policy(self.tmp, "[egress.sidecar]\nport = 3140\n"),
            image=IMAGE,
            root=self.root,
            deps=self.deps(),
        )
        self.assertEqual(sidecar.port, 3140)


class DriftTests(Base):
    """Task 4.4: reload for the allowlist, warnings for restart-only fields."""

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
            '[egress]\nallow = ["pypi.org"]\n[egress.sidecar]\nport = 3140\n',
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
        reload = next(c for c in self.fake.calls if self.fake.kind(c) == "reload")
        self.assertEqual(reload, ["docker", "exec", CONTAINER, "sbx-reload"])
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

    def test_port_drift_is_a_launch_error(self) -> None:
        self.ensure()
        self.fake.calls.clear()
        moved = _policy(self.tmp, "[egress.sidecar]\nport = 3140\n")
        with self.assertRaisesRegex(
            SidecarError, "project-sandbox-egress restart"
        ) as ctx:
            self.ensure(moved, expected_port=moved.port)
        self.assertIn("publishes 127.0.0.1:3129", str(ctx.exception))
        self.assertIn("port = 3140", str(ctx.exception))
        self.assertFalse({"rm", "run"} & set(self.fake.kinds()))

    def test_port_drift_is_only_a_warning_for_up(self) -> None:
        self.ensure()
        sidecar = self.ensure(_policy(self.tmp, "[egress.sidecar]\nport = 3140\n"))
        self.assertFalse(sidecar.started)
        self.assertEqual(sidecar.port, 3129)
        warning = next(m for m in sidecar.messages if m.startswith("[W]"))
        self.assertIn("[egress.sidecar] port", warning)

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

    def test_reload_command_applies_the_allowlist(self) -> None:
        self.ensure()
        self.fake.calls.clear()
        messages = egress_sidecar.reload(
            self.runtime,
            _policy(self.tmp, '[egress]\nallow = ["pypi.org"]\n'),
            image=IMAGE,
            root=self.root,
            deps=self.deps(),
        )
        self.assertEqual([k for k in self.fake.kinds() if k in MUTATING], ["reload"])
        self.assertTrue(any("reloaded" in m for m in messages))


FIXTURES = Path(__file__).parent / "fixtures" / "runtime-info"
GATEWAY = egress_sidecar.Strategy(egress_sidecar.DOCKER_GATEWAY, "172.17.0.1")
PASTA = egress_sidecar.Strategy(egress_sidecar.PODMAN_PASTA)


def _info(name: str):
    data = json.loads((FIXTURES / f"{name}.json").read_text())
    return lambda _runtime: data


class StrategyTests(TestCase):
    """egress-sidecar-linux task 1.1: design.md Decision 1, one test per row."""

    def _strategy(self, runtime, fixture, *, platform="linux", gateway="172.17.0.1"):
        gateways: list[str] = []

        def probe_gateway(rt):
            gateways.append(rt.name)
            return gateway

        with patch("sys.platform", platform):
            strategy = egress_sidecar.endpoint_strategy(
                runtime, info=_info(fixture), gateway=probe_gateway
            )
        return strategy, gateways

    def test_apple_container_publishes_on_loopback_without_probes(self) -> None:
        def no_probe(_rt):
            raise AssertionError("Apple container needs no probe")

        self.assertEqual(
            egress_sidecar.endpoint_strategy(
                APPLE_CONTAINER, info=no_probe, gateway=no_probe
            ),
            egress_sidecar.Strategy(),
        )

    def test_docker_desktop_publishes_on_loopback(self) -> None:
        strategy, gateways = self._strategy(DOCKER, "docker-desktop")
        self.assertEqual(strategy, egress_sidecar.Strategy())
        self.assertEqual(gateways, [])

    def test_docker_engine_publishes_on_the_bridge_gateway(self) -> None:
        strategy, gateways = self._strategy(DOCKER, "docker-engine")
        self.assertEqual(strategy, GATEWAY)
        self.assertEqual(gateways, ["docker"])

    def test_docker_engine_rejects_an_unsafe_gateway(self) -> None:
        for bad in ("127.0.0.1", "0.0.0.0", "8.8.8.8", "not-an-ip"):
            with self.subTest(gateway=bad), self.assertRaises(SidecarError):
                self._strategy(DOCKER, "docker-engine", gateway=bad)

    def test_rootless_docker_is_rejected_naming_the_supported_runtimes(self) -> None:
        with self.assertRaises(SidecarError) as caught:
            self._strategy(DOCKER, "docker-rootless")
        message = str(caught.exception)
        self.assertIn("rootless Docker", message)
        for name in (
            "Apple container",
            "Docker Desktop",
            "Docker Engine",
            "Podman machine",
            "rootless Podman",
            "--egress firewall",
        ):
            self.assertIn(name, message)

    def test_rootless_podman_with_pasta(self) -> None:
        strategy, _ = self._strategy(PODMAN, "podman-pasta")
        self.assertEqual(strategy, PASTA)
        self.assertEqual(strategy.address, "127.0.0.1")

    def test_rootless_podman_with_slirp4netns_uses_the_host_alias(self) -> None:
        strategy, _ = self._strategy(PODMAN, "podman-slirp4netns")
        self.assertEqual(strategy, egress_sidecar.Strategy())

    def test_podman_machine_publishes_on_loopback(self) -> None:
        # The client reports the VM's pasta; its loopback is not this host's.
        strategy, _ = self._strategy(PODMAN, "podman-pasta", platform="darwin")
        self.assertEqual(strategy, egress_sidecar.Strategy())

    def test_probe_failure_is_a_sidecar_error(self) -> None:
        def failing(_rt):
            raise SystemExit("Could not inspect docker networking: boom")

        with self.assertRaisesRegex(SidecarError, "boom"):
            egress_sidecar.endpoint_strategy(DOCKER, info=failing)

    def test_recorded_strategy_defaults_to_loopback(self) -> None:
        self.assertEqual(
            egress_sidecar.recorded_strategy(None), egress_sidecar.Strategy()
        )
        self.assertEqual(
            egress_sidecar.recorded_strategy(
                {"strategy": "docker-gateway", "address": "172.17.0.1"}
            ),
            GATEWAY,
        )


class LinuxArgvTests(TestCase):
    """Tasks 2.1 and 3.1: the Docker Engine and pasta sidecar argv."""

    def _run(self, strategy, *, parent_port=None, runtime=DOCKER):
        return egress_sidecar.sidecar_run_argv(
            runtime,
            image=IMAGE,
            sidecar_dir=Path("/state/sidecar"),
            port=3129,
            memory="256M",
            cpus=1,
            restart_fingerprint="feedface",
            strategy=strategy,
            parent_port=parent_port,
        )

    @staticmethod
    def _values(argv, flag):
        return [argv[i + 1] for i, a in enumerate(argv) if a == flag]

    def test_docker_engine_publishes_on_the_gateway_only(self) -> None:
        argv = self._run(GATEWAY)
        self.assertEqual(self._values(argv, "--publish"), ["172.17.0.1:3129:3128"])
        self.assertIn("project-sandbox.egress.address=172.17.0.1", argv)
        self.assertNotIn("--network", argv)
        self.assertEqual(self._values(argv, "--add-host"), [])

    def test_docker_engine_loopback_parent_maps_the_alias_to_the_gateway(self) -> None:
        argv = self._run(GATEWAY, parent_port=3128)
        self.assertEqual(
            self._values(argv, "--add-host"), ["host.docker.internal:172.17.0.1"]
        )
        self.assertNotIn("--network", argv)

    def test_pasta_without_a_parent(self) -> None:
        argv = self._run(PASTA, runtime=PODMAN)
        self.assertEqual(
            self._values(argv, "--network"),
            ["pasta:-a,198.18.0.2,-n,24,-g,198.18.0.1"],
        )
        self.assertEqual(self._values(argv, "--publish"), ["127.0.0.1:3129:3128"])
        self.assertEqual(
            self._values(argv, "--add-host"), ["host.docker.internal:127.0.0.1"]
        )

    def test_pasta_with_a_loopback_parent_forwards_only_its_port(self) -> None:
        argv = self._run(PASTA, parent_port=18080, runtime=PODMAN)
        self.assertEqual(
            self._values(argv, "--network"),
            ["pasta:-a,198.18.0.2,-n,24,-g,198.18.0.1,-4,-T,18080:18080"],
        )
        self.assertEqual(self._values(argv, "--publish"), ["127.0.0.1:3129:3128"])

    def test_sandbox_plan_on_docker_engine_has_no_listener(self) -> None:
        plan = egress_sidecar.sandbox_plan(GATEWAY, 3129)
        assert plan is not None
        self.assertEqual(plan.add_host, "host.docker.internal:172.17.0.1")
        self.assertNotEqual(plan.strategy, "linux-bridge-forwarder")
        plan.start()  # no bridge forwarder
        self.assertIsNone(plan.lease)
        self.assertIsNone(egress_sidecar.sandbox_plan(PASTA, 3129))
        self.assertIsNone(egress_sidecar.sandbox_plan(egress_sidecar.Strategy(), 3129))


class DockerEngineEnsureTests(Base):
    """Task 2.1: gateway publish, port-free check and drift; task 2.4 forwarder."""

    LOOPBACK_PARENT = '[egress.upstream]\nurl = "http://127.0.0.1:3128"\n'

    def setUp(self) -> None:
        super().setUp()
        self.strategy = GATEWAY
        self.free_checks: list[tuple[int, str]] = []

    def deps(self, **overrides):
        def port_free(port, address):
            self.free_checks.append((port, address))
            return port not in self.taken

        overrides.setdefault("port_free", port_free)
        return super().deps(**overrides)

    def _run_argv(self):
        return next(c for c in self.fake.calls if self.fake.kind(c) == "run")

    def test_cold_start_publishes_and_records_the_gateway(self) -> None:
        sidecar = self.ensure()
        self.assertIn("--publish", self._run_argv())
        self.assertIn("172.17.0.1:3129:3128", self._run_argv())
        self.assertEqual(self.free_checks, [(3129, "172.17.0.1")])
        self.assertEqual(self.host_addresses, ["172.17.0.1"])
        self.assertEqual(sidecar.endpoint, "172.17.0.1:3129")
        state = egress_sidecar.read_state(self.lay)
        assert state is not None
        self.assertEqual(state["address"], "172.17.0.1")
        self.assertEqual(state["strategy"], "docker-gateway")
        self.assertEqual(state["restart_inputs"]["address"], "172.17.0.1")
        self.assertEqual(self.relay.forwards, [])  # no parent, no forwarder

    def test_taken_gateway_port_fails_before_run_naming_the_key(self) -> None:
        self.taken.add(3129)
        with self.assertRaisesRegex(
            SidecarError, r"172\.17\.0\.1:3129.*\[egress\.sidecar\] port"
        ):
            self.ensure()
        self.assertNotIn("run", self.fake.kinds())

    def test_loopback_fingerprint_is_unchanged_by_the_address_field(self) -> None:
        policy = _policy(self.tmp)
        loopback = egress_sidecar.restart_inputs(DOCKER, policy, image=IMAGE)
        self.assertNotIn("address", loopback)
        gateway = egress_sidecar.restart_inputs(
            DOCKER, policy, image=IMAGE, address="172.17.0.1"
        )
        self.assertNotEqual(
            egress_sidecar.fingerprint(loopback), egress_sidecar.fingerprint(gateway)
        )

    def test_changed_gateway_is_drift_a_launch_fails_on(self) -> None:
        self.ensure()
        self.strategy = egress_sidecar.Strategy(
            egress_sidecar.DOCKER_GATEWAY, "172.18.0.1"
        )
        self.fake.calls.clear()
        with self.assertRaisesRegex(SidecarError, "project-sandbox-egress restart"):
            self.ensure(expected_port=3129)
        self.assertNotIn("run", self.fake.kinds())
        # `up` keeps the running sidecar and names the restart-only field.
        sidecar = self.ensure()
        self.assertTrue(
            any(
                "published address" in m and m.startswith("[W]")
                for m in sidecar.messages
            )
        )
        report = egress_sidecar.status(
            DOCKER, _policy(self.tmp), image=IMAGE, root=self.root, deps=self.deps()
        )
        self.assertIn(
            "published address (Docker's default bridge gateway)",
            report["drift"]["restart"],
        )
        self.assertEqual(report["endpoint"], "172.17.0.1:3129")

    def test_loopback_parent_starts_the_forwarder_after_run(self) -> None:
        sidecar = self.ensure(_policy(self.tmp, self.LOOPBACK_PARENT))
        kinds = self.fake.kinds()
        self.assertLess(kinds.index("run"), kinds.index("reload"))
        self.assertEqual(
            self.relay.forwards,
            [
                {
                    "bind": "172.17.0.1",
                    "peer": "172.17.0.2",
                    "target_port": 3128,
                    "port": 0,
                }
            ],
        )
        port = sidecar.state["relay_port"]
        self.assertEqual(port, 40001)
        self.assertEqual(sidecar.state["sidecar_address"], "172.17.0.2")
        conf = (self.lay.sidecar / "squid.conf").read_text()
        self.assertIn(f"cache_peer host.docker.internal parent {port} ", conf)
        self.assertIn("host.docker.internal:172.17.0.1", self._run_argv())
        self.assertEqual(self.relay.spawned, [])  # never the Apple CONNECT relay

    def test_cold_start_always_restarts_the_forwarder(self) -> None:
        self.ensure(_policy(self.tmp, self.LOOPBACK_PARENT))
        self.fake.container = "stopped"  # e.g. after a reboot
        self.fake.ip = "172.17.0.5"
        self.ensure(_policy(self.tmp, self.LOOPBACK_PARENT))
        self.assertEqual(self.relay.stopped, 1)
        self.assertEqual(
            [f["peer"] for f in self.relay.forwards], ["172.17.0.2", "172.17.0.5"]
        )

    def test_dead_forwarder_is_respawned_by_a_launch(self) -> None:
        self.ensure(_policy(self.tmp, self.LOOPBACK_PARENT))
        port = self.relay._port
        self.relay.live = False
        self.relay.bind = port
        self.fake.calls.clear()
        sidecar = self.ensure(
            _policy(self.tmp, self.LOOPBACK_PARENT), expected_port=3129
        )
        self.assertNotIn("run", self.fake.kinds())
        self.assertNotIn("reload", self.fake.kinds())
        self.assertEqual(self.relay.forwards[-1]["port"], port)  # the recorded port
        self.assertTrue(
            any("Restarted the egress parent forwarder" in m for m in sidecar.messages)
        )

    def test_forwarder_port_move_rerenders_and_reloads(self) -> None:
        self.ensure(_policy(self.tmp, self.LOOPBACK_PARENT))
        self.relay.live = False
        self.relay.bind = 45678
        self.fake.calls.clear()
        sidecar = self.ensure(_policy(self.tmp, self.LOOPBACK_PARENT))
        self.assertIn("reload", self.fake.kinds())
        self.assertNotIn("run", self.fake.kinds())
        self.assertEqual(sidecar.state["relay_port"], 45678)
        conf = (self.lay.sidecar / "squid.conf").read_text()
        self.assertIn("cache_peer host.docker.internal parent 45678 ", conf)
        self.assertFalse(any(m.startswith("[W]") for m in sidecar.messages))

    def test_down_stops_the_forwarder(self) -> None:
        self.ensure(_policy(self.tmp, self.LOOPBACK_PARENT))
        egress_sidecar.down(DOCKER, root=self.root, deps=self.deps())
        self.assertFalse(self.relay.live)
        self.assertEqual(self.relay.stopped, 1)

    def test_status_reports_the_forwarder(self) -> None:
        self.ensure(_policy(self.tmp, self.LOOPBACK_PARENT))
        report = egress_sidecar.status(
            DOCKER,
            _policy(self.tmp, self.LOOPBACK_PARENT),
            image=IMAGE,
            root=self.root,
            deps=self.deps(),
        )
        self.assertEqual(report["relay"]["mode"], "forward")
        lines = "\n".join(egress_sidecar.status_lines(report))
        self.assertIn(
            "Endpoint: 172.17.0.1:3129 (Docker's default bridge gateway)", lines
        )
        self.assertIn(
            "Parent forwarder: 172.17.0.1:40001, accepts only the sidecar at 172.17.0.2",
            lines,
        )
        self.relay.live = False
        report = egress_sidecar.status(
            DOCKER,
            _policy(self.tmp, self.LOOPBACK_PARENT),
            image=IMAGE,
            root=self.root,
            deps=self.deps(),
        )
        self.assertIn(
            "Parent forwarder: not running",
            "\n".join(egress_sidecar.status_lines(report)),
        )

    def test_remote_parent_needs_no_forwarder(self) -> None:
        self.ensure(
            _policy(
                self.tmp, '[egress.upstream]\nurl = "http://proxy.corp.example:3128"\n'
            )
        )
        self.assertEqual(self.relay.forwards, [])
        self.assertNotIn("--add-host", self._run_argv())


class PastaEnsureTests(Base):
    """Task 3.1 through ensure(): the loopback parent rides the sidecar's pasta."""

    runtime = PODMAN

    def setUp(self) -> None:
        super().setUp()
        self.strategy = PASTA

    def test_loopback_parent(self) -> None:
        self.ensure(
            _policy(self.tmp, '[egress.upstream]\nurl = "http://127.0.0.1:18080"\n')
        )
        run = next(c for c in self.fake.calls if self.fake.kind(c) == "run")
        self.assertIn("pasta:-a,198.18.0.2,-n,24,-g,198.18.0.1,-4,-T,18080:18080", run)
        self.assertIn("127.0.0.1:3129:3128", run)
        self.assertEqual(self.relay.forwards, [])
        self.assertEqual(self.relay.spawned, [])
        conf = (self.lay.sidecar / "squid.conf").read_text()
        self.assertIn("cache_peer host.docker.internal parent 18080 ", conf)


class RelayIntegrationTests(Base):
    """Task 5.1 supervision with a fake relay (Apple container, remote upstream)."""

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
        text = "\n".join(egress_sidecar.status_lines(report))
        self.assertIn("State: not running", text)
        self.assertIn("Endpoint: 127.0.0.1:3129 (host loopback, once started)", text)

    def test_status_is_read_only_and_reports_drift(self) -> None:
        self.ensure()
        before = self.lay.state_file.read_bytes()
        self.fake.calls.clear()
        policy = _policy(
            self.tmp,
            '[egress]\nallow = ["pypi.org"]\n[egress.sidecar]\nport = 3140\ncpus = 2\n',
        )
        report = egress_sidecar.status(
            self.runtime, policy, image=IMAGE, root=self.root, deps=self.deps()
        )
        self.assertFalse(MUTATING & set(self.fake.kinds()), self.fake.kinds())
        self.assertEqual(self.lay.state_file.read_bytes(), before)
        self.assertEqual(report["would"], "reuse")
        self.assertEqual(report["endpoint"], "127.0.0.1:3129")
        self.assertEqual(report["configured_endpoint"], "127.0.0.1:3140")
        self.assertTrue(report["drift"]["reload"])
        self.assertEqual(
            report["drift"]["restart"],
            ["[egress.sidecar] port", "[egress.sidecar] cpus"],
        )
        text = "\n".join(egress_sidecar.status_lines(report))
        self.assertIn("Endpoint: 127.0.0.1:3129 (host loopback)", text)
        self.assertIn("project-sandbox-egress reload", text)
        self.assertIn("[egress.sidecar] port", text)
        self.assertIn("project-sandbox-egress restart", text)
        json.dumps(report)  # --json


class SandboxInputTests(TestCase):
    """Task 6.3 helper: only the public certificates reach the sandbox."""

    def test_inputs_with_and_without_interception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            upstream = root / "upstream.pem"
            upstream.write_bytes(b"UP")
            for bump, expect_ca in (([], False), (["api.github.com"], True)):
                ca_dir = root / f"ca-{expect_ca}"
                ca_dir.mkdir()
                ca = session_ca.generate(ca_dir, bump, name="x")
                session = root / f"s-{expect_ca}"
                session.mkdir()
                sandbox = egress_sidecar.write_sandbox_inputs(
                    session, ca=ca, upstream_ca=upstream
                )
                with self.subTest(interception=expect_ca):
                    expected = ["upstream-ca.pem"] + (["ca.pem"] if expect_ca else [])
                    self.assertEqual(
                        sorted(p.name for p in sandbox.iterdir()), sorted(expected)
                    )
                    self.assertEqual((sandbox / "upstream-ca.pem").read_bytes(), b"UP")
                    self.assertEqual(sandbox.stat().st_mode & 0o777, 0o755)
            empty = root / "s-empty"
            empty.mkdir()
            sandbox = egress_sidecar.write_sandbox_inputs(empty, ca=None)
            self.assertEqual(list(sandbox.iterdir()), [])
