"""local_forwarder: the shared, leased bridge forwarder (share-local-service-forwarders).

Real sockets and real ``flock``s in a temporary state directory; no container
runtime. The forwarder binds ``127.0.0.2:P`` and forwards to an echo service on
``127.0.0.1:P``, the same shape as ``<gateway>:P`` -> ``127.0.0.1:P``.
"""

import fcntl
import os
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import TestCase

from forwarder_test_support import (
    BIND,
    Echo,
    can_bind,
    has_ipv6_loopback,
    requires_second_loopback,
    roundtrip,
    wait_for,
)

from project_sandbox import __version__, egress_relay, local_forwarder
from project_sandbox.local_forwarder import ForwarderError

POLL = 0.2  # the spawned forwarders' idle poll, to keep the suite fast


class _StateTestCase(TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "project-sandbox" / "local-forward"
        self.control = egress_relay.RelayControl(files=local_forwarder.FILES)

    def directory(self, port: int) -> Path:
        return local_forwarder.state_dir(BIND, port, root_dir=self.root)

    def attach(self, port: int, **kwargs) -> local_forwarder.Lease:
        kwargs.setdefault("idle_poll", POLL)
        lease = local_forwarder.attach(BIND, port, root_dir=self.root, **kwargs)
        self.addCleanup(lease.release)
        return lease

    def stop_forwarder(self, port: int) -> None:
        directory = self.directory(port)
        if directory.exists():
            self.control.stop(directory)

    def echo(self, host: str = "127.0.0.1", port: int = 0) -> Echo:
        echo = Echo(host, port)
        self.addCleanup(echo.close)
        # Registered after the echo: runs first, so no forwarder outlives a test.
        self.addCleanup(self.stop_forwarder, echo.port)
        return echo

    def pid(self, port: int) -> int:
        return int((self.directory(port) / local_forwarder.FILES.pid).read_text())

    def forwarder_gone(self, port: int, timeout: float = 5.0) -> bool:
        return wait_for(
            lambda: (
                not self.control.is_live(self.directory(port)) and can_bind(BIND, port)
            ),
            timeout,
        )


@requires_second_loopback
class StateLayoutTests(_StateTestCase):
    """Task 2.1: private state, no symlinks followed."""

    def test_state_directory_and_files_are_private(self) -> None:
        echo = self.echo()
        self.attach(echo.port)
        directory = self.directory(echo.port)
        self.assertEqual(directory.name, f"{BIND}-{echo.port}")
        for path in (self.root, directory):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700, path)
        names = set(os.listdir(directory))
        self.assertTrue(
            {"fwd.lock", "fwd.pid", "fwd.log", "lease", "ctl.lock", "upstream"}
            <= names,
            names,
        )
        for name in names:
            self.assertEqual((directory / name).stat().st_mode & 0o777, 0o600, name)
        self.assertEqual(
            (directory / "upstream").read_text(), f"127.0.0.1\n{__version__}\n"
        )

    def test_symlinked_state_file_is_refused(self) -> None:
        echo = self.echo()
        directory = self.directory(echo.port)
        directory.mkdir(parents=True, mode=0o700)
        target = directory.parent / "elsewhere"
        target.write_text("keep\n")
        for name in ("ctl.lock", "lease", "upstream", "fwd.lock", "fwd.log"):
            link = directory / name
            link.symlink_to(target)
            with self.subTest(name=name):
                with self.assertRaisesRegex(ForwarderError, str(link)):
                    self.attach(echo.port)
                with self.assertRaisesRegex(ForwarderError, str(link)):
                    local_forwarder.check_available(BIND, echo.port, root_dir=self.root)
            link.unlink()
        self.assertEqual(target.read_text(), "keep\n")
        self.assertFalse(self.control.is_live(directory))

    def test_symlinked_state_directory_is_refused(self) -> None:
        echo = self.echo()
        elsewhere = self.root.parent / "elsewhere"
        elsewhere.mkdir(parents=True)
        self.root.mkdir(parents=True)
        self.directory(echo.port).symlink_to(elsewhere)
        with self.assertRaisesRegex(ForwarderError, "symlink"):
            self.attach(echo.port)
        self.assertEqual(os.listdir(elsewhere), [])

    def test_state_dir_name_must_be_an_address(self) -> None:
        for gateway in ("../x", "gateway", ""):
            with self.subTest(gateway=gateway), self.assertRaises(ValueError):
                local_forwarder.state_dir(gateway, 1, root_dir=self.root)


@requires_second_loopback
class IdleCheckTests(_StateTestCase):
    """Task 2.2: the forwarder-side exit check, run in a thread."""

    def _serve(self, echo: Echo, poll: float = 0.1):
        directory = local_forwarder.ensure_state_dir(self.directory(echo.port))
        result: dict = {}
        thread = threading.Thread(
            target=lambda: result.setdefault(
                "rc",
                local_forwarder.serve(
                    directory,
                    bind=BIND,
                    port=echo.port,
                    target_host="127.0.0.1",
                    target_port=echo.port,
                    idle_poll=poll,
                    install_signals=False,
                ),
            ),
            daemon=True,
        )
        return directory, thread, result

    def _hold(self, directory: Path, name: str, operation: int) -> "_Held":
        held = _Held(directory / name)
        self.addCleanup(held.close)
        fcntl.flock(held.fd, operation | fcntl.LOCK_NB)
        return held

    def test_forwarder_without_a_lease_exits_within_about_two_polls(self) -> None:
        echo = self.echo()
        directory, thread, result = self._serve(echo, poll=0.1)
        started = time.monotonic()
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 0.1 * 2 + 1.0)
        self.assertEqual(result["rc"], 0)
        self.assertFalse(self.control.is_live(directory))
        self.assertFalse((directory / "fwd.pid").exists())
        self.assertTrue(can_bind(BIND, echo.port))
        self.assertIn("no session holds a lease", (directory / "fwd.log").read_text())
        # The forwarder released ctl.lock and lease on exit.
        self._hold(directory, "ctl.lock", fcntl.LOCK_EX)
        self._hold(directory, "lease", fcntl.LOCK_EX)

    def test_forwarder_with_a_held_lease_keeps_serving(self) -> None:
        echo = self.echo()
        directory, thread, _result = self._serve(echo, poll=0.05)
        lease = self._hold(directory, "lease", fcntl.LOCK_SH)
        thread.start()
        self.assertTrue(wait_for(lambda: self.control.port(directory) is not None))
        time.sleep(0.05 * 6)
        self.assertTrue(thread.is_alive())
        self.assertEqual(roundtrip(echo.port), b"ping")
        lease.close()
        thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_attaching_session_holding_ctl_lock_postpones_the_exit(self) -> None:
        echo = self.echo()
        directory, thread, _result = self._serve(echo, poll=0.05)
        ctl = self._hold(directory, "ctl.lock", fcntl.LOCK_EX)
        thread.start()
        self.assertTrue(wait_for(lambda: self.control.port(directory) is not None))
        time.sleep(0.05 * 6)
        self.assertTrue(thread.is_alive())
        ctl.close()
        thread.join(5)
        self.assertFalse(thread.is_alive())


@requires_second_loopback
class AttachTests(_StateTestCase):
    """Task 2.3: session attach under ctl.lock."""

    def test_two_leases_share_one_forwarder_process(self) -> None:
        echo = self.echo()
        first = self.attach(echo.port)
        pid = self.pid(echo.port)
        self.assertNotEqual(pid, os.getpid())
        local_forwarder.check_available(BIND, echo.port, root_dir=self.root)
        second = self.attach(echo.port)
        self.assertEqual(self.pid(echo.port), pid)
        self.assertEqual(roundtrip(echo.port), b"ping")
        self.assertTrue(first.held and second.held)

    @unittest.skipUnless(has_ipv6_loopback(), "no IPv6 loopback on this host")
    def test_upstream_mismatch_names_both_addresses(self) -> None:
        echo = self.echo()
        self.attach(echo.port)
        pattern = rf"127\.0\.0\.1:{echo.port}(.|\n)*\[::1\]:{echo.port}"
        with self.assertRaisesRegex(ForwarderError, pattern):
            self.attach(echo.port, target_host="::1")
        with self.assertRaisesRegex(ForwarderError, pattern):
            local_forwarder.check_available(
                BIND, echo.port, target_host="::1", root_dir=self.root
            )

    @unittest.skipUnless(has_ipv6_loopback(), "no IPv6 loopback on this host")
    def test_ipv6_loopback_service_is_forwarded(self) -> None:
        echo6 = self.echo("::1")
        if not can_bind(BIND, echo6.port):
            self.skipTest("port busy on the bind address")
        self.attach(echo6.port, target_host="::1")
        self.assertEqual(roundtrip(echo6.port, b"six"), b"six")

    def test_version_mismatch_is_refused(self) -> None:
        echo = self.echo()
        self.attach(echo.port)
        (self.directory(echo.port) / "upstream").write_text("127.0.0.1\n0.0.1-old\n")
        with self.assertRaisesRegex(
            ForwarderError, r"another project-sandbox version \(0\.0\.1-old\)"
        ):
            self.attach(echo.port)
        # The forwarder other sessions rely on is left running.
        self.assertTrue(self.control.is_live(self.directory(echo.port)))

    def test_foreign_listener_is_refused(self) -> None:
        echo = self.echo()
        with socket.create_server((BIND, echo.port)):
            for check in (
                lambda: self.attach(echo.port),
                lambda: local_forwarder.check_available(
                    BIND, echo.port, root_dir=self.root
                ),
            ):
                with self.assertRaisesRegex(
                    ForwarderError, "Cannot bind .*another process owns"
                ):
                    check()
        self.assertFalse(self.control.is_live(self.directory(echo.port)))

    def test_check_available_creates_no_state(self) -> None:
        echo = self.echo()
        local_forwarder.check_available(BIND, echo.port, root_dir=self.root)
        self.assertFalse(self.root.exists())

    def test_lease_fd_is_not_inheritable(self) -> None:
        echo = self.echo()
        lease = self.attach(echo.port)
        self.assertIs(os.get_inheritable(lease.fileno()), False)
        lease.release()
        lease.release()  # idempotent
        self.assertFalse(lease.held)

    def test_spawn_failure_reports_the_forwarder_log(self) -> None:
        echo = self.echo()

        class FailingControl(egress_relay.RelayControl):
            def spawn_local_forward(self, *_a, **_kw):
                raise egress_relay.RelayError("did not start\nRelay log: (none)")

        with self.assertRaisesRegex(
            ForwarderError, f"Could not start the Ollama forwarder on {BIND}"
        ):
            self.attach(
                echo.port,
                control=FailingControl(files=local_forwarder.FILES),
            )


_SESSION = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from project_sandbox import local_forwarder
    lease = local_forwarder.attach(
        sys.argv[1], int(sys.argv[2]), root_dir=Path(sys.argv[3]),
        idle_poll=float(sys.argv[4]),
    )
    print("attached", flush=True)
    time.sleep(60)
    """
)


@requires_second_loopback
class LifecycleTests(_StateTestCase):
    """Task 2.4: a real spawned forwarder outlives sessions only while leased."""

    def _session_process(self, port: int) -> subprocess.Popen:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _SESSION,
                BIND,
                str(port),
                str(self.root),
                str(POLL),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(proc.wait)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        assert proc.stdout is not None
        self.assertEqual(proc.stdout.readline().strip(), "attached")
        return proc

    def test_spawning_session_releases_first_and_the_other_keeps_relaying(
        self,
    ) -> None:
        echo = self.echo()
        spawner = self.attach(echo.port)
        pid = self.pid(echo.port)
        other = self.attach(echo.port)
        spawner.release()
        time.sleep(POLL * 4)  # several exit checks
        self.assertTrue(self.control.is_live(self.directory(echo.port)))
        self.assertEqual(self.pid(echo.port), pid)
        before = echo.accepted
        self.assertEqual(roundtrip(echo.port, b"after"), b"after")  # new connection
        self.assertEqual(echo.accepted, before + 1)
        other.release()
        self.assertTrue(self.forwarder_gone(echo.port))

    def test_last_session_release_stops_the_forwarder(self) -> None:
        echo = self.echo()
        lease = self.attach(echo.port)
        lease.release()
        self.assertTrue(self.forwarder_gone(echo.port))
        self.assertFalse((self.directory(echo.port) / "fwd.pid").exists())

    def test_sigkilled_session_releases_its_lease(self) -> None:
        echo = self.echo()
        session = self._session_process(echo.port)
        directory = self.directory(echo.port)
        self.assertTrue(self.control.is_live(directory))
        survivor = self.attach(echo.port)
        session.send_signal(signal.SIGKILL)
        session.wait(5)
        # The other session keeps reaching the service.
        time.sleep(POLL * 3)
        self.assertEqual(roundtrip(echo.port), b"ping")
        survivor.release()
        self.assertTrue(self.forwarder_gone(echo.port))

    def test_sigkilled_only_session_lets_the_forwarder_exit(self) -> None:
        echo = self.echo()
        session = self._session_process(echo.port)
        session.send_signal(signal.SIGKILL)
        session.wait(5)
        self.assertTrue(self.forwarder_gone(echo.port))

    def test_attach_blocked_while_the_forwarder_exits_gets_a_live_forwarder(
        self,
    ) -> None:
        echo = self.echo()
        directory = self.directory(echo.port)
        first = self.attach(echo.port)
        old_pid = self.pid(echo.port)
        # Hold ctl.lock as an exiting forwarder does, then let it go away.
        ctl = _Held(directory / "ctl.lock")
        self.addCleanup(ctl.close)
        fcntl.flock(ctl.fd, fcntl.LOCK_EX)
        attached: dict = {}

        def late_session() -> None:
            try:
                attached["lease"] = self.attach(echo.port)
            except BaseException as exc:  # noqa: BLE001 - ForwarderError is SystemExit
                attached["error"] = exc

        thread = threading.Thread(target=late_session, daemon=True)
        thread.start()
        time.sleep(0.1)
        self.assertTrue(thread.is_alive(), "attach did not wait on ctl.lock")
        first.release()
        self.assertTrue(self.control.stop(directory))  # the old forwarder exits
        ctl.close()
        thread.join(egress_relay.SPAWN_TIMEOUT + 5)
        self.assertNotIn("error", attached)
        self.assertTrue(attached["lease"].held)
        self.assertTrue(self.control.is_live(directory))
        self.assertNotEqual(self.pid(echo.port), old_pid)
        self.assertEqual(roundtrip(echo.port), b"ping")
        # The new forwarder lives while the late session's lease is held.
        time.sleep(POLL * 3)
        self.assertTrue(self.control.is_live(directory))


class _Held:
    """An fd on a state file that is closed exactly once."""

    def __init__(self, path: Path) -> None:
        self.fd: int | None = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)

    def close(self) -> None:
        fd, self.fd = self.fd, None
        if fd is not None:
            os.close(fd)
