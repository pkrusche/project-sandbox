import contextlib
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import build_cache, host_locks, jj_workspace, paths


@contextlib.contextmanager
def _noop():
    yield


class HostLockTests(TestCase):
    def test_lock_name_is_stable_across_path_spellings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "repo"
            target.mkdir()
            link = Path(tmp) / "link"
            link.symlink_to(target)

            self.assertEqual(
                host_locks.lock_path("build", target),
                host_locks.lock_path("build", link),
            )

    def test_distinct_kinds_and_targets_do_not_share_a_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            one = Path(tmp) / "one"
            two = Path(tmp) / "two"
            one.mkdir()
            two.mkdir()

            self.assertNotEqual(
                host_locks.lock_path("build", one), host_locks.lock_path("build", two)
            )
            self.assertNotEqual(
                host_locks.lock_path("build", one),
                host_locks.lock_path("jj-workspace", one),
            )

    def test_lock_is_taken_and_released(self) -> None:
        # A fresh interpreter has an independent file description and cannot
        # accidentally inherit the holder's flock.
        probe = """
import fcntl
import sys
with open(sys.argv[1], "r") as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(10)
"""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            path = host_locks.lock_path("build", target)
            self.addCleanup(path.unlink, missing_ok=True)

            def assert_probe(expected):
                result = subprocess.run(
                    [sys.executable, "-c", probe, str(path)],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                self.assertEqual(result.returncode, expected, result.stderr)

            with host_locks.path_lock("build", target):
                self.assertTrue(path.is_file())
                assert_probe(10)
            assert_probe(0)
            with self.assertRaisesRegex(RuntimeError, "holder failed"):
                with host_locks.path_lock("build", target):
                    assert_probe(10)
                    raise RuntimeError("holder failed")
            assert_probe(0)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_lock_lives_outside_the_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context_dir = Path(tmp) / ".project-sandbox"
            context_dir.mkdir()
            path = host_locks.lock_path("build", context_dir)

            self.assertFalse(
                str(path).startswith(str(Path(tmp).resolve())),
                "a lock inside the project would land in the build context",
            )
            # Private per-user state, not the shared temp dir.
            self.assertEqual(path.parent, paths.state_root_path() / "locks")

    def test_all_host_locks_use_the_shared_helper(self) -> None:
        """One helper keeps every lock symlink-safe; a hand-rolled open() in any
        of these modules would silently opt out of that."""
        seen = []

        def fake_lock(kind, target):
            seen.append((kind, Path(target).name))
            return _noop()

        with patch.object(host_locks, "path_lock", side_effect=fake_lock):
            with build_cache.build_lock(Path("/tmp/ctx")):
                pass
            with jj_workspace._workspace_lock(Path("/tmp/repo")):
                pass

        self.assertEqual(seen, [("build", "ctx"), ("jj-workspace", "repo")])

    def test_non_blocking_lease_refuses_while_held(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "W"
            with host_locks.path_lock("workspace-lease", target, blocking=False):
                # A second open file description conflicts like another process.
                with self.assertRaises(host_locks.LockBusy):
                    with host_locks.path_lock(
                        "workspace-lease", target, blocking=False
                    ):
                        pass
            with host_locks.path_lock("workspace-lease", target, blocking=False):
                pass


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


class HostLockFileTests(TestCase):
    def test_locks_live_in_private_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            path = host_locks.lock_path("build", target)
            self.assertEqual(path.parent, paths.state_root_path() / "locks")
            with host_locks.path_lock("build", target):
                self.assertTrue(path.is_file())
            self.assertEqual(_mode(path.parent), 0o700)
            self.assertEqual(_mode(path), 0o600)

    def _plant(self, target: Path, make) -> Path:
        path = host_locks.lock_path("build", target)
        with paths.open_state_root() as root:
            root.subdir("locks").close()
        make(path)
        return path

    def test_fifo_is_refused_without_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._plant(Path(tmp), os.mkfifo)
            with self.assertRaises(SystemExit) as ctx:
                with host_locks.path_lock("build", Path(tmp)):
                    pass
            self.assertIn(str(path), str(ctx.exception))

    def test_symlink_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            victim = Path(tmp) / "victim"
            victim.write_text("keep", encoding="utf-8")
            path = self._plant(Path(tmp), lambda p: p.symlink_to(victim))
            with self.assertRaises(SystemExit) as ctx:
                with host_locks.path_lock("build", Path(tmp)):
                    pass
            self.assertIn(str(path), str(ctx.exception))
            self.assertEqual(victim.read_text(encoding="utf-8"), "keep")

    def test_foreign_owned_lock_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._plant(Path(tmp), lambda p: p.write_text("", encoding="utf-8"))
            with patch.object(paths, "_uid", return_value=os.geteuid() + 1):
                with patch.object(
                    paths,
                    "_fstat",
                    lambda fd: os.stat_result(
                        (stat.S_IFDIR | 0o700, 0, 0, 0, os.geteuid() + 1, 0, 0, 0, 0, 0)
                    ),
                ):
                    with self.assertRaises(SystemExit) as ctx:
                        with host_locks.path_lock("build", Path(tmp)):
                            pass
            self.assertIn(str(path), str(ctx.exception))
            self.assertTrue(path.is_file())


class ConcurrentFirstLockTests(TestCase):
    def test_concurrent_first_opens_of_one_lock_all_succeed(self) -> None:
        """Regression: macOS can fail a concurrent O_CREAT of one new name with
        ENOENT; every caller must still get the lock, one after another."""
        import threading
        import time

        errors: list[BaseException] = []

        def worker(target: Path) -> None:
            try:
                with host_locks.path_lock("race", target):
                    time.sleep(0.005)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        with tempfile.TemporaryDirectory() as tmp:
            for round_ in range(20):
                target = Path(tmp) / str(round_)
                target.mkdir()
                threads = [
                    threading.Thread(target=worker, args=(target,)) for _ in range(4)
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
        self.assertEqual(errors, [])
