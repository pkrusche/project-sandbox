"""Workspace ownership must outlive setup and survive competing launches."""

import contextlib
import fcntl
import io
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from test_jj_workspace import _make_jj_repo
from test_worktree import _make_repo

from project_sandbox import host_locks, jj_workspace, worktree
from project_sandbox.workspace_lease import WorkspaceLease


class WorkspaceLeaseTests(TestCase):
    def test_overlapping_processes_and_stale_finalizers(self) -> None:
        for backend, init in ((worktree, _make_repo), (jj_workspace, _make_jj_repo)):
            if backend is jj_workspace and not shutil.which("jj"):
                continue
            with (
                self.subTest(backend=backend.__name__),
                tempfile.TemporaryDirectory() as tmp,
            ):
                root = Path(tmp)
                repo = root / "repo"
                init(repo)
                alias = root / "alias"
                alias.symlink_to(repo, target_is_directory=True)
                first = backend.setup(repo, "feature")
                self.addCleanup(first.lease.close)
                (first.path / "agent.txt").write_text("in progress\n")
                # A fresh interpreter exercises actual host lock contention,
                # including alternate repository spelling and workspace root.
                script = """
import sys
from pathlib import Path
from project_sandbox import worktree, jj_workspace
backend = worktree if sys.argv[1] == "git" else jj_workspace
kwargs = {}
if len(sys.argv) > 3:
    kwargs["worktree_dir" if backend is worktree else "workspace_dir"] = Path(sys.argv[3])
backend.setup(Path(sys.argv[2]), "feature", **kwargs)
"""
                for extra in ([], [str(root / "alternate")]):
                    result = subprocess.run(
                        [
                            sys.executable,
                            "-c",
                            script,
                            "git" if backend is worktree else "jj",
                            str(alias),
                            *extra,
                        ],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("already in use", result.stderr)
                    self.assertEqual(
                        (first.path / "agent.txt").read_text(), "in progress\n"
                    )
                    self.assertFalse((root / "alternate").exists())
                # Other branches can run concurrently in the same repository.
                other = backend.setup(repo, "other")
                backend.finalize(
                    repo,
                    other,
                    keep_workspace=False,
                    session_failed=False,
                    message="other",
                )
                self.assertFalse(other.path.exists())
                backend.finalize(
                    repo,
                    first,
                    keep_workspace=True,
                    session_failed=False,
                    message="first",
                )
                second = backend.setup(repo, "feature")
                self.addCleanup(second.lease.close)
                # A delayed/repeated cleanup must not mutate a new owner's files.
                (second.path / "later.txt").write_text("second session\n")
                backend.finalize(
                    repo,
                    first,
                    keep_workspace=False,
                    session_failed=False,
                    message="stale",
                )
                if backend is jj_workspace:
                    backend.remove(repo, first)
                self.assertTrue((second.path / "later.txt").exists())
                backend.finalize(
                    repo,
                    second,
                    keep_workspace=False,
                    session_failed=False,
                    message="second",
                )
                self.assertFalse(second.path.exists())

    def test_directory_alias_conflicts_even_with_different_ref_or_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "workspace"
            path.mkdir()
            alias = root / "alias"
            alias.symlink_to(path, target_is_directory=True)
            with WorkspaceLease("git", root, "first", path):
                with self.assertRaisesRegex(SystemExit, "already in use"):
                    WorkspaceLease("jj", root / "other-repo", "second", alias)
            # The partially acquired ref lease was released on failure too.
            with WorkspaceLease("jj", root / "other-repo", "second", alias):
                pass

    def test_setup_failures_release_lease_for_retry(self) -> None:
        for backend in (worktree, jj_workspace):
            with (
                self.subTest(backend=backend.__name__),
                tempfile.TemporaryDirectory() as tmp,
            ):
                repo = Path(tmp)
                with patch.object(
                    backend, "_setup_locked", side_effect=RuntimeError("failed")
                ):
                    for _ in range(2):
                        with self.assertRaisesRegex(RuntimeError, "failed"):
                            backend.setup(repo, "feature")

    def test_git_setup_and_finalize_hold_same_mutation_lock_and_session_lease(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _make_repo(repo)
            real_git = worktree._git
            calls = []

            def checked_git(path, args, capture=False):
                with host_locks.lock_path("git-teardown", repo).open() as handle:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(SystemExit, "already in use"):
                    worktree.setup(repo, "feature")
                calls.append(args)
                return real_git(path, args, capture=capture)

            with patch.object(worktree, "_git", side_effect=checked_git):
                wt = worktree.setup(repo, "feature")
                worktree.finalize(
                    repo, wt, keep_workspace=False, session_failed=False, message="done"
                )
            self.assertTrue(any(args[:2] == ["worktree", "add"] for args in calls))
            self.assertTrue(any(args[:2] == ["worktree", "remove"] for args in calls))
            self.assertFalse(wt.lease.active)

    def test_git_lock_uses_actual_metadata_path_when_basename_collides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _make_repo(repo)
            worktree._git(
                repo,
                ["worktree", "add", "-b", "other", str(root / "other" / "feature")],
            )
            wt = worktree.setup(repo, "feature")
            metadata = Path(
                worktree._git(
                    wt.path, ["rev-parse", "--absolute-git-dir"], capture=True
                ).strip()
            )
            self.assertNotEqual(metadata.name, wt.path.name)
            lock = metadata / "index.lock"
            lock.write_text("owned externally\n")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                worktree.finalize(
                    repo, wt, keep_workspace=False, session_failed=False, message="done"
                )
            self.assertTrue(wt.path.is_dir())
            self.assertEqual(lock.read_text(), "owned externally\n")
            self.assertIn(str(lock), out.getvalue())
            with self.assertRaisesRegex(SystemExit, "Git index lock exists"):
                worktree.setup(repo, "feature")
