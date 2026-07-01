import contextlib
import io
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

def _find_docker() -> str | None:
    for cmd in ("docker", "podman"):
        path = shutil.which(cmd)
        if path is None:
            continue
        try:
            if subprocess.run([path, "info"], capture_output=True, timeout=5).returncode == 0:
                return path
        except (subprocess.TimeoutExpired, OSError):
            continue
    return None

DOCKER = _find_docker()
DOCKER_IMAGE = "alpine/git:v2.45.2"

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

    def test_setup_existing_branch_reused(self) -> None:
        subprocess.run(
            ["git", "-C", str(self.repo), "branch", "existing-branch"],
            check=True, capture_output=True,
        )
        wt = worktree_mod.setup(self.repo, "existing-branch")

        self.assertTrue(wt.path.is_dir())

    def test_setup_start_at_on_existing_branch_raises(self) -> None:
        subprocess.run(
            ["git", "-C", str(self.repo), "branch", "existing-branch"],
            check=True, capture_output=True,
        )
        with self.assertRaises(SystemExit) as raised:
            worktree_mod.setup(self.repo, "existing-branch", start_at="HEAD")

        msg = str(raised.exception)
        self.assertIn("existing-branch", msg)
        self.assertIn("already exists", msg)

    def test_setup_respects_start_at(self) -> None:
        # Record current branch, create a side branch with an extra commit,
        # switch back, then create a worktree starting at that side branch.
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

        wt = worktree_mod.setup(self.repo, "feat/from-base", start_at="base-branch")

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

    def _finalize(self, *, keep_workspace=False, session_failed=False, message="msg"):
        worktree_mod.finalize(
            self.repo,
            self.wt,
            keep_workspace=keep_workspace,
            session_failed=session_failed,
            message=message,
        )

    def _main_log(self) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), "log", "--oneline"],
            capture_output=True, text=True, check=True,
        ).stdout

    def _branch_log(self) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), "log", "--oneline", "feat/work"],
            capture_output=True, text=True, check=True,
        ).stdout

    def test_finalize_removes_worktree_but_keeps_branch(self) -> None:
        self._finalize()

        # Worktree gone, branch retained with the agent's commit.
        self.assertFalse(self.wt.path.exists())
        self.assertIn("agent work", self._branch_log())
        # Never integrated into the main checkout.
        self.assertNotIn("agent work", self._main_log())

    def test_finalize_commit_failure_leaves_worktree_without_raising(self) -> None:
        # finalize runs from main()'s finally block: a failed host-side commit
        # must be reported, not raised (which would mask the session exit code).
        (self.wt.path / "uncommitted.txt").write_text("later\n", encoding="utf-8")
        out = io.StringIO()
        real_git = worktree_mod._git

        def failing_git(repo, args, capture=False):
            if args[:1] == ["commit"]:
                raise subprocess.CalledProcessError(1, "git commit")
            return real_git(repo, args, capture=capture)

        with (
            patch.object(worktree_mod, "_git", side_effect=failing_git),
            contextlib.redirect_stdout(out),
        ):
            self._finalize()  # must not raise

        self.assertTrue(self.wt.path.is_dir(), "worktree kept so work is not lost")
        self.assertIn("could not commit", out.getvalue())

    def test_finalize_keep_workspace_leaves_worktree(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self._finalize(keep_workspace=True)

        self.assertTrue(self.wt.path.is_dir())
        self.assertIn("kept", out.getvalue())
        self.assertIn("agent work", self._branch_log())

    def test_finalize_session_failed_leaves_worktree(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self._finalize(session_failed=True)

        self.assertTrue(self.wt.path.is_dir())
        self.assertIn("failed", out.getvalue())

    def test_finalize_commits_uncommitted_work_before_removal(self) -> None:
        # Leave dirty, uncommitted changes in the worktree; finalize must commit
        # them onto the branch so the forced removal cannot discard them.
        (self.wt.path / "uncommitted.txt").write_text("later\n", encoding="utf-8")

        self._finalize(message="session — 2026-07-01T09:10")

        self.assertFalse(self.wt.path.exists())
        branch_log = self._branch_log()
        self.assertIn("session — 2026-07-01T09:10", branch_log)
        # The file is present in the branch tip tree.
        files = subprocess.run(
            ["git", "-C", str(self.repo), "ls-tree", "-r", "--name-only", "feat/work"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn("uncommitted.txt", files)

    def test_finalize_clears_stale_index_lock(self) -> None:
        # Simulate a stale lock left by a crashed container, plus dirty changes so
        # finalize needs the index to commit.
        (self.wt.path / "uncommitted.txt").write_text("later\n", encoding="utf-8")
        git_dir = self.repo.resolve() / ".git"
        wt_name = self.wt.path.name
        lock = git_dir / "worktrees" / wt_name / "index.lock"
        lock.write_text("locked\n", encoding="utf-8")

        self._finalize()

        self.assertFalse(lock.exists())
        self.assertIn("uncommitted.txt", self._branch_log_files())

    def _branch_log_files(self) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), "ls-tree", "-r", "--name-only", "feat/work"],
            capture_output=True, text=True, check=True,
        ).stdout


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


@unittest.skipUnless(DOCKER, "docker/podman not available")
class GitWorktreeDockerEndToEndTests(TestCase):
    """Container writes a file+commit via bash; host verifies tree and revision, then teardown."""

    def setUp(self) -> None:
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _make_repo(self.repo)

    def tearDown(self) -> None:
        self._td.cleanup()

    def _docker(self, wt_path: Path, bash_cmd: str) -> None:
        git_dir = str((self.repo / ".git").resolve())
        uid = os.getuid()
        subprocess.run(
            [
                DOCKER, "run", "--rm", "-u", str(uid),
                "--mount", f"type=bind,source={wt_path},target=/workspace",
                "--mount", f"type=bind,source={git_dir},target={git_dir}",
                "--workdir", "/workspace",
                "--entrypoint", "sh",
                DOCKER_IMAGE,
                "-c", bash_cmd,
            ],
            check=True,
        )

    def test_container_adds_file_tree_and_revision_visible_on_host(self) -> None:
        # 1. Create worktree
        wt = worktree_mod.setup(self.repo, "feat/e2e")

        # 2. Container adds a file and commits via bash
        self._docker(
            wt.path,
            "export HOME=/tmp && "
            "git config --global --add safe.directory /workspace && "
            "git config user.email t@test.com && "
            "git config user.name Test && "
            "echo 'hello from container' > agent_output.txt && "
            "git add . && "
            "git commit -m 'agent: add agent_output'",
        )

        # 3. Show tree — file present on host
        self.assertTrue((wt.path / "agent_output.txt").exists())
        tree_out = subprocess.run(
            ["find", str(wt.path), "-not", "-path", f"{wt.path}/.git*", "-type", "f"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn("agent_output.txt", tree_out)

        # 4. Show revision — commit in branch log
        log_out = subprocess.run(
            ["git", "-C", str(wt.path), "log", "--oneline"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn("agent: add agent_output", log_out)

    def test_container_commit_survives_finalize(self) -> None:
        wt = worktree_mod.setup(self.repo, "feat/e2e-finalize")
        self._docker(
            wt.path,
            "export HOME=/tmp && "
            "git config --global --add safe.directory /workspace && "
            "git config user.email t@test.com && "
            "git config user.name Test && "
            "echo 'hello from container' > agent_output.txt && "
            "git add . && "
            "git commit -m 'agent: add file'",
        )

        worktree_mod.finalize(
            self.repo, wt, keep_workspace=False, session_failed=False, message="msg"
        )

        self.assertFalse(wt.path.exists())  # worktree removed
        # The commit lives on the branch, not on the main checkout.
        branch_log = subprocess.run(
            ["git", "-C", str(self.repo), "log", "--oneline", "feat/e2e-finalize"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn("agent: add file", branch_log)
        main_log = subprocess.run(
            ["git", "-C", str(self.repo), "log", "--oneline"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertNotIn("agent: add file", main_log)
