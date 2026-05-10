import contextlib
import io
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import cli
from project_sandbox.git_identity import GitIdentity
from project_sandbox.worktree import Worktree


def _make_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@test.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True, capture_output=True)
    (path / "README.md").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], check=True, capture_output=True)


class CliTests(TestCase):
    def test_help_includes_core_options(self) -> None:
        parser = cli.build_parser()
        with self.assertRaises(SystemExit) as raised, contextlib.redirect_stdout(io.StringIO()) as stdout:
            parser.parse_args(["--help"])

        self.assertEqual(raised.exception.code, 0)
        help_text = stdout.getvalue()
        self.assertIn("--dry-run", help_text)
        self.assertIn("--branch", help_text)

    def test_dry_run_does_not_write_project_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "README.md").write_text("# demo\n", encoding="utf-8")

            with patch.object(cli, "read_identity", return_value=GitIdentity("Ada", "ada@example.com")):
                rc = cli.main(["--dry-run", "--no-build", str(project), "python:3.12-slim"])

            self.assertEqual(rc, 0)
            self.assertFalse((project / ".project-sandbox").exists())
            self.assertFalse((project / ".gitignore").exists())

    def test_branch_jj_repo_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".jj").mkdir()
            with patch.object(cli, "read_identity", return_value=GitIdentity("A", "a@b.com")):
                with self.assertRaises(SystemExit) as raised:
                    cli.main(["--branch", "feat/x", str(project), "python:3.12-slim"])
        self.assertIn("jj", str(raised.exception).lower())

    def test_branch_file_git_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".git").write_text("gitdir: ../some/.git/worktrees/x\n", encoding="utf-8")
            with patch.object(cli, "read_identity", return_value=GitIdentity("A", "a@b.com")):
                with self.assertRaises(SystemExit) as raised:
                    cli.main(["--branch", "feat/x", str(project), "python:3.12-slim"])
        self.assertIn("plain git repo", str(raised.exception))

    def test_branch_dry_run_argv_includes_git_metadata_mount(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _make_git_repo(project)

            wt_path = project.parent / f"{project.name}-worktrees" / "feat-x"
            fake_wt = Worktree(path=wt_path, branch="feat/x", created=True)

            stdout_buf = io.StringIO()
            with (
                patch.object(cli, "read_identity", return_value=GitIdentity("A", "a@b.com")),
                patch.object(cli.worktree_mod, "setup", return_value=fake_wt),
                contextlib.redirect_stdout(stdout_buf),
            ):
                rc = cli.main([
                    "--dry-run", "--no-build", "--no-firewall",
                    "--branch", "feat/x", "--after-session", "nothing",
                    str(project), "python:3.12-slim",
                ])

        self.assertEqual(rc, 0)
        output = stdout_buf.getvalue()

        git_dir = str((project / ".git").resolve())
        # The container run argv line should contain the .git metadata mount
        self.assertIn(git_dir, output)
        # And the workspace mount should point at the worktree, not the project root
        self.assertIn(str(wt_path), output)
        self.assertNotIn(f"source={project},target=/workspace", output)

    def test_branch_dry_run_prints_worktree_info(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _make_git_repo(project)

            wt_path = project.parent / f"{project.name}-worktrees" / "feat-x"
            fake_wt = Worktree(path=wt_path, branch="feat/x", created=True)

            stdout_buf = io.StringIO()
            with (
                patch.object(cli, "read_identity", return_value=GitIdentity("A", "a@b.com")),
                patch.object(cli.worktree_mod, "setup", return_value=fake_wt),
                contextlib.redirect_stdout(stdout_buf),
            ):
                cli.main([
                    "--dry-run", "--no-build", "--no-firewall",
                    "--branch", "feat/x", "--after-session", "nothing",
                    str(project), "python:3.12-slim",
                ])

        output = stdout_buf.getvalue()
        self.assertIn("Would create worktree at:", output)
        self.assertIn("Would mount .git metadata:", output)

    def test_after_session_ask_unsupervised_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # Validation fires before resolve_strict, so no git repo needed.
            with self.assertRaises(SystemExit) as raised:
                cli.main([
                    "--branch", "feat/x",
                    "--prompt-text", "do something",
                    tmp, "python:3.12-slim",
                ])
        self.assertIn("ask", str(raised.exception).lower())
        self.assertIn("unsupervised", str(raised.exception).lower())
