import subprocess
import sys
from pathlib import Path
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import worktree as worktree_mod


def _make_repo(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@test.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True, capture_output=True)
    (path / "a.txt").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], check=True, capture_output=True)


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

        self.assertTrue(wt.created)
        self.assertEqual(wt.branch, "feat/hello")
        self.assertTrue(wt.path.is_dir())
        # worktree's .git should be a file (gitdir pointer)
        git_entry = wt.path / ".git"
        self.assertTrue(git_entry.is_file())
        self.assertIn("gitdir:", git_entry.read_text(encoding="utf-8"))

    def test_setup_default_worktree_dir(self) -> None:
        wt = worktree_mod.setup(self.repo, "feat/hello")

        expected_root = self.repo.resolve().parent / f"{self.repo.name}-worktrees"
        self.assertEqual(wt.path.resolve(), expected_root / "feat-hello")

    def test_setup_custom_worktree_dir(self) -> None:
        custom = self.root / "custom-wts"
        wt = worktree_mod.setup(self.repo, "feat/x", worktree_dir=custom)

        self.assertEqual(wt.path, custom / "feat-x")
        self.assertTrue(wt.path.is_dir())

    def test_setup_idempotent_reuse(self) -> None:
        wt1 = worktree_mod.setup(self.repo, "feat/x")
        wt2 = worktree_mod.setup(self.repo, "feat/x")

        self.assertTrue(wt1.created)
        self.assertFalse(wt2.created)
        self.assertEqual(wt1.path, wt2.path)

    def test_setup_existing_branch(self) -> None:
        subprocess.run(
            ["git", "-C", str(self.repo), "branch", "existing-branch"],
            check=True, capture_output=True,
        )
        wt = worktree_mod.setup(self.repo, "existing-branch")

        self.assertFalse(wt.created)
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

        self.assertTrue(wt.created)
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

    def test_teardown_merge_fast_forwards_main(self) -> None:
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
