import contextlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import build_cache, host_locks, jj_workspace, worktree

# conftest patches ``locks_root`` per test; keep the real one to test it.
_REAL_LOCKS_ROOT = host_locks.locks_root


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

    def test_symlinked_lock_name_is_refused_instead_of_truncated(self) -> None:
        """The lock name is guessable from a project path, so a symlink planted
        at it must not redirect the open onto another file."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "repo"
            target.mkdir()
            victim = Path(tmp) / "important.txt"
            victim.write_text("keep me\n", encoding="utf-8")
            path = host_locks.lock_path("build", target)
            path.parent.mkdir(mode=0o700)
            path.symlink_to(victim)
            try:
                with self.assertRaisesRegex(SystemExit, "Could not open the build"):
                    with host_locks.path_lock("build", target):
                        pass
                self.assertEqual(victim.read_text(encoding="utf-8"), "keep me\n")
            finally:
                path.unlink()

    def test_non_regular_lock_file_is_refused(self) -> None:
        target = Path(".")
        with host_locks.path_lock("build", target):
            pass
        path = host_locks.lock_path("build", target)
        path.unlink()
        os.mkfifo(path, 0o600)
        with self.assertRaisesRegex(SystemExit, "not a regular file"):
            with host_locks.path_lock("build", target):
                self.fail("entered a lock held on a FIFO")

    def test_lock_lives_outside_the_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context_dir = Path(tmp) / ".project-sandbox"
            context_dir.mkdir()
            path = host_locks.lock_path("build", context_dir)

            self.assertFalse(
                str(path).startswith(str(Path(tmp).resolve())),
                "a lock inside the project would land in the build context",
            )
            self.assertEqual(path.parent, host_locks.locks_root())

    def test_locks_root_is_under_the_state_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp).resolve()
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(state)}):
                self.assertEqual(
                    _REAL_LOCKS_ROOT(), state / "project-sandbox" / "locks"
                )
            for value in ("", "relative/state"):
                with patch.dict(os.environ, {"XDG_STATE_HOME": value}):
                    self.assertEqual(
                        _REAL_LOCKS_ROOT(),
                        Path.home() / ".local/state/project-sandbox/locks",
                    )

    def test_missing_lock_directory_is_created_private(self) -> None:
        root = host_locks.locks_root()
        self.assertFalse(root.exists())
        previous = os.umask(0o022)
        try:
            with host_locks.path_lock("build", Path(".")):
                pass
        finally:
            os.umask(previous)
        self.assertEqual(root.stat().st_mode & 0o777, 0o700)
        lock = host_locks.lock_path("build", Path("."))
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)

    def test_no_lock_is_created_in_the_temp_dir(self) -> None:
        temp = Path(tempfile.gettempdir())
        before = set(temp.glob("project-sandbox-*.lock"))
        with tempfile.TemporaryDirectory() as tmp:
            with host_locks.path_lock("build", Path(tmp)):
                pass
            path = host_locks.lock_path("build", Path(tmp))
        self.assertTrue(path.is_file())
        self.assertEqual(set(temp.glob("project-sandbox-*.lock")), before)

    def test_symlinked_lock_directory_is_refused(self) -> None:
        root = host_locks.locks_root()
        elsewhere = root.parent / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        root.symlink_to(elsewhere)
        with self.assertRaisesRegex(SystemExit, str(root)):
            with host_locks.path_lock("build", Path(".")):
                pass
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_shared_lock_directory_is_refused_and_left_alone(self) -> None:
        root = host_locks.locks_root()
        root.mkdir()
        os.chmod(root, 0o770)
        with self.assertRaisesRegex(SystemExit, str(root)):
            with host_locks.path_lock("build", Path(".")):
                pass
        self.assertEqual(root.stat().st_mode & 0o777, 0o770)
        self.assertEqual(list(root.iterdir()), [])

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
            with worktree._workspace_lock(Path("/tmp/repo")):
                pass

        self.assertEqual(
            seen,
            [("build", "ctx"), ("jj-workspace", "repo"), ("git-teardown", "repo")],
        )
