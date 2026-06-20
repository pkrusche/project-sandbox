import contextlib
import io
import subprocess
import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import worktree as worktree_mod


def _make_repo(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@test.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True, capture_output=True)
    (path / "a.txt").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], check=True, capture_output=True)


class PathForCollisionTests(TestCase):
    """Unit tests for path_for() disambiguation — no real git repo required."""

    def _fake_repo(self) -> Path:
        return Path("/tmp/fake/repo")

    def test_slash_and_dash_branch_get_different_paths(self) -> None:
        p_slash = worktree_mod.path_for(self._fake_repo(), "feat/x")
        p_dash = worktree_mod.path_for(self._fake_repo(), "feat-x")
        self.assertNotEqual(p_slash, p_dash)

    def test_plain_branch_has_no_suffix(self) -> None:
        p = worktree_mod.path_for(self._fake_repo(), "main")
        self.assertEqual(p.name, "main")

    def test_plain_dash_branch_has_no_suffix(self) -> None:
        p = worktree_mod.path_for(self._fake_repo(), "my-feature")
        self.assertEqual(p.name, "my-feature")

    def test_slash_branch_ends_with_six_char_hex_suffix(self) -> None:
        import re
        p = worktree_mod.path_for(self._fake_repo(), "feat/x")
        # Expected: "feat-x-<6 hex chars>"
        self.assertRegex(p.name, r"^feat-x-[0-9a-f]{6}$")

    def test_slash_branch_suffix_is_deterministic(self) -> None:
        p1 = worktree_mod.path_for(self._fake_repo(), "feat/x")
        p2 = worktree_mod.path_for(self._fake_repo(), "feat/x")
        self.assertEqual(p1, p2)


class WorktreeSetupTests(TestCase):
    def setUp(self) -> None:
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _make_repo(self.repo)

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_setup_creates_new_branch_and_worktree(self) -> None:
        wt = worktree_mod.setup(self.repo, "feat/hello")

        self.assertEqual(wt.branch, "feat/hello")
        self.assertTrue(wt.path.is_dir())
        # worktree's .git should be a file (gitdir pointer)
        git_entry = wt.path / ".git"
        self.assertTrue(git_entry.is_file())
        self.assertIn("gitdir:", git_entry.read_text(encoding="utf-8"))

    def test_setup_default_worktree_dir(self) -> None:
        wt = worktree_mod.setup(self.repo, "feat/hello")

        expected_root = self.repo.resolve().parent / f"{self.repo.name}-worktrees"
        self.assertEqual(wt.path.resolve(), expected_root / "feat-hello-b316b9")

    def test_setup_custom_worktree_dir(self) -> None:
        custom = self.root / "custom-wts"
        wt = worktree_mod.setup(self.repo, "feat/x", worktree_dir=custom)

        self.assertEqual(wt.path, custom / "feat-x-79b4cc")
        self.assertTrue(wt.path.is_dir())

    def test_setup_idempotent_reuse(self) -> None:
        wt1 = worktree_mod.setup(self.repo, "feat/x")
        wt2 = worktree_mod.setup(self.repo, "feat/x")

        self.assertEqual(wt1.path, wt2.path)

    def test_list_worktrees_handles_paths_with_spaces(self) -> None:
        custom = self.root / "work trees with spaces"
        wt1 = worktree_mod.setup(self.repo, "feat/x", worktree_dir=custom)
        listed = worktree_mod._list_worktrees(self.repo)

        self.assertIn(str(wt1.path.resolve()), listed)
        # Idempotent reuse must recognize the spaced path and not re-add it.
        wt2 = worktree_mod.setup(self.repo, "feat/x", worktree_dir=custom)
        self.assertEqual(wt1.path, wt2.path)

    def test_setup_existing_branch(self) -> None:
        subprocess.run(
            ["git", "-C", str(self.repo), "branch", "existing-branch"],
            check=True, capture_output=True,
        )
        wt = worktree_mod.setup(self.repo, "existing-branch")

        self.assertTrue(wt.path.is_dir())

    def test_setup_respects_base_branch(self) -> None:
        # Record current branch, create a side branch with an extra commit,
        # switch back, then create a worktree from that side branch.
        main_branch = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        subprocess.run(
            ["git", "-C", str(self.repo), "checkout", "-b", "base-branch"],
            check=True, capture_output=True,
        )
        (self.repo / "base.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-m", "base commit"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "checkout", main_branch],
            check=True, capture_output=True,
        )

        wt = worktree_mod.setup(self.repo, "feat/from-base", base="base-branch")

        self.assertTrue((wt.path / "base.txt").exists())


class WorktreeTeardownTests(TestCase):
    def setUp(self) -> None:
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _make_repo(self.repo)
        self.wt = worktree_mod.setup(self.repo, "feat/work")
        # Make a commit in the worktree
        (self.wt.path / "work.txt").write_text("work\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.wt.path), "add", "."], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(self.wt.path), "commit", "-m", "agent work"],
            check=True, capture_output=True,
        )

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_teardown_nothing_leaves_worktree_intact(self) -> None:
        worktree_mod.teardown(self.repo, self.wt, after="nothing")

        self.assertTrue(self.wt.path.is_dir())
        out = subprocess.run(
            ["git", "-C", str(self.repo), "branch", "--list", "feat/work"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn("feat/work", out)

    def test_teardown_merge_brings_branch_into_main(self) -> None:
        worktree_mod.teardown(self.repo, self.wt, after="merge")

        log = subprocess.run(
            ["git", "-C", str(self.repo), "log", "--oneline"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn("agent work", log)
        self.assertFalse(self.wt.path.exists())

    def test_teardown_rebase_replays_onto_main(self) -> None:
        # Add a divergent commit on main so rebase actually has work to do.
        (self.repo / "main.txt").write_text("main\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-m", "main commit"],
            check=True, capture_output=True,
        )

        worktree_mod.teardown(self.repo, self.wt, after="rebase")

        log = subprocess.run(
            ["git", "-C", str(self.repo), "log", "--oneline"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn("agent work", log)
        self.assertIn("main commit", log)
        self.assertFalse(self.wt.path.exists())

    def test_teardown_merge_conflict_leaves_worktree_and_prints_message(self) -> None:
        out = io.StringIO()
        with (
            patch.object(worktree_mod, "_git", side_effect=subprocess.CalledProcessError(1, "git merge")),
            contextlib.redirect_stdout(out),
        ):
            # Should not raise
            worktree_mod.teardown(self.repo, self.wt, after="merge")

        self.assertTrue(self.wt.path.is_dir(), "worktree must remain after conflict")
        message = out.getvalue()
        self.assertIn("merge conflict", message)
        self.assertIn(str(self.wt.path), message)

    def test_teardown_rebase_conflict_leaves_worktree_and_prints_message(self) -> None:
        out = io.StringIO()
        real_run = subprocess.run

        def mock_run(cmd, **kwargs):
            # Match the direct rebase call (not --abort) by checking element membership.
            if "rebase" in cmd and "--abort" not in cmd:
                raise subprocess.CalledProcessError(1, cmd)
            return real_run(["true"], **{k: v for k, v in kwargs.items() if k != "check"})

        with (
            patch("subprocess.run", side_effect=mock_run),
            contextlib.redirect_stdout(out),
        ):
            worktree_mod.teardown(self.repo, self.wt, after="rebase")

        self.assertTrue(self.wt.path.is_dir(), "worktree must remain after conflict")
        message = out.getvalue()
        self.assertIn("merge conflict", message)
        self.assertIn(str(self.wt.path), message)

    def test_teardown_pr_runs_gh_in_repo_and_preserves_worktree_on_success(self) -> None:
        real_run = subprocess.run
        calls = []

        def mock_run(cmd, **kwargs):
            if cmd[:1] == ["gh"]:
                calls.append((cmd, kwargs))
                return subprocess.CompletedProcess(cmd, 0)
            if "push" in cmd:
                # No 'origin' remote in the test repo; stub a successful push.
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return real_run(cmd, **kwargs)

        with patch("subprocess.run", side_effect=mock_run):
            worktree_mod.teardown(self.repo, self.wt, after="pr")

        self.assertEqual(len(calls), 1, "gh pr create should be invoked exactly once")
        cmd, kwargs = calls[0]
        self.assertEqual(cmd, ["gh", "pr", "create", "--head", self.wt.branch, "--fill"])
        self.assertEqual(kwargs.get("cwd"), str(self.repo))
        self.assertTrue(kwargs.get("check"))
        # pr teardown never removes the worktree
        self.assertTrue(self.wt.path.is_dir())

    def test_teardown_pr_failure_leaves_worktree_and_prints_message(self) -> None:
        out = io.StringIO()
        real_run = subprocess.run

        def mock_run(cmd, **kwargs):
            if cmd[:1] == ["gh"]:
                raise subprocess.CalledProcessError(1, cmd)
            if "push" in cmd:
                # No 'origin' remote in the test repo; stub a successful push.
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return real_run(cmd, **kwargs)

        with (
            patch("subprocess.run", side_effect=mock_run),
            contextlib.redirect_stdout(out),
        ):
            # A failing gh pr create must be surfaced, not swallowed silently.
            worktree_mod.teardown(self.repo, self.wt, after="pr")

        self.assertTrue(self.wt.path.is_dir(), "worktree must remain after PR failure")
        message = out.getvalue()
        self.assertIn("gh pr create", message)
        self.assertIn(str(self.wt.path), message)

    def test_teardown_clears_stale_index_lock(self) -> None:
        # Simulate a stale lock left by a crashed container.
        git_dir = self.repo.resolve() / ".git"
        wt_name = self.wt.path.name
        lock = git_dir / "worktrees" / wt_name / "index.lock"
        lock.write_text("locked\n", encoding="utf-8")

        # merge should still succeed after clearing the lock
        worktree_mod.teardown(self.repo, self.wt, after="merge")

        self.assertFalse(lock.exists())
        log = subprocess.run(
            ["git", "-C", str(self.repo), "log", "--oneline"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn("agent work", log)


class WorktreeSetupStaleDirectoryTests(TestCase):
    def setUp(self) -> None:
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _make_repo(self.repo)

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_setup_stale_unregistered_directory_raises_system_exit(self) -> None:
        # Create the directory that setup would use, but do NOT register it as a worktree.
        wt_path = worktree_mod.path_for(self.repo, "feat/stale")
        wt_path.mkdir(parents=True)
        (wt_path / "stray.txt").write_text("leftover\n", encoding="utf-8")

        with self.assertRaises(SystemExit) as raised:
            worktree_mod.setup(self.repo, "feat/stale")

        msg = str(raised.exception)
        self.assertIn(str(wt_path), msg)
        self.assertIn("not registered", msg)
        # Directory must NOT be deleted automatically
        self.assertTrue(wt_path.is_dir())
