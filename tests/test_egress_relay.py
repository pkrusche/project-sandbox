"""egress_relay: the detached host relay and its host-side control (tasks 5.1, 5.2)."""

import fcntl
import os
import signal
import socket
import sys
import tempfile
import threading
import time
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
