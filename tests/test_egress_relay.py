"""egress_relay: the detached host relay and its host-side control (tasks 5.1, 5.2)."""

import fcntl
import os
import signal
import socket
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path
from unittest import TestCase

from project_sandbox import egress_relay
from project_sandbox.egress_relay import RelayControl, RelayError


class FakeParent:
    """A loopback 'parent proxy' that answers any CONNECT with 200."""

    def __init__(self) -> None:
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        self.requests: list[bytes] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                self.requests.append(data)
                conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")

    def close(self) -> None:
        self.server.close()


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class ServeTests(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name)
        self.parent = FakeParent()

    def tearDown(self) -> None:
        self.parent.close()
        self._tmp.cleanup()

    def test_holds_lock_relays_connect_and_releases_on_stop(self) -> None:
        stop = threading.Event()
        result = {}
        thread = threading.Thread(
            target=lambda: result.setdefault(
                "rc",
                egress_relay.serve(
                    self.state,
                    upstream=f"http://127.0.0.1:{self.parent.port}",
                    port=0,
                    stop=stop,
                    install_signals=False,
                ),
            )
        )
        thread.start()
        control = RelayControl()
        self.assertTrue(_wait(lambda: control.port(self.state) is not None))
        self.assertTrue(control.is_live(self.state))
        self.assertEqual(
            int((self.state / egress_relay.PID_NAME).read_text()), os.getpid()
        )
        with socket.create_connection(("127.0.0.1", control.port(self.state))) as c:
            c.sendall(b"CONNECT pypi.org:443 HTTP/1.1\r\nHost: pypi.org:443\r\n\r\n")
            reply = c.recv(4096)
        self.assertTrue(reply.startswith(b"HTTP/1.1 200"))
        self.assertTrue(self.parent.requests[0].startswith(b"CONNECT pypi.org:443"))
        # A second relay on the same state directory refuses to start.
        self.assertEqual(
            egress_relay.serve(
                self.state,
                upstream=f"http://127.0.0.1:{self.parent.port}",
                port=0,
                install_signals=False,
            ),
            egress_relay.EXIT_ALREADY_RUNNING,
        )
        stop.set()
        thread.join(5)
        self.assertEqual(result["rc"], 0)
        self.assertFalse(control.is_live(self.state))
        self.assertFalse((self.state / egress_relay.PID_NAME).exists())
        self.assertIn("relaying 127.0.0.1:", (self.state / "relay.log").read_text())

    def test_starts_while_a_liveness_probe_holds_the_lock(self) -> None:
        # Regression (CI): RelayControl.is_live() takes the lock for an instant,
        # and a relay starting inside that instant exited "already running".
        self.state.mkdir(parents=True, exist_ok=True)
        probe = os.open(self.state / egress_relay.LOCK_NAME, os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, probe)
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stop = threading.Event()
        result = {}
        thread = threading.Thread(
            target=lambda: result.setdefault(
                "rc",
                egress_relay.serve(
                    self.state,
                    upstream=f"http://127.0.0.1:{self.parent.port}",
                    port=0,
                    stop=stop,
                    install_signals=False,
                ),
            )
        )
        thread.start()
        time.sleep(0.05)  # serve() is now retrying against the probe's lock
        fcntl.flock(probe, fcntl.LOCK_UN)
        control = RelayControl()
        try:
            self.assertTrue(_wait(lambda: control.port(self.state) is not None))
        finally:
            stop.set()
            thread.join(5)
        self.assertEqual(result["rc"], 0)

    def test_detached_process_and_sigterm(self) -> None:
        control = RelayControl()
        port = control.spawn(
            self.state, upstream=f"http://127.0.0.1:{self.parent.port}", port=0
        )
        self.assertGreater(port, 0)
        self.assertTrue(control.is_live(self.state))
        pid = int((self.state / egress_relay.PID_NAME).read_text())
        self.assertNotEqual(pid, os.getpid())
        # Its own session: our terminal's signals do not reach it.
        self.assertEqual(os.getsid(pid), pid)
        self.assertTrue(control.stop(self.state))
        self.assertFalse(control.is_live(self.state))

    def test_busy_recorded_port_falls_back(self) -> None:
        with socket.create_server(("127.0.0.1", 0)) as busy:
            taken = busy.getsockname()[1]
            control = RelayControl()
            port = control.spawn(
                self.state, upstream=f"http://127.0.0.1:{self.parent.port}", port=taken
            )
            try:
                self.assertNotEqual(port, taken)
            finally:
                control.stop(self.state)


class EchoParent:
    """A loopback 'parent' that echoes, counting the connections it accepted."""

    def __init__(self) -> None:
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        self.accepted = 0
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            self.accepted += 1
            with conn:
                while chunk := conn.recv(4096):
                    conn.sendall(chunk)

    def close(self) -> None:
        self.server.close()


class ForwardTests(TestCase):
    """egress-sidecar-linux task 2.3: the Docker Engine parent forwarder."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name)
        self.parent = EchoParent()

    def tearDown(self) -> None:
        self.parent.close()
        self._tmp.cleanup()

    def _serve(self, peer: str):
        stop = threading.Event()
        result: dict = {}
        thread = threading.Thread(
            target=lambda: result.setdefault(
                "rc",
                egress_relay.serve_forward(
                    self.state,
                    bind="127.0.0.1",
                    peer=peer,
                    target_port=self.parent.port,
                    stop=stop,
                    install_signals=False,
                ),
            )
        )
        thread.start()
        control = RelayControl()
        self.assertTrue(_wait(lambda: control.port(self.state) is not None))
        return stop, thread, result, control.port(self.state)

    def test_allowed_peer_is_relayed(self) -> None:
        stop, thread, result, port = self._serve("127.0.0.1")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
                c.sendall(b"GET http://x/ HTTP/1.1\r\n\r\n")
                self.assertEqual(c.recv(4096), b"GET http://x/ HTTP/1.1\r\n\r\n")
        finally:
            stop.set()
            thread.join(5)
        self.assertEqual(result["rc"], 0)
        self.assertEqual(self.parent.accepted, 1)
        log = (self.state / egress_relay.LOG_NAME).read_text()
        self.assertIn(f"to 127.0.0.1:{self.parent.port} for peer 127.0.0.1", log)

    def test_wrong_peer_is_closed_without_an_upstream_connect(self) -> None:
        stop, thread, _result, port = self._serve("172.17.0.2")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
                c.settimeout(5)
                c.sendall(b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n")
                try:
                    data = c.recv(4096)
                except ConnectionResetError:
                    data = b""
                self.assertEqual(data, b"")
        finally:
            stop.set()
            thread.join(5)
        self.assertEqual(self.parent.accepted, 0)
        self.assertIn(
            "refused peer 127.0.0.1",
            (self.state / egress_relay.LOG_NAME).read_text(),
        )

    def test_never_listens_on_a_wildcard(self) -> None:
        with self.assertRaises(SystemExit):
            egress_relay.TcpForwarder(bind="0.0.0.0", peer="172.17.0.2", target_port=1)

    def test_detached_forwarder_and_sigterm_release_the_lock(self) -> None:
        control = RelayControl()
        port = control.spawn_forward(
            self.state, bind="127.0.0.1", peer="127.0.0.1", target_port=self.parent.port
        )
        self.assertGreater(port, 0)
        self.assertTrue(control.is_live(self.state))
        with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
            c.sendall(b"ping")
            self.assertEqual(c.recv(16), b"ping")
        self.assertTrue(control.stop(self.state))  # SIGTERM
        self.assertFalse(control.is_live(self.state))
        self.assertIsNone(control.port(self.state))

    def test_spawn_forward_argv(self) -> None:
        seen = {}
        clock = iter([0.0, 99.0])
        control = RelayControl(
            popen=lambda argv, **kw: seen.update(argv=argv, **kw),
            sleep=lambda _s: None,
            clock=lambda: next(clock),
            spawn_timeout=1,
        )
        with self.assertRaises(RelayError):
            control.spawn_forward(
                self.state, bind="172.17.0.1", peer="172.17.0.2", target_port=3128
            )
        argv = seen["argv"]
        self.assertIn("--forward", argv)
        for flag, value in (
            ("--bind", "172.17.0.1"),
            ("--peer", "172.17.0.2"),
            ("--target-port", "3128"),
            ("--port", "0"),
        ):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertTrue(seen["start_new_session"])


class ControlTests(TestCase):
    """Fake spawner and kill: no process is started or signalled."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name)
        self.killed: list[tuple[int, int]] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def control(self, **kw) -> RelayControl:
        return RelayControl(
            kill=lambda pid, sig: self.killed.append((pid, sig)),
            sleep=lambda _s: None,
            **kw,
        )

    def test_pid_without_held_lock_is_never_signalled(self) -> None:
        (self.state / egress_relay.PID_NAME).write_text("4242\n")
        (self.state / egress_relay.LOCK_NAME).write_text("")
        self.assertFalse(self.control().stop(self.state))
        self.assertEqual(self.killed, [])

    def test_spawn_argv_is_detached(self) -> None:
        seen = {}

        def popen(argv, **kwargs):
            seen.update(argv=argv, **kwargs)

        clock = iter([0.0, 0.0, 99.0])
        control = self.control(popen=popen, clock=lambda: next(clock), spawn_timeout=1)
        with self.assertRaisesRegex(RelayError, "did not start"):
            control.spawn(
                self.state,
                upstream="https://proxy.corp.example",
                port=41000,
                ca_file=self.state / "ca.pem",
            )
        self.assertEqual(seen["argv"][1:3], ["-m", "project_sandbox.egress_relay"])
        self.assertIn("--port", seen["argv"])
        self.assertIn(str(self.state / "ca.pem"), seen["argv"])
        self.assertTrue(seen["start_new_session"])
        self.assertTrue(seen["close_fds"])
        self.assertEqual(seen["stdin"], egress_relay.subprocess.DEVNULL)

    def test_stop_signals_the_lock_holder(self) -> None:
        import fcntl

        fd = os.open(self.state / egress_relay.LOCK_NAME, os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX)
        (self.state / egress_relay.PID_NAME).write_text("4242\n")

        def kill(pid, sig):
            self.killed.append((pid, sig))
            os.close(fd)  # the relay exits and releases its lock

        control = RelayControl(kill=kill, sleep=lambda _s: None)
        self.assertTrue(control.stop(self.state))
        self.assertEqual(self.killed, [(4242, signal.SIGTERM)])


class PreferredPortTests(TestCase):
    def test_stable_and_separate_from_session_relay(self) -> None:
        from project_sandbox import internet_proxy, proxy_endpoint

        endpoint = proxy_endpoint.parse("https://proxy.corp.example", option="x")
        port = egress_relay.preferred_port(endpoint)
        self.assertEqual(port, egress_relay.preferred_port(endpoint))
        self.assertNotEqual(
            port,
            internet_proxy.relay_port(
                internet_proxy.parse("https://proxy.corp.example")
            ),
        )
        self.assertIn(sys.executable, RelayControl()._python)


def _has_ipv6_loopback() -> bool:
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(("::1", 0))
    except OSError:
        return False
    return True


class EchoParent6(EchoParent):
    """An echo 'service' listening on IPv6 loopback only."""

    def __init__(self) -> None:
        self.server = socket.create_server(("::1", 0), family=socket.AF_INET6)
        self.port = self.server.getsockname()[1]
        self.accepted = 0
        threading.Thread(target=self._serve, daemon=True).start()


def _echo(port: int, payload: bytes = b"ping", host: str = "127.0.0.1") -> bytes:
    with socket.create_connection((host, port), timeout=5) as c:
        c.sendall(payload)
        return c.recv(4096)


class GeneralizedForwarderTests(TestCase):
    """share-local-service-forwarders task 1.1: optional peer, ::1, exact port."""

    def setUp(self) -> None:
        self.parent = EchoParent()
        self.addCleanup(self.parent.close)

    def test_no_peer_filter_relays_any_peer(self) -> None:
        forwarder = egress_relay.TcpForwarder(
            bind="127.0.0.1", peer=None, target_port=self.parent.port
        )
        with forwarder:
            self.assertEqual(_echo(forwarder.port), b"ping")
            self.assertIn("for any peer", forwarder.describe())
        self.assertEqual(forwarder.refused, 0)
        self.assertEqual(self.parent.accepted, 1)

    @unittest.skipUnless(_has_ipv6_loopback(), "no IPv6 loopback on this host")
    def test_ipv6_loopback_upstream_is_relayed(self) -> None:
        parent6 = EchoParent6()
        self.addCleanup(parent6.close)
        forwarder = egress_relay.TcpForwarder(
            bind="127.0.0.1", peer=None, target_port=parent6.port, target_host="::1"
        )
        with forwarder:
            self.assertEqual(_echo(forwarder.port, b"six"), b"six")
            self.assertIn(f"to [::1]:{parent6.port}", forwarder.describe())
        self.assertEqual(parent6.accepted, 1)

    def test_only_loopback_targets_are_accepted(self) -> None:
        for host in ("172.17.0.1", "localhost", "0.0.0.0", "::"):
            with self.subTest(host=host), self.assertRaises(SystemExit):
                egress_relay.TcpForwarder(
                    bind="127.0.0.1", peer=None, target_port=1, target_host=host
                )

    def test_exact_port_in_use_raises_instead_of_falling_back(self) -> None:
        with socket.create_server(("127.0.0.1", 0)) as busy:
            taken = busy.getsockname()[1]
            with self.assertRaisesRegex(SystemExit, f"127.0.0.1:{taken}"):
                egress_relay.TcpForwarder(
                    bind="127.0.0.1",
                    peer=None,
                    target_port=self.parent.port,
                    port=taken,
                    exact_port=True,
                )
            # Without exact_port the same request falls back to a free port.
            fallback = egress_relay.TcpForwarder(
                bind="127.0.0.1", peer=None, target_port=self.parent.port, port=taken
            )
            self.addCleanup(fallback.close)
            self.assertNotEqual(fallback.port, taken)

    def test_exact_port_binds_the_requested_port(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            wanted = probe.getsockname()[1]
        forwarder = egress_relay.TcpForwarder(
            bind="127.0.0.1",
            peer=None,
            target_port=self.parent.port,
            port=wanted,
            exact_port=True,
        )
        with forwarder:
            self.assertEqual(forwarder.port, wanted)

    def test_listener_sets_reuseaddr(self) -> None:
        forwarder = egress_relay.TcpForwarder(
            bind="127.0.0.1", peer=None, target_port=self.parent.port
        )
        self.addCleanup(forwarder.close)
        self.assertTrue(
            forwarder._listener.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR)
        )

    def test_exact_port_tolerates_time_wait_from_a_previous_forwarder(self) -> None:
        first = egress_relay.TcpForwarder(
            bind="127.0.0.1", peer=None, target_port=self.parent.port
        )
        port = first.port
        with first:
            self.assertEqual(_echo(port), b"ping")
        # The closed connection may leave TIME_WAIT on the port; a respawn binds.
        second = egress_relay.TcpForwarder(
            bind="127.0.0.1",
            peer=None,
            target_port=self.parent.port,
            port=port,
            exact_port=True,
        )
        second.close()


class ForwardMainTests(TestCase):
    """share-local-service-forwarders task 1.2: CLI flags and spawn argv."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name)

    def _main_error(self, argv: list[str]) -> str:
        from contextlib import redirect_stderr
        from io import StringIO

        err = StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            egress_relay.main(["--state-dir", str(self.state), *argv])
        self.assertEqual(caught.exception.code, 2)
        return err.getvalue()

    def test_forward_without_peer_or_exact_port_is_rejected(self) -> None:
        err = self._main_error(
            ["--forward", "--bind", "127.0.0.1", "--target-port", "1", "--port", "0"]
        )
        self.assertIn("--peer unless --exact-port", err)

    def test_exact_port_needs_a_port(self) -> None:
        err = self._main_error(
            [
                "--forward",
                "--exact-port",
                "--bind",
                "127.0.0.1",
                "--target-port",
                "1",
                "--port",
                "0",
            ]
        )
        self.assertIn("non-zero --port", err)

    def test_target_host_must_be_loopback(self) -> None:
        err = self._main_error(
            [
                "--forward",
                "--exact-port",
                "--bind",
                "127.0.0.1",
                "--target-host",
                "10.0.0.1",
                "--target-port",
                "1",
                "--port",
                "1",
            ]
        )
        self.assertIn("--target-host", err)

    def test_lease_needs_exact_port_and_no_peer(self) -> None:
        base = ["--forward", "--bind", "127.0.0.1", "--target-port", "1"]
        err = self._main_error([*base, "--lease", "--peer", "127.0.0.1", "--port", "1"])
        self.assertIn("--lease needs --exact-port and no --peer", err)

    def test_forward_without_peer_with_exact_port_is_accepted(self) -> None:
        seen = {}

        def serve_forward(state_dir, **kwargs):
            seen.update(kwargs)
            return 0

        with unittest.mock.patch.object(egress_relay, "serve_forward", serve_forward):
            rc = egress_relay.main(
                [
                    "--state-dir",
                    str(self.state),
                    "--forward",
                    "--exact-port",
                    "--bind",
                    "127.0.0.1",
                    "--target-host",
                    "::1",
                    "--target-port",
                    "11434",
                    "--port",
                    "11434",
                ]
            )
        self.assertEqual(rc, 0)
        self.assertIsNone(seen["peer"])
        self.assertTrue(seen["exact_port"])
        self.assertEqual(seen["target_host"], "::1")

    def test_spawn_local_forward_argv(self) -> None:
        seen = {}
        clock = iter([0.0, 99.0])
        control = RelayControl(
            popen=lambda argv, **kw: seen.update(argv=argv, **kw),
            sleep=lambda _s: None,
            clock=lambda: next(clock),
            spawn_timeout=1,
        )
        with self.assertRaises(RelayError):
            control.spawn_local_forward(
                self.state,
                bind="172.17.0.1",
                port=11434,
                target_host="::1",
                target_port=11434,
            )
        argv = seen["argv"]
        self.assertEqual(argv[1:3], ["-m", "project_sandbox.egress_relay"])
        for flag in ("--forward", "--lease", "--exact-port"):
            self.assertIn(flag, argv)
        self.assertNotIn("--peer", argv)
        for flag, value in (
            ("--bind", "172.17.0.1"),
            ("--target-host", "::1"),
            ("--target-port", "11434"),
            ("--port", "11434"),
        ):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertTrue(seen["start_new_session"])
        self.assertTrue(seen["close_fds"])
        self.assertNotIn("preexec_fn", seen)

    def test_spawn_reports_an_early_exit_with_the_log(self) -> None:
        (self.state / egress_relay.LOG_NAME).write_text("x failed to start: busy\n")

        class Exited:
            def poll(self):
                return 1

        control = RelayControl(popen=lambda argv, **kw: Exited(), sleep=lambda _s: None)
        with self.assertRaisesRegex(RelayError, "exited with status 1(.|\n)*busy"):
            control.spawn_forward(
                self.state, bind="127.0.0.1", peer="127.0.0.1", target_port=1
            )


class RunLockedTests(TestCase):
    """share-local-service-forwarders task 1.3: file names and the idle callback."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name)
        self.parent = EchoParent()
        self.addCleanup(self.parent.close)

    FILES = egress_relay.StateFiles(
        lock="x.lock", pid="x.pid", port="x.port", log="x.log"
    )

    def test_custom_files_and_idle_callback_stop_the_server(self) -> None:
        calls = []

        def idle() -> bool:
            calls.append(time.monotonic())
            return len(calls) >= 2

        ready = []
        result = {}
        thread = threading.Thread(
            target=lambda: result.setdefault(
                "rc",
                egress_relay.serve_forward(
                    self.state,
                    bind="127.0.0.1",
                    peer=None,
                    target_port=self.parent.port,
                    install_signals=False,
                    files=self.FILES,
                    idle=idle,
                    idle_poll=0.05,
                    on_ready=lambda server: ready.append(
                        (self.state / "x.port").exists()
                    ),
                ),
            )
        )
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["rc"], 0)
        self.assertEqual(len(calls), 2)
        # on_ready runs before the port file (the readiness signal) appears.
        self.assertEqual(ready, [False])
        # The lock is released and the relay's own names were never used.
        control = RelayControl(files=self.FILES)
        self.assertFalse(control.is_live(self.state))
        self.assertTrue((self.state / "x.lock").exists())
        self.assertFalse((self.state / "x.pid").exists())
        self.assertFalse((self.state / "x.port").exists())
        self.assertIn("no session holds a lease", (self.state / "x.log").read_text())
        for name in ("relay.lock", "relay.pid", "relay.port", "relay.log"):
            self.assertFalse((self.state / name).exists(), name)

    def test_relay_files_are_private_and_never_follow_symlinks(self) -> None:
        target = self.state / "elsewhere"
        target.write_text("keep\n")
        (self.state / egress_relay.LOG_NAME).symlink_to(target)
        rc = egress_relay.serve_forward(
            self.state,
            bind="127.0.0.1",
            peer="127.0.0.1",
            target_port=self.parent.port,
            stop=_set_event(),
            install_signals=False,
        )
        self.assertEqual(rc, 1)
        self.assertEqual(target.read_text(), "keep\n")
        (self.state / egress_relay.LOG_NAME).unlink()
        rc = egress_relay.serve_forward(
            self.state,
            bind="127.0.0.1",
            peer="127.0.0.1",
            target_port=self.parent.port,
            stop=_set_event(),
            install_signals=False,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(
            os.stat(self.state / egress_relay.LOG_NAME).st_mode & 0o777, 0o600
        )
        self.assertEqual(
            os.stat(self.state / egress_relay.LOCK_NAME).st_mode & 0o777, 0o600
        )


def _set_event() -> threading.Event:
    event = threading.Event()
    event.set()
    return event
