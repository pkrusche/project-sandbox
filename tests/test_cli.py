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


def _agent_paths(home: Path) -> dict[str, Path]:
    return {
        "claude": home / ".claude",
        "codex": home / ".codex",
        "opencode": home / ".config" / "opencode",
    }


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
        self.assertIn("--dockerfile", help_text)
        self.assertNotIn("--rebuild", help_text)
        self.assertNotIn("--refresh-config", help_text)
        self.assertIn("bash", help_text)

    def test_refresh_flags_are_removed(self) -> None:
        parser = cli.build_parser()

        for flag in ("--rebuild", "--refresh-config"):
            with self.subTest(flag=flag):
                with (
                    self.assertRaises(SystemExit),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    parser.parse_args([flag, "/tmp/project", "python:3.12-slim"])

    def test_dry_run_does_not_write_project_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "README.md").write_text("# demo\n", encoding="utf-8")
            host_home = project / "home"
            paths = _agent_paths(host_home)
            paths["claude"].mkdir(parents=True)

            with (
                patch.object(cli, "read_identity", return_value=GitIdentity("Ada", "ada@example.com")),
                patch.object(cli.config_agents, "_agent_host_paths", return_value=paths),
            ):
                rc = cli.main(["--dry-run", "--no-build", str(project), "python:3.12-slim"])

            self.assertEqual(rc, 0)
            self.assertFalse((project / ".project-sandbox").exists())
            self.assertFalse((project / ".gitignore").exists())

    def test_default_run_initializes_files_without_starting_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "README.md").write_text("# demo\n", encoding="utf-8")
            host_home = project / "home"
            paths = _agent_paths(host_home)
            paths["claude"].mkdir(parents=True)
            out = io.StringIO()

            with (
                patch.object(cli, "read_identity", return_value=GitIdentity("Ada", "ada@example.com")),
                patch.object(cli.config_agents, "_agent_host_paths", return_value=paths),
                patch.object(cli.config_agents, "sync_credentials"),
                patch.object(cli.container_cli, "ensure_system_started") as ensure_system_started,
                patch.object(cli.container_cli, "build_image") as build_image,
                patch.object(cli.container_cli, "run") as run,
                contextlib.redirect_stdout(out),
            ):
                rc = cli.main([str(project), "python:3.12-slim"])

            self.assertEqual(rc, 0)
            self.assertTrue((project / ".project-sandbox" / "Dockerfile").exists())
            self.assertTrue((project / ".devcontainer" / "devcontainer.json").exists())
            self.assertIn("project-sandbox ready", out.getvalue())
            ensure_system_started.assert_not_called()
            build_image.assert_not_called()
            run.assert_not_called()

    def test_default_run_overwrites_existing_generated_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "README.md").write_text("# demo\n", encoding="utf-8")
            sandbox = project / ".project-sandbox"
            sandbox.mkdir()
            (sandbox / "Dockerfile").write_text("FROM old:image\n", encoding="utf-8")
            (sandbox / "entrypoint.sh").write_text(
                "#!/bin/sh\necho old\n",
                encoding="utf-8",
            )
            (sandbox / "project-sandbox-devcontainer-init").write_text(
                "#!/bin/sh\necho old\n",
                encoding="utf-8",
            )
            (sandbox / "claude").mkdir()
            (sandbox / "claude" / "settings.json").write_text(
                '{"theme":"dark"}\n',
                encoding="utf-8",
            )
            (sandbox / "codex").mkdir()
            (sandbox / "codex" / "config.toml").write_text(
                "old = true\n",
                encoding="utf-8",
            )
            dc_dir = project / ".devcontainer"
            dc_dir.mkdir()
            (dc_dir / "devcontainer.json").write_text(
                '{"old":true}\n',
                encoding="utf-8",
            )
            host_home = project / "home"
            paths = _agent_paths(host_home)
            paths["claude"].mkdir(parents=True)

            with (
                patch.object(cli, "read_identity", return_value=GitIdentity("Ada", "ada@example.com")),
                patch.object(cli.config_agents, "_agent_host_paths", return_value=paths),
                patch.object(cli.config_agents, "sync_credentials"),
            ):
                rc = cli.main([str(project), "python:3.12-slim"])

            self.assertEqual(rc, 0)
            self.assertIn(
                "FROM python:3.12-slim",
                (sandbox / "Dockerfile").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "project-sandbox-run",
                (sandbox / "entrypoint.sh").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "devcontainer init complete",
                (sandbox / "project-sandbox-devcontainer-init").read_text(
                    encoding="utf-8"
                ),
            )
            self.assertIn(
                '"theme": "auto"',
                (sandbox / "claude" / "settings.json").read_text(encoding="utf-8"),
            )
            self.assertIn(
                'approval_policy = "never"',
                (sandbox / "codex" / "config.toml").read_text(encoding="utf-8"),
            )
            self.assertIn(
                '"remoteUser": "agent"',
                (dc_dir / "devcontainer.json").read_text(encoding="utf-8"),
            )

    def test_bash_agent_is_available_without_host_agent_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "README.md").write_text("# demo\n", encoding="utf-8")
            out = io.StringIO()

            with (
                patch.object(cli, "read_identity", return_value=GitIdentity("Ada", "ada@example.com")),
                patch.object(cli.config_agents, "_agent_host_paths", return_value=_agent_paths(project / "home")),
                contextlib.redirect_stdout(out),
            ):
                rc = cli.main([
                    "--dry-run",
                    "--no-build",
                    "--no-firewall",
                    "--agent",
                    "bash",
                    str(project),
                    "python:3.12-slim",
                ])

            self.assertEqual(rc, 0)
            output = out.getvalue()
            self.assertIn("project-sandbox-run bash", output)
            self.assertNotIn("Would write launcher scripts", output)

    def test_dry_run_accepts_dockerfile_without_base_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "README.md").write_text("# demo\n", encoding="utf-8")
            source = project / "Dockerfile"
            source.write_text("FROM python:3.12-slim\n", encoding="utf-8")
            host_home = project / "home"
            paths = _agent_paths(host_home)
            paths["claude"].mkdir(parents=True)
            out = io.StringIO()

            with (
                patch.object(cli, "read_identity", return_value=GitIdentity("Ada", "ada@example.com")),
                patch.object(cli.config_agents, "_agent_host_paths", return_value=paths),
                contextlib.redirect_stdout(out),
            ):
                rc = cli.main([
                    "--dry-run",
                    "--no-build",
                    "--dockerfile",
                    str(source),
                    str(project),
                ])

            self.assertEqual(rc, 0)
            output = out.getvalue()
            self.assertIn(
                f"Would append sandbox layers to Dockerfile: {source.resolve()}",
                output,
            )
            self.assertIn(f"Would use build context: {project.resolve()}", output)
            self.assertFalse((project / ".project-sandbox").exists())

    def test_dry_run_warns_when_source_dockerfile_user_setup_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "README.md").write_text("# demo\n", encoding="utf-8")
            source = project / "Dockerfile"
            source.write_text(
                "FROM python:3.12-slim\n"
                "RUN useradd -m -u 1000 app\n"
                "USER app\n",
                encoding="utf-8",
            )
            host_home = project / "home"
            paths = _agent_paths(host_home)
            paths["claude"].mkdir(parents=True)
            out = io.StringIO()

            with (
                patch.object(cli, "read_identity", return_value=GitIdentity("Ada", "ada@example.com")),
                patch.object(cli.config_agents, "_agent_host_paths", return_value=paths),
                contextlib.redirect_stdout(out),
            ):
                rc = cli.main([
                    "--dry-run",
                    "--no-build",
                    "--dockerfile",
                    str(source),
                    str(project),
                ])

            self.assertEqual(rc, 0)
            output = out.getvalue()
            self.assertIn("WARNING: Removed 2 restricted user setup instructions", output)
            self.assertIn("project-sandbox will create its own agent user with UID 1000", output)
            self.assertFalse((project / ".project-sandbox").exists())

    def test_dockerfile_and_base_image_are_mutually_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = project / "Dockerfile"
            source.write_text("FROM python:3.12-slim\n", encoding="utf-8")

            with self.assertRaises(SystemExit) as raised:
                cli.main([
                    "--dry-run",
                    "--dockerfile",
                    str(source),
                    str(project),
                    "python:3.12-slim",
                ])

            self.assertIn("either base_image or --dockerfile", str(raised.exception))

    def test_branch_jj_repo_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".jj").mkdir()
            with (
                patch.object(cli, "read_identity", return_value=GitIdentity("A", "a@b.com")),
                patch.object(cli.config_agents, "_agent_host_paths", return_value=_agent_paths(project / "home")),
            ):
                with self.assertRaises(SystemExit) as raised:
                    cli.main([
                        "--agent",
                        "claude",
                        "--branch",
                        "feat/x",
                        str(project),
                        "python:3.12-slim",
                    ])
        self.assertIn("jj", str(raised.exception).lower())

    def test_branch_file_git_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".git").write_text("gitdir: ../some/.git/worktrees/x\n", encoding="utf-8")
            with (
                patch.object(cli, "read_identity", return_value=GitIdentity("A", "a@b.com")),
                patch.object(cli.config_agents, "_agent_host_paths", return_value=_agent_paths(project / "home")),
            ):
                with self.assertRaises(SystemExit) as raised:
                    cli.main([
                        "--agent",
                        "claude",
                        "--branch",
                        "feat/x",
                        str(project),
                        "python:3.12-slim",
                    ])
        self.assertIn("plain git repo", str(raised.exception))

    def test_branch_dry_run_argv_includes_git_metadata_mount(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _make_git_repo(project)

            wt_path = project.parent / f"{project.name}-worktrees" / "feat-x"
            host_home = project / "home"
            paths = _agent_paths(host_home)
            paths["claude"].mkdir(parents=True)

            stdout_buf = io.StringIO()
            with (
                patch.object(cli, "read_identity", return_value=GitIdentity("A", "a@b.com")),
                patch.object(cli.worktree_mod, "setup") as setup_worktree,
                patch.object(cli.config_agents, "_agent_host_paths", return_value=paths),
                contextlib.redirect_stdout(stdout_buf),
            ):
                rc = cli.main([
                    "--dry-run", "--no-build", "--no-firewall",
                    "--agent", "claude",
                    "--branch", "feat/x", "--after-session", "nothing",
                    str(project), "python:3.12-slim",
                ])

        self.assertEqual(rc, 0)
        setup_worktree.assert_not_called()
        self.assertFalse(wt_path.exists())
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
            host_home = project / "home"
            paths = _agent_paths(host_home)
            paths["claude"].mkdir(parents=True)

            stdout_buf = io.StringIO()
            with (
                patch.object(cli, "read_identity", return_value=GitIdentity("A", "a@b.com")),
                patch.object(cli.worktree_mod, "setup") as setup_worktree,
                patch.object(cli.config_agents, "_agent_host_paths", return_value=paths),
                contextlib.redirect_stdout(stdout_buf),
            ):
                cli.main([
                    "--dry-run", "--no-build", "--no-firewall",
                    "--agent", "claude",
                    "--branch", "feat/x", "--after-session", "nothing",
                    str(project), "python:3.12-slim",
                ])

        output = stdout_buf.getvalue()
        setup_worktree.assert_not_called()
        self.assertFalse(wt_path.exists())
        self.assertIn("Would use worktree at:", output)
        self.assertIn("Would mount .git metadata:", output)

    def test_branch_without_agent_or_prompt_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _make_git_repo(project)

            with (
                patch.object(cli, "read_identity", return_value=GitIdentity("A", "a@b.com")),
                patch.object(cli.config_agents, "_agent_host_paths", return_value=_agent_paths(project / "home")),
            ):
                with self.assertRaises(SystemExit) as raised:
                    cli.main([
                        "--branch",
                        "feat/x",
                        str(project),
                        "python:3.12-slim",
                    ])

        self.assertIn("--branch requires", str(raised.exception))

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

    def test_unsupervised_opencode_uses_headless_dispatch_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "README.md").write_text("# demo\n", encoding="utf-8")
            host_home = project / "home"
            paths = _agent_paths(host_home)
            paths["opencode"].mkdir(parents=True)
            out = io.StringIO()

            with (
                patch.object(cli, "read_identity", return_value=GitIdentity("Ada", "ada@example.com")),
                patch.object(cli.config_agents, "_agent_host_paths", return_value=paths),
                contextlib.redirect_stdout(out),
            ):
                rc = cli.main([
                    "--dry-run",
                    "--no-build",
                    "--agent",
                    "opencode",
                    "--prompt-text",
                    "fix this",
                    str(project),
                    "python:3.12-slim",
                ])

        self.assertEqual(rc, 0)
        self.assertIn("opencode-headless", out.getvalue())

    def test_unsupervised_bash_uses_headless_dispatch_without_host_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "README.md").write_text("# demo\n", encoding="utf-8")
            out = io.StringIO()

            with (
                patch.object(cli, "read_identity", return_value=GitIdentity("Ada", "ada@example.com")),
                patch.object(cli.config_agents, "_agent_host_paths", return_value=_agent_paths(project / "home")),
                contextlib.redirect_stdout(out),
            ):
                rc = cli.main([
                    "--dry-run",
                    "--no-build",
                    "--agent",
                    "bash",
                    "--prompt-text",
                    "echo ok",
                    str(project),
                    "python:3.12-slim",
                ])

        self.assertEqual(rc, 0)
        self.assertIn("bash-headless", out.getvalue())

    def test_unavailable_agent_raises_with_available_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "README.md").write_text("# demo\n", encoding="utf-8")
            host_home = project / "home"
            paths = _agent_paths(host_home)
            paths["claude"].mkdir(parents=True)

            with (
                patch.object(cli, "read_identity", return_value=GitIdentity("Ada", "ada@example.com")),
                patch.object(cli.config_agents, "_agent_host_paths", return_value=paths),
            ):
                with self.assertRaises(SystemExit) as raised:
                    cli.main([
                        "--dry-run",
                        "--no-build",
                        "--agent",
                        "opencode",
                        str(project),
                        "python:3.12-slim",
                    ])

        self.assertIn("unavailable", str(raised.exception).lower())
        self.assertIn("claude", str(raised.exception).lower())
        self.assertIn("bash", str(raised.exception).lower())
