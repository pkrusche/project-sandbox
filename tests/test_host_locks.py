import contextlib
import os
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import build_cache, host_locks, jj_workspace, worktree


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
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            path = host_locks.lock_path("build", target)
            with host_locks.path_lock("build", target):
                self.assertTrue(path.is_file())
            # Re-entering must not block once the first holder is done.
            with host_locks.path_lock("build", target):
                pass
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.unlink()

    def test_symlinked_lock_name_is_refused_instead_of_truncated(self) -> None:
        """The lock name is guessable and lives in the shared temp dir, so a
        pre-planted symlink must not redirect the open onto another file."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "repo"
            target.mkdir()
            victim = Path(tmp) / "important.txt"
            victim.write_text("keep me\n", encoding="utf-8")
            path = host_locks.lock_path("build", target)
            path.symlink_to(victim)
            try:
                with self.assertRaisesRegex(SystemExit, "Could not open the build"):
                    with host_locks.path_lock("build", target):
                        pass
                self.assertEqual(victim.read_text(encoding="utf-8"), "keep me\n")
            finally:
                path.unlink()

    def test_lock_lives_outside_the_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context_dir = Path(tmp) / ".project-sandbox"
            context_dir.mkdir()
            path = host_locks.lock_path("build", context_dir)

            self.assertFalse(
                str(path).startswith(str(Path(tmp).resolve())),
                "a lock inside the project would land in the build context",
            )
            self.assertEqual(path.parent, Path(tempfile.gettempdir()))
            self.assertTrue(os.access(path.parent, os.W_OK))

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
            with worktree._teardown_lock(Path("/tmp/repo")):
                pass

        self.assertEqual(
            seen,
            [("build", "ctx"), ("jj-workspace", "repo"), ("git-teardown", "repo")],
        )
