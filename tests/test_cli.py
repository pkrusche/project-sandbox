import contextlib
import io
import os
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
        # Normalize whitespace to handle argparse line-wrapping in assertions.
        flat_help = " ".join(help_text.split())
        self.assertIn("--dry-run", help_text)
        self.assertIn("--branch", help_text)
        self.assertIn("--dockerfile", help_text)
        self.assertIn("--runtime", help_text)
        self.assertIn("--allow-github", help_text)
        self.assertIn("--api-key-env", help_text)
        self.assertIn("--api-key-env-file", help_text)
        self.assertNotIn("--rebuild", help_text)
        self.assertNotIn("--refresh-config", help_text)
        self.assertIn("bash", help_text)
        self.assertIn("--model", help_text)
        self.assertIn("--effort", help_text)
        self.assertIn("claude models", flat_help)
        self.assertIn("codex models list", flat_help)
        self.assertIn("opencode models", flat_help)

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

    def test_dry_run_uses_explicit_docker_runtime(self) -> None:
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
                    "--runtime",
                    "docker",
                    "--agent",
                    "bash",
                    str(project),
                    "python:3.12-slim",
                ])

            self.assertEqual(rc, 0)
            self.assertIn("docker run", out.getvalue())
            self.assertNotIn("container system start", out.getvalue())

    def test_missing_explicit_runtime_fails_before_writing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "README.md").write_text("# demo\n", encoding="utf-8")

            with (
                patch.object(cli, "read_identity", return_value=GitIdentity("Ada", "ada@example.com")),
                patch.object(cli.config_agents, "_agent_host_paths", return_value=_agent_paths(project / "home")),
                patch("project_sandbox.container_cli.shutil.which", return_value=None),
            ):
                with self.assertRaises(SystemExit) as raised:
                    cli.main([
                        "--runtime",
                        "docker",
                        "--agent",
                        "bash",
                        str(project),
                        "python:3.12-slim",
                    ])

            self.assertIn("docker CLI not found", str(raised.exception))
            self.assertFalse((project / ".project-sandbox").exists())
            self.assertFalse((project / ".devcontainer").exists())

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

    def test_docker_context_requires_dockerfile(self) -> None:
        import argparse

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            context_dir = project / ".project-sandbox"
            args = argparse.Namespace(
                docker_context=str(project),
                dockerfile=None,
                base_image="python:3.12-slim",
            )

            with self.assertRaises(SystemExit) as raised:
                cli._resolve_build_source(args, project=project, context_dir=context_dir)

        self.assertIn("--docker-context requires --dockerfile", str(raised.exception))

    def test_dockerfile_must_point_to_file(self) -> None:
        import argparse

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            dockerfile_dir = project / "Dockerfile"
            dockerfile_dir.mkdir()
            args = argparse.Namespace(
                docker_context=None,
                dockerfile=str(dockerfile_dir),
                base_image=None,
            )

            with self.assertRaises(SystemExit) as raised:
                cli._resolve_build_source(
                    args,
                    project=project,
                    context_dir=project / ".project-sandbox",
                )

        self.assertIn("--dockerfile must point to a file", str(raised.exception))

    def test_docker_context_must_point_to_directory(self) -> None:
        import argparse

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = project / "Dockerfile"
            source.write_text("FROM python:3.12-slim\n", encoding="utf-8")
            context_file = project / "context.txt"
            context_file.write_text("not a directory\n", encoding="utf-8")
            args = argparse.Namespace(
                docker_context=str(context_file),
                dockerfile=str(source),
                base_image=None,
            )

            with self.assertRaises(SystemExit) as raised:
                cli._resolve_build_source(
                    args,
                    project=project,
                    context_dir=project / ".project-sandbox",
                )

        self.assertIn("--docker-context must point to a directory", str(raised.exception))

    def test_docker_context_must_contain_generated_sandbox_dir(self) -> None:
        import argparse

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            source = project / "Dockerfile"
            source.write_text("FROM python:3.12-slim\n", encoding="utf-8")
            outside_context = root / "context"
            outside_context.mkdir()
            args = argparse.Namespace(
                docker_context=str(outside_context),
                dockerfile=str(source),
                base_image=None,
            )

            with self.assertRaises(SystemExit) as raised:
                cli._resolve_build_source(
                    args,
                    project=project,
                    context_dir=project / ".project-sandbox",
                )

        self.assertIn("must contain the generated .project-sandbox", str(raised.exception))

    def test_base_image_is_required_without_dockerfile(self) -> None:
        import argparse

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            args = argparse.Namespace(
                docker_context=None,
                dockerfile=None,
                base_image=None,
            )

            with self.assertRaises(SystemExit) as raised:
                cli._resolve_build_source(
                    args,
                    project=project,
                    context_dir=project / ".project-sandbox",
                )

        self.assertIn("base_image is required", str(raised.exception))

    def test_branch_jj_repo_dispatches_to_jj_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".jj").mkdir()
            host_home = project / "home"
            paths = _agent_paths(host_home)
            paths["claude"].mkdir(parents=True)

            fake_ws = cli.jj_workspace_mod.JjWorkspace(
                path=project.parent / f"{project.name}-workspaces" / "feat-x",
                bookmark="feat/x",
            )
            stdout_buf = io.StringIO()
            with (
                patch.object(cli, "read_identity", return_value=GitIdentity("A", "a@b.com")),
                patch.object(cli.config_agents, "_agent_host_paths", return_value=paths),
                patch.object(cli.jj_workspace_mod, "setup", return_value=fake_ws) as jj_setup,
                patch.object(cli.worktree_mod, "setup") as git_setup,
                patch.object(cli.jj_workspace_mod, "teardown"),
                contextlib.redirect_stdout(stdout_buf),
            ):
                cli.main([
                    "--dry-run", "--no-build", "--no-firewall",
                    "--agent", "claude",
                    "--branch", "feat/x", "--after-session", "nothing",
                    str(project), "python:3.12-slim",
                ])

        jj_setup.assert_not_called()  # dry-run uses path_for, not setup
        git_setup.assert_not_called()
        # Dry-run output should reference the jj workspace path
        output = stdout_buf.getvalue()
        self.assertIn(str(fake_ws.path), output)

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
        self.assertIn(".git is a file or missing", str(raised.exception))

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

    def test_branch_jj_dry_run_argv_includes_jj_metadata_mount(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".jj").mkdir()
            host_home = project / "home"
            paths = _agent_paths(host_home)
            paths["claude"].mkdir(parents=True)

            stdout_buf = io.StringIO()
            with (
                patch.object(cli, "read_identity", return_value=GitIdentity("A", "a@b.com")),
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
        output = stdout_buf.getvalue()
        jj_dir = str((project / ".jj").resolve())
        self.assertIn(jj_dir, output)
        self.assertIn("Would mount .jj metadata:", output)
        self.assertIn("-> /", output)
        self.assertNotIn("Would mount .git metadata:", output)

    def test_branch_jj_mount_conflicting_with_jj_metadata_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".jj").mkdir()
            host_home = project / "home"
            paths = _agent_paths(host_home)
            paths["claude"].mkdir(parents=True)
            jj_dir = str((project / ".jj").resolve())

            with (
                patch.object(cli, "read_identity", return_value=GitIdentity("A", "a@b.com")),
                patch.object(cli.config_agents, "_agent_host_paths", return_value=paths),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as raised:
                    cli.main([
                        "--dry-run", "--no-build", "--no-firewall",
                        "--agent", "claude",
                        "--branch", "feat/x", "--after-session", "nothing",
                        "--mount", f"type=bind,source={jj_dir},target=/jj",
                        str(project), "python:3.12-slim",
                    ])

        self.assertIn("--mount conflicts", str(raised.exception))
        self.assertIn(jj_dir, str(raised.exception))

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

    def test_branch_mount_conflicting_with_git_metadata_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _make_git_repo(project)
            host_home = project / "home"
            paths = _agent_paths(host_home)
            paths["claude"].mkdir(parents=True)
            git_dir = (project / ".git").resolve()

            with (
                patch.object(cli, "read_identity", return_value=GitIdentity("A", "a@b.com")),
                patch.object(cli.config_agents, "_agent_host_paths", return_value=paths),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as raised:
                    cli.main([
                        "--dry-run", "--no-build", "--no-firewall",
                        "--agent", "claude",
                        "--branch", "feat/x", "--after-session", "nothing",
                        "--mount", f"type=bind,source={git_dir},target=/git",
                        str(project), "python:3.12-slim",
                    ])

        self.assertIn("--mount conflicts", str(raised.exception))
        self.assertIn(str(git_dir), str(raised.exception))

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

    def test_failed_build_tears_down_worktree_without_integrating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _make_git_repo(project)
            host_home = project / "home"
            paths = _agent_paths(host_home)
            paths["claude"].mkdir(parents=True)

            fake_wt = cli.worktree_mod.Worktree(
                path=project.parent / f"{project.name}-worktrees" / "feat-x",
                branch="feat/x",
            )

            with (
                patch.object(cli, "read_identity", return_value=GitIdentity("A", "a@b.com")),
                patch.object(cli.config_agents, "_agent_host_paths", return_value=paths),
                patch.object(cli.config_agents, "sync_credentials", return_value={
                    "claude": host_home / "c", "claude-devcontainer": host_home / "cd",
                }),
                patch.object(cli.worktree_mod, "setup", return_value=fake_wt),
                patch.object(cli.worktree_mod, "teardown") as teardown,
                patch.object(cli.container_cli, "select_runtime", return_value=cli.container_cli.DOCKER),
                patch.object(cli.container_cli, "ensure_system_started", return_value=0),
                patch.object(cli.container_cli, "build_image", return_value=1),
                patch.object(cli.container_cli, "run") as run,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                rc = cli.main([
                    "--no-firewall",
                    "--agent", "claude",
                    "--branch", "feat/x", "--after-session", "merge",
                    str(project), "python:3.12-slim",
                ])

            self.assertEqual(rc, 1)
            run.assert_not_called()
            # Build failed before the agent ran: teardown must NOT integrate
            # (no merge of an empty/failed session), regardless of --after-session.
            teardown.assert_called_once()
            self.assertEqual(teardown.call_args.kwargs.get("after"), "nothing")

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
        output = out.getvalue()
        self.assertIn("opencode-headless", output)
        self.assertIn("OpenCode provider network access depends", output)
        self.assertIn("--allow-github", output)

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

    def test_github_allowlist_is_enabled_for_headless_copilot_cli_command(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args([
            "--agent",
            "bash",
            "--prompt-text",
            "copilot -p 'summarize this repo'",
            "/tmp/project",
            "python:3.12-slim",
        ])

        self.assertTrue(cli._allow_github(args, "bash"))

    def test_github_allowlist_is_explicit_for_non_copilot_commands(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args([
            "--agent",
            "bash",
            "--prompt-text",
            "git status",
            "/tmp/project",
            "python:3.12-slim",
        ])
        explicit = parser.parse_args([
            "--allow-github",
            "--agent",
            "bash",
            "--prompt-text",
            "git status",
            "/tmp/project",
            "python:3.12-slim",
        ])

        self.assertFalse(cli._allow_github(args, "bash"))
        self.assertTrue(cli._allow_github(explicit, "bash"))

    def _headless_dry_run(self, *extra_args: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "README.md").write_text("# demo\n", encoding="utf-8")
            out = io.StringIO()
            with (
                patch.object(cli, "read_identity", return_value=GitIdentity("Ada", "ada@example.com")),
                patch.object(
                    cli.config_agents,
                    "_agent_host_paths",
                    return_value=_agent_paths(project / "home"),
                ),
                contextlib.redirect_stdout(out),
            ):
                rc = cli.main([
                    "--dry-run", "--no-build", "--agent", "bash",
                    "--prompt-text", "echo ok",
                    *extra_args,
                    str(project), "python:3.12-slim",
                ])
            self.assertEqual(rc, 0)
            return out.getvalue()

    def test_non_verbose_headless_quiets_startup_and_log_redirect(self) -> None:
        out = self._headless_dry_run()
        # Quiet startup is requested in-container and stdout is redirected to the
        # log only (no tee to the terminal).
        self.assertIn("PROJECT_SANDBOX_QUIET=1", out)
        self.assertNotIn("| tee", out)

    def test_verbose_headless_streams_and_skips_quiet(self) -> None:
        out = self._headless_dry_run("--verbose")
        self.assertNotIn("PROJECT_SANDBOX_QUIET", out)
        self.assertIn("| tee", out)

    def test_prompt_text_dry_run_uses_prompt_file_not_environment(self) -> None:
        out = self._headless_dry_run()
        self.assertIn("Would write prompt to:", out)
        self.assertIn(
            "PROJECT_SANDBOX_PROMPT_FILE=/project-sandbox-prompt/prompt.txt",
            out,
        )
        self.assertIn("target=/project-sandbox-prompt,readonly", out)
        self.assertNotIn("PROJECT_SANDBOX_PROMPT=echo ok", out)

    def test_dry_run_masks_workspace_project_sandbox_after_user_mounts(self) -> None:
        out = self._headless_dry_run(
            "--mount",
            "type=bind,source=/tmp/custom,target=/workspace/.project-sandbox",
        )

        custom = "type=bind,source=/tmp/custom,target=/workspace/.project-sandbox"
        mask = "target=/workspace/.project-sandbox,readonly"
        self.assertIn("Would mask workspace sandbox files with:", out)
        self.assertIn(custom, out)
        self.assertIn(mask, out)
        self.assertLess(out.index(custom), out.index(mask))

    def test_prompt_text_writes_prompt_file_for_short_prompt(self) -> None:
        import argparse

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            context_dir = project / ".project-sandbox"
            claude_cfg = context_dir / "claude" / "settings.json"
            codex_cfg = context_dir / "codex" / "config.toml"
            credential_dirs = {"claude": context_dir / "claude-secrets"}
            args = argparse.Namespace(
                branch=None,
                cpus=4,
                extra_mounts=[],
                image_tag="project-sandbox:test",
                log=None,
                memory="8g",
                no_firewall=True,
                prompt=None,
                prompt_text="echo ok",
                verbose=False,
            )

            cmd, log_path, unsupervised, _stop_argv = cli._build_session_command(
                args,
                project=project,
                context_dir=context_dir,
                workspace=project,
                worktree=None,
                identity=GitIdentity(None, None),
                run_agent="bash",
                claude_cfg=claude_cfg,
                credential_dirs=credential_dirs,
                codex_cfg=codex_cfg,
                runtime=cli.container_cli.DOCKER,
                create_prompt_files=True,
            )

            prompt_file = context_dir / "prompts" / "prompt.txt"
            self.assertTrue(unsupervised)
            self.assertIsNotNone(log_path)
            self.assertEqual(prompt_file.read_text(encoding="utf-8"), "echo ok")
            self.assertIn(
                f"type=bind,source={prompt_file.parent.resolve()},"
                "target=/project-sandbox-prompt,readonly",
                cmd,
            )
            self.assertIn(
                "PROJECT_SANDBOX_PROMPT_FILE=/project-sandbox-prompt/prompt.txt",
                cmd,
            )
            self.assertNotIn("PROJECT_SANDBOX_PROMPT=echo ok", cmd)

    def test_prompt_file_mounts_staged_copy_not_source_parent(self) -> None:
        import argparse

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            context_dir = project / ".project-sandbox"
            prompt_file = project / "prompt.txt"
            prompt_file.write_text("echo ok", encoding="utf-8")
            claude_cfg = context_dir / "claude" / "settings.json"
            codex_cfg = context_dir / "codex" / "config.toml"
            credential_dirs = {"claude": context_dir / "claude-secrets"}
            args = argparse.Namespace(
                branch=None,
                cpus=4,
                extra_mounts=[],
                image_tag="project-sandbox:test",
                log=None,
                memory="8g",
                no_firewall=True,
                prompt=str(prompt_file),
                prompt_text=None,
                verbose=False,
            )

            cmd, _, unsupervised, _stop_argv = cli._build_session_command(
                args,
                project=project,
                context_dir=context_dir,
                workspace=project,
                worktree=None,
                identity=GitIdentity(None, None),
                run_agent="bash",
                claude_cfg=claude_cfg,
                credential_dirs=credential_dirs,
                codex_cfg=codex_cfg,
                runtime=cli.container_cli.DOCKER,
                create_prompt_files=True,
            )

            self.assertTrue(unsupervised)
            # The prompt is copied into a private staging dir and only that dir
            # is mounted; the source parent (which could be $HOME) is not.
            staging_dir = context_dir / "prompt"
            staged_file = staging_dir / "prompt.txt"
            self.assertTrue(staged_file.is_file())
            self.assertEqual(staged_file.read_text(encoding="utf-8"), "echo ok")
            self.assertIn(
                f"type=bind,source={staging_dir.resolve()},"
                "target=/project-sandbox-prompt,readonly",
                cmd,
            )
            self.assertNotIn(
                f"type=bind,source={prompt_file.parent.resolve()},"
                "target=/project-sandbox-prompt,readonly",
                cmd,
            )
            self.assertIn(
                "PROJECT_SANDBOX_PROMPT_FILE=/project-sandbox-prompt/prompt.txt",
                cmd,
            )

    def test_interactive_session_mounts_history_files(self) -> None:
        import argparse

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            context_dir = project / ".project-sandbox"
            claude_cfg = context_dir / "claude" / "settings.json"
            codex_cfg = context_dir / "codex" / "config.toml"
            credential_dirs = {"claude": context_dir / "claude-secrets"}
            args = argparse.Namespace(
                branch=None,
                cpus=4,
                extra_mounts=[],
                image_tag="project-sandbox:test",
                log=None,
                memory="8g",
                no_firewall=True,
                prompt=None,
                prompt_text=None,
                verbose=False,
            )

            cmd, log_path, unsupervised, _stop_argv = cli._build_session_command(
                args,
                project=project,
                context_dir=context_dir,
                workspace=project,
                worktree=None,
                identity=GitIdentity(None, None),
                run_agent="claude",
                claude_cfg=claude_cfg,
                credential_dirs=credential_dirs,
                codex_cfg=codex_cfg,
                runtime=cli.container_cli.DOCKER,
                create_prompt_files=True,
            )

            history_dir = project / ".project-sandbox" / "history"
            shell_dir = history_dir / "shell"
            claude_projects = history_dir / "claude_projects"

            self.assertFalse(unsupervised)
            self.assertIsNone(log_path)
            # history dirs must be created (both sources are directories so the
            # mounts work on apple/container, which rejects file bind mounts)
            self.assertTrue(shell_dir.is_dir())
            self.assertTrue(claude_projects.is_dir())
            # shell history dir mounted at /home/agent/.bash_history.d ...
            self.assertIn(
                f"type=bind,source={shell_dir.resolve()},target=/home/agent/.bash_history.d",
                cmd,
            )
            # ... with HISTFILE pointing at a file inside it
            self.assertIn("HISTFILE=/home/agent/.bash_history.d/bash_history", cmd)
            # claude_projects dir must be mounted at /home/agent/.claude/projects
            self.assertIn(
                f"type=bind,source={claude_projects.resolve()},target=/home/agent/.claude/projects",
                cmd,
            )

    def test_headless_session_does_not_mount_history_files(self) -> None:
        import argparse

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            context_dir = project / ".project-sandbox"
            claude_cfg = context_dir / "claude" / "settings.json"
            codex_cfg = context_dir / "codex" / "config.toml"
            credential_dirs = {"claude": context_dir / "claude-secrets"}
            args = argparse.Namespace(
                branch=None,
                cpus=4,
                extra_mounts=[],
                image_tag="project-sandbox:test",
                log=None,
                memory="8g",
                no_firewall=True,
                prompt=None,
                prompt_text="do something",
                verbose=False,
            )

            cmd, log_path, unsupervised, _stop_argv = cli._build_session_command(
                args,
                project=project,
                context_dir=context_dir,
                workspace=project,
                worktree=None,
                identity=GitIdentity(None, None),
                run_agent="claude",
                claude_cfg=claude_cfg,
                credential_dirs=credential_dirs,
                codex_cfg=codex_cfg,
                runtime=cli.container_cli.DOCKER,
                create_prompt_files=True,
            )

            self.assertTrue(unsupervised)
            self.assertNotIn("/home/agent/.bash_history", " ".join(cmd))
            self.assertNotIn("/home/agent/.claude/projects", " ".join(cmd))

    def test_project_sandbox_gitignore_excludes_history_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context_dir = Path(tmp)
            cli._write_project_sandbox_gitignore(context_dir)
            content = (context_dir / ".gitignore").read_text(encoding="utf-8")
            self.assertIn("history/", content)

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


class ModelSelectionTests(TestCase):
    """--model passes PROJECT_SANDBOX_MODEL into unsupervised container runs."""

    def _headless_dry_run_with_model(self, agent: str, model: str | None) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "README.md").write_text("# demo\n", encoding="utf-8")
            host_home = project / "home"
            paths = _agent_paths(host_home)
            for key in paths:
                paths[key].mkdir(parents=True, exist_ok=True)
            out = io.StringIO()
            extra = ["--model", model] if model else []
            with (
                patch.object(cli, "read_identity", return_value=GitIdentity("Ada", "ada@example.com")),
                patch.object(cli.config_agents, "_agent_host_paths", return_value=paths),
                contextlib.redirect_stdout(out),
            ):
                rc = cli.main([
                    "--dry-run", "--no-build", "--no-firewall",
                    "--agent", agent,
                    "--prompt-text", "do something",
                    *extra,
                    str(project), "python:3.12-slim",
                ])
            self.assertEqual(rc, 0)
            return out.getvalue()

    def test_model_injected_for_claude_headless(self) -> None:
        output = self._headless_dry_run_with_model("claude", "claude-opus-4-5")
        self.assertIn("PROJECT_SANDBOX_MODEL=claude-opus-4-5", output)

    def test_model_injected_for_codex_headless(self) -> None:
        output = self._headless_dry_run_with_model("codex", "o4-mini")
        self.assertIn("PROJECT_SANDBOX_MODEL=o4-mini", output)

    def test_model_injected_for_opencode_headless(self) -> None:
        output = self._headless_dry_run_with_model("opencode", "gpt-4o")
        self.assertIn("PROJECT_SANDBOX_MODEL=gpt-4o", output)

    def test_no_model_does_not_inject_env_var(self) -> None:
        output = self._headless_dry_run_with_model("claude", None)
        self.assertNotIn("PROJECT_SANDBOX_MODEL", output)

    def test_model_not_injected_in_interactive_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "README.md").write_text("# demo\n", encoding="utf-8")
            host_home = project / "home"
            paths = _agent_paths(host_home)
            paths["claude"].mkdir(parents=True, exist_ok=True)
            out = io.StringIO()
            with (
                patch.object(cli, "read_identity", return_value=GitIdentity("Ada", "ada@example.com")),
                patch.object(cli.config_agents, "_agent_host_paths", return_value=paths),
                contextlib.redirect_stdout(out),
            ):
                rc = cli.main([
                    "--dry-run", "--no-build", "--no-firewall",
                    "--agent", "claude",
                    "--model", "claude-opus-4-5",
                    str(project), "python:3.12-slim",
                ])
            self.assertEqual(rc, 0)
            self.assertNotIn("PROJECT_SANDBOX_MODEL", out.getvalue())


class EffortSelectionTests(TestCase):
    """--effort passes PROJECT_SANDBOX_EFFORT into unsupervised Claude runs."""

    def _headless_dry_run_with_effort(self, agent: str, effort: str | None) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "README.md").write_text("# demo\n", encoding="utf-8")
            host_home = project / "home"
            paths = _agent_paths(host_home)
            for key in paths:
                paths[key].mkdir(parents=True, exist_ok=True)
            out = io.StringIO()
            extra = ["--effort", effort] if effort else []
            with (
                patch.object(cli, "read_identity", return_value=GitIdentity("Ada", "ada@example.com")),
                patch.object(cli.config_agents, "_agent_host_paths", return_value=paths),
                contextlib.redirect_stdout(out),
            ):
                rc = cli.main([
                    "--dry-run", "--no-build", "--no-firewall",
                    "--agent", agent,
                    "--prompt-text", "do something",
                    *extra,
                    str(project), "python:3.12-slim",
                ])
            self.assertEqual(rc, 0)
            return out.getvalue()

    def test_effort_injected_for_claude_headless(self) -> None:
        output = self._headless_dry_run_with_effort("claude", "high")
        self.assertIn("PROJECT_SANDBOX_EFFORT=high", output)

    def test_effort_injected_for_codex_headless(self) -> None:
        output = self._headless_dry_run_with_effort("codex", "low")
        self.assertIn("PROJECT_SANDBOX_EFFORT=low", output)

    def test_no_effort_does_not_inject_env_var(self) -> None:
        output = self._headless_dry_run_with_effort("claude", None)
        self.assertNotIn("PROJECT_SANDBOX_EFFORT", output)

    def test_effort_not_injected_in_interactive_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "README.md").write_text("# demo\n", encoding="utf-8")
            host_home = project / "home"
            paths = _agent_paths(host_home)
            paths["claude"].mkdir(parents=True, exist_ok=True)
            out = io.StringIO()
            with (
                patch.object(cli, "read_identity", return_value=GitIdentity("Ada", "ada@example.com")),
                patch.object(cli.config_agents, "_agent_host_paths", return_value=paths),
                contextlib.redirect_stdout(out),
            ):
                rc = cli.main([
                    "--dry-run", "--no-build", "--no-firewall",
                    "--agent", "claude",
                    "--effort", "max",
                    str(project), "python:3.12-slim",
                ])
            self.assertEqual(rc, 0)
            self.assertNotIn("PROJECT_SANDBOX_EFFORT", out.getvalue())

    def test_effort_choices_are_validated(self) -> None:
        parser = cli.build_parser()
        with (
            self.assertRaises(SystemExit),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            parser.parse_args([
                "--effort", "ultra",
                "/tmp/project", "python:3.12-slim",
            ])

    def test_effort_and_model_can_be_combined(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "README.md").write_text("# demo\n", encoding="utf-8")
            host_home = project / "home"
            paths = _agent_paths(host_home)
            for key in paths:
                paths[key].mkdir(parents=True, exist_ok=True)
            out = io.StringIO()
            with (
                patch.object(cli, "read_identity", return_value=GitIdentity("Ada", "ada@example.com")),
                patch.object(cli.config_agents, "_agent_host_paths", return_value=paths),
                contextlib.redirect_stdout(out),
            ):
                rc = cli.main([
                    "--dry-run", "--no-build", "--no-firewall",
                    "--agent", "claude",
                    "--prompt-text", "do something",
                    "--model", "claude-opus-4-8",
                    "--effort", "xhigh",
                    str(project), "python:3.12-slim",
                ])
            self.assertEqual(rc, 0)
            result = out.getvalue()
            self.assertIn("PROJECT_SANDBOX_MODEL=claude-opus-4-8", result)
            self.assertIn("PROJECT_SANDBOX_EFFORT=xhigh", result)


class TeardownWorktreeOnFailureTests(TestCase):
    """_teardown_worktree must skip integration for all modes on nonzero exit."""

    def _run_teardown(self, exit_code: int, after_session: str) -> tuple[str, str]:
        import argparse
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _make_git_repo(project)
            fake_wt = cli.worktree_mod.Worktree(
                path=project.parent / "wt" / "feat-x",
                branch="feat/x",
            )
            args = argparse.Namespace(after_session=after_session)
            out, err = io.StringIO(), io.StringIO()
            with (
                patch.object(cli.worktree_mod, "teardown") as teardown,
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(err),
            ):
                cli._teardown_worktree(args, project=project, wt=fake_wt, exit_code=exit_code)
            return out.getvalue(), teardown.call_args.kwargs.get("after")

    def test_merge_skipped_on_nonzero_exit(self) -> None:
        output, after = self._run_teardown(exit_code=124, after_session="merge")
        self.assertEqual(after, "nothing")
        self.assertIn("124", output)
        self.assertIn("merge", output)

    def test_rebase_skipped_on_nonzero_exit(self) -> None:
        output, after = self._run_teardown(exit_code=1, after_session="rebase")
        self.assertEqual(after, "nothing")
        self.assertIn("1", output)
        self.assertIn("rebase", output)

    def test_pr_skipped_on_nonzero_exit(self) -> None:
        output, after = self._run_teardown(exit_code=1, after_session="pr")
        self.assertEqual(after, "nothing")
        self.assertIn("pr", output)

    def test_nothing_silent_on_nonzero_exit(self) -> None:
        output, after = self._run_teardown(exit_code=1, after_session="nothing")
        self.assertEqual(after, "nothing")
        self.assertEqual(output, "")

    def test_proceeds_on_zero_exit(self) -> None:
        _, after = self._run_teardown(exit_code=0, after_session="merge")
        self.assertEqual(after, "merge")


class DefaultImageTagTests(TestCase):
    def test_differs_per_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
            tag1 = cli._default_image_tag(Path(tmp1))
            tag2 = cli._default_image_tag(Path(tmp2))
        self.assertNotEqual(tag1, tag2)

    def test_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tag = cli._default_image_tag(Path(tmp))
        self.assertRegex(tag, r"^project-sandbox-[a-z0-9._-]+-[0-9a-f]{8}:latest$")

    def test_explicit_image_tag_overrides_default(self) -> None:
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
                patch.object(cli.container_cli, "select_runtime", return_value=cli.container_cli.DOCKER),
                patch.object(cli.container_cli, "ensure_system_started", return_value=0),
                patch.object(cli.container_cli, "build_image", return_value=0) as build_image,
                patch.object(cli.container_cli, "run", return_value=0),
                contextlib.redirect_stdout(out),
            ):
                cli.main([
                    "--image-tag", "my-custom:v1",
                    "--agent", "claude",
                    str(project), "python:3.12-slim",
                ])

            call_kwargs = build_image.call_args.kwargs
            self.assertEqual(call_kwargs["image_tag"], "my-custom:v1")


class BuildCacheReuseTests(TestCase):
    """The build is skipped when inputs are unchanged and the image exists."""

    def _run(
        self,
        project: Path,
        *,
        image_exists: bool,
        extra_args: list[str] | None = None,
        base_image: str | None = "python:3.12-slim",
    ) -> tuple[int, str, "patch"]:
        host_home = project / "home"
        paths = _agent_paths(host_home)
        paths["claude"].mkdir(parents=True, exist_ok=True)
        out = io.StringIO()
        positional = [str(project)] + ([base_image] if base_image else [])
        with (
            patch.object(cli, "read_identity", return_value=GitIdentity("Ada", "ada@example.com")),
            patch.object(cli.config_agents, "_agent_host_paths", return_value=paths),
            patch.object(cli.config_agents, "sync_credentials"),
            patch.object(cli.container_cli, "select_runtime", return_value=cli.container_cli.DOCKER),
            patch.object(cli.container_cli, "ensure_system_started", return_value=0),
            patch.object(cli.container_cli, "image_exists", return_value=image_exists),
            patch.object(cli.container_cli, "build_image", return_value=0) as build_image,
            patch.object(cli.container_cli, "run", return_value=0),
            contextlib.redirect_stdout(out),
        ):
            rc = cli.main([
                "--agent", "claude",
                *(extra_args or []),
                *positional,
            ])
        return rc, out.getvalue(), build_image

    def _make_project(self, tmp: str) -> Path:
        project = Path(tmp)
        (project / "README.md").write_text("# demo\n", encoding="utf-8")
        return project

    def test_first_run_builds_and_records_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._make_project(tmp)
            rc, out, build_image = self._run(project, image_exists=False)
            self.assertEqual(rc, 0)
            build_image.assert_called_once()
            self.assertIn("Built image in", out)
            self.assertTrue((project / ".project-sandbox" / ".build-state.json").exists())

    def test_second_run_reuses_cached_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._make_project(tmp)
            self._run(project, image_exists=False)  # seed state
            rc, out, build_image = self._run(project, image_exists=True)
            self.assertEqual(rc, 0)
            build_image.assert_not_called()
            self.assertIn("Reusing cached image", out)

    def test_force_build_rebuilds_despite_valid_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._make_project(tmp)
            self._run(project, image_exists=False)  # seed state
            rc, out, build_image = self._run(
                project, image_exists=True, extra_args=["--force-build"]
            )
            self.assertEqual(rc, 0)
            build_image.assert_called_once()

    def test_missing_image_forces_rebuild_even_with_matching_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._make_project(tmp)
            self._run(project, image_exists=False)  # seed state
            rc, out, build_image = self._run(project, image_exists=False)
            self.assertEqual(rc, 0)
            build_image.assert_called_once()

    def test_changed_inputs_force_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._make_project(tmp)
            self._run(project, image_exists=False)  # seed state
            # Corrupt the recorded fingerprint so it no longer matches.
            state = project / ".project-sandbox" / ".build-state.json"
            state.write_text('{"image_tag": "x", "fingerprint": "stale"}\n', encoding="utf-8")
            rc, out, build_image = self._run(project, image_exists=True)
            self.assertEqual(rc, 0)
            build_image.assert_called_once()

    def test_python_uv_run_generates_dockerignore_but_base_image_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._make_project(tmp)
            self._run(project, image_exists=False)  # base-image flow
            self.assertFalse(
                (project / ".project-sandbox" / "Dockerfile.dockerignore").exists()
            )

        with tempfile.TemporaryDirectory() as tmp:
            project = self._make_project(tmp)
            (project / "pyproject.toml").write_text("[project]\nname='d'\n", encoding="utf-8")
            (project / "uv.lock").write_text("version = 1\n", encoding="utf-8")
            self._run(project, image_exists=False, extra_args=["--python-uv"], base_image=None)
            self.assertTrue(
                (project / ".project-sandbox" / "Dockerfile.dockerignore").exists()
            )

    def test_python_uv_whole_project_context_never_skips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._make_project(tmp)
            (project / "pyproject.toml").write_text("[project]\nname='d'\n", encoding="utf-8")
            (project / "uv.lock").write_text("version = 1\n", encoding="utf-8")
            # Even with a matching state file and an existing image, the
            # whole-project build context disables the auto-skip.
            self._run(project, image_exists=False, extra_args=["--python-uv"], base_image=None)
            rc, out, build_image = self._run(
                project, image_exists=True, extra_args=["--python-uv"], base_image=None
            )
            self.assertEqual(rc, 0)
            build_image.assert_called_once()


class PythonUvFlagTests(TestCase):
    """Tests for --python-uv and --python VERSION flags."""

    def _make_project(
        self,
        tmp: str,
        *,
        with_pyproject: bool = True,
        with_uvlock: bool = True,
    ) -> Path:
        project = Path(tmp)
        (project / "README.md").write_text("# demo\n", encoding="utf-8")
        if with_pyproject:
            (project / "pyproject.toml").write_text(
                "[project]\nname = 'demo'\n", encoding="utf-8"
            )
        if with_uvlock:
            (project / "uv.lock").write_text("version = 1\n", encoding="utf-8")
        return project

    def _dry_run_python_uv(self, project: Path, extra_args: list[str] | None = None) -> tuple[int, str]:
        out = io.StringIO()
        with (
            patch.object(cli, "read_identity", return_value=GitIdentity("A", "a@b.com")),
            patch.object(
                cli.config_agents,
                "_agent_host_paths",
                return_value=_agent_paths(project / "home"),
            ),
            contextlib.redirect_stdout(out),
        ):
            rc = cli.main(
                ["--dry-run", "--python-uv", *(extra_args or []), str(project)]
            )
        return rc, out.getvalue()

    # --- dry-run output ---

    def test_python_uv_dry_run_shows_synthesised_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._make_project(tmp)
            rc, output = self._dry_run_python_uv(project)

        self.assertEqual(rc, 0)
        self.assertIn("Would write synthesised Dockerfile:", output)
        self.assertIn("Dockerfile.python-uv", output)
        self.assertIn("Would use build context:", output)

    def test_python_uv_dry_run_does_not_write_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._make_project(tmp)
            self._dry_run_python_uv(project)

        self.assertFalse((project / ".project-sandbox").exists())

    # --- --python VERSION ---

    def test_render_python_uv_dockerfile_uses_specified_version(self) -> None:
        from project_sandbox import dockerfile as df

        with tempfile.TemporaryDirectory() as tmp:
            context_dir = Path(tmp) / ".project-sandbox"
            context_dir.mkdir()
            out_path = df.render_python_uv_dockerfile(
                context_dir,
                python_version="3.12",
                has_pyproject=True,
                has_uvlock=True,
            )
            content = out_path.read_text(encoding="utf-8")

        self.assertIn("python:3.12-slim", content)
        self.assertNotIn("3.11", content)

    def test_python_version_flag_passes_through_to_dockerfile(self) -> None:
        """--python 3.12 with --python-uv writes a Dockerfile referencing 3.12."""
        import argparse

        with tempfile.TemporaryDirectory() as tmp:
            project = self._make_project(tmp)
            context_dir = project / ".project-sandbox"
            context_dir.mkdir()

            args = argparse.Namespace(
                python_uv=True,
                python_version="3.12",
                dockerfile=None,
                docker_context=None,
                base_image=None,
            )
            _, base_df, build_context = cli._resolve_build_source(
                args,
                project=project,
                context_dir=context_dir,
                write_generated=True,
            )

            self.assertIsNotNone(base_df)
            assert base_df is not None
            content = base_df.read_text(encoding="utf-8")
            self.assertIn("python:3.12-slim", content)
            self.assertEqual(build_context, project)

    # --- cache-warming block presence ---

    def test_render_includes_cache_warm_when_both_files_present(self) -> None:
        from project_sandbox import dockerfile as df

        with tempfile.TemporaryDirectory() as tmp:
            context_dir = Path(tmp) / ".project-sandbox"
            context_dir.mkdir()
            out_path = df.render_python_uv_dockerfile(
                context_dir,
                python_version="3.11",
                has_pyproject=True,
                has_uvlock=True,
            )
            content = out_path.read_text(encoding="utf-8")

        self.assertIn("COPY pyproject.toml uv.lock", content)
        self.assertIn("uv sync --frozen", content)
        # The cache must be baked into an image layer and owned by the agent
        # user (UID 1000); a BuildKit cache mount would be ephemeral.
        self.assertNotIn("--mount=type=cache", content)
        self.assertNotIn("type=cache", content)
        self.assertIn("chown -R 1000:1000 /opt/uv-cache /opt/venv", content)
        # venv must live outside /workspace so the host .venv is never touched
        self.assertIn("UV_PROJECT_ENVIRONMENT=/opt/venv", content)
        # project must be pre-installed at image build time so 'uv run' works
        # offline inside the sandbox (avoids fetching build deps behind firewall)
        self.assertIn("COPY . .", content)
        self.assertIn("RUN uv sync --frozen &&", content)

    def test_render_omits_cache_warm_when_pyproject_missing(self) -> None:
        from project_sandbox import dockerfile as df

        with tempfile.TemporaryDirectory() as tmp:
            context_dir = Path(tmp) / ".project-sandbox"
            context_dir.mkdir()
            out_path = df.render_python_uv_dockerfile(
                context_dir,
                python_version="3.11",
                has_pyproject=False,
                has_uvlock=True,
            )
            content = out_path.read_text(encoding="utf-8")

        self.assertNotIn("COPY pyproject.toml", content)
        self.assertNotIn("uv sync", content)

    def test_render_omits_cache_warm_when_uvlock_missing(self) -> None:
        from project_sandbox import dockerfile as df

        with tempfile.TemporaryDirectory() as tmp:
            context_dir = Path(tmp) / ".project-sandbox"
            context_dir.mkdir()
            out_path = df.render_python_uv_dockerfile(
                context_dir,
                python_version="3.11",
                has_pyproject=True,
                has_uvlock=False,
            )
            content = out_path.read_text(encoding="utf-8")

        self.assertNotIn("COPY pyproject.toml", content)
        self.assertNotIn("uv sync", content)

    # --- warnings for missing project files ---

    def test_python_uv_warns_when_pyproject_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._make_project(tmp, with_pyproject=False, with_uvlock=True)
            rc, output = self._dry_run_python_uv(project)

        self.assertEqual(rc, 0)
        self.assertIn("pyproject.toml not found", output)
        self.assertIn("cache-warming step will be skipped", output)

    def test_python_uv_warns_when_uvlock_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._make_project(tmp, with_pyproject=True, with_uvlock=False)
            rc, output = self._dry_run_python_uv(project)

        self.assertEqual(rc, 0)
        self.assertIn("uv.lock not found", output)
        self.assertIn("cache-warming step will be skipped", output)

    def test_python_uv_warns_when_both_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._make_project(tmp, with_pyproject=False, with_uvlock=False)
            rc, output = self._dry_run_python_uv(project)

        self.assertEqual(rc, 0)
        self.assertIn("pyproject.toml not found", output)
        self.assertIn("uv.lock not found", output)

    # --- mutual-exclusion ---

    def test_python_uv_and_dockerfile_are_mutually_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = project / "Dockerfile"
            source.write_text("FROM python:3.11-slim\n", encoding="utf-8")

            with self.assertRaises(SystemExit) as raised:
                cli.main([
                    "--dry-run",
                    "--python-uv",
                    "--dockerfile", str(source),
                    str(project),
                ])

        self.assertIn("mutually exclusive", str(raised.exception))

    def test_python_uv_and_base_image_are_mutually_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)

            with self.assertRaises(SystemExit) as raised:
                cli.main([
                    "--dry-run",
                    "--python-uv",
                    str(project),
                    "python:3.12-slim",
                ])

        self.assertIn("mutually exclusive", str(raised.exception))

    def test_invalid_build_source_fails_before_worktree(self) -> None:
        # A bad build source must abort before _setup_worktree runs, so no
        # branch/worktree is orphaned by the failure.
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _make_git_repo(project)
            with (
                patch.object(cli, "read_identity", return_value=GitIdentity("A", "a@b.com")),
                patch.object(cli, "_setup_worktree") as setup_wt,
                patch.object(cli.config_agents, "available_agents", return_value=("claude",)),
                patch.object(cli.container_cli, "select_runtime", return_value=cli.container_cli.DOCKER),
            ):
                with self.assertRaises(SystemExit) as raised:
                    cli.main([
                        "--agent", "claude",
                        "--branch", "feat/x", "--after-session", "nothing",
                        "--python-uv",
                        str(project), "python:3.12-slim",
                    ])

            setup_wt.assert_not_called()
            self.assertIn("mutually exclusive", str(raised.exception))

    def test_missing_prompt_file_fails_before_worktree(self) -> None:
        # A missing --prompt must abort before _setup_worktree runs.
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _make_git_repo(project)
            with (
                patch.object(cli, "read_identity", return_value=GitIdentity("A", "a@b.com")),
                patch.object(cli, "_setup_worktree") as setup_wt,
                patch.object(cli.config_agents, "available_agents", return_value=("claude",)),
                patch.object(cli.container_cli, "select_runtime", return_value=cli.container_cli.DOCKER),
            ):
                with self.assertRaises((SystemExit, FileNotFoundError)):
                    cli.main([
                        "--agent", "claude",
                        "--branch", "feat/x", "--after-session", "nothing",
                        "--prompt", str(project / "missing-prompt.md"),
                        str(project), "python:3.12-slim",
                    ])

            setup_wt.assert_not_called()


class HostTokenRefreshGatingTests(TestCase):
    def _run_with_refresh_mock(self, extra_args: list[str], *, agent: str = "claude"):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "README.md").write_text("# demo\n", encoding="utf-8")
            host_home = project / "home"
            paths = _agent_paths(host_home)
            paths["claude"].mkdir(parents=True)
            paths["codex"].mkdir(parents=True)
            with (
                patch.object(cli, "read_identity", return_value=GitIdentity("Ada", "ada@example.com")),
                patch.object(cli.config_agents, "_agent_host_paths", return_value=paths),
                patch.object(cli.config_agents, "sync_credentials"),
                patch.object(cli.container_cli, "select_runtime", return_value=cli.container_cli.DOCKER),
                patch.object(cli.container_cli, "ensure_system_started", return_value=0),
                patch.object(cli.container_cli, "build_image", return_value=0),
                patch.object(cli.container_cli, "run", return_value=0),
                # Re-patch over the suite-wide autouse stub to observe the call.
                patch.object(cli.oauth_refresh, "refresh_host_token") as refresh,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                cli.main(["--agent", agent, *extra_args, str(project), "python:3.12-slim"])
            return refresh

    def test_claude_run_refreshes_claude(self) -> None:
        refresh = self._run_with_refresh_mock([])
        refresh.assert_called_once()
        self.assertEqual(refresh.call_args.args[0], "claude")

    def test_codex_run_refreshes_codex(self) -> None:
        refresh = self._run_with_refresh_mock([], agent="codex")
        refresh.assert_called_once()
        self.assertEqual(refresh.call_args.args[0], "codex")

    def test_bash_run_refreshes_claude(self) -> None:
        refresh = self._run_with_refresh_mock([], agent="bash")
        refresh.assert_called_once()
        self.assertEqual(refresh.call_args.args[0], "claude")

    def test_no_token_refresh_skips_refresh(self) -> None:
        self.assertEqual(self._run_with_refresh_mock(["--no-token-refresh"]).call_count, 0)

    def test_no_forward_credentials_skips_refresh(self) -> None:
        self.assertEqual(
            self._run_with_refresh_mock(["--no-forward-credentials"]).call_count, 0
        )


class NoForwardCredentialsTests(TestCase):
    def test_skips_staging_and_purges_instead(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "README.md").write_text("# demo\n", encoding="utf-8")
            host_home = project / "home"
            paths = _agent_paths(host_home)
            paths["claude"].mkdir(parents=True)
            with (
                patch.object(cli, "read_identity", return_value=GitIdentity("Ada", "ada@example.com")),
                patch.object(cli.config_agents, "_agent_host_paths", return_value=paths),
                patch.object(cli.config_agents, "sync_credentials") as sync,
                patch.object(cli.config_agents, "purge_staged_credentials") as purge,
                patch.object(cli.container_cli, "select_runtime", return_value=cli.container_cli.DOCKER),
                patch.object(cli.container_cli, "ensure_system_started", return_value=0),
                patch.object(cli.container_cli, "build_image", return_value=0),
                patch.object(cli.container_cli, "run", return_value=0),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                cli.main([
                    "--agent", "claude", "--no-forward-credentials",
                    str(project), "python:3.12-slim",
                ])
            sync.assert_not_called()
            purge.assert_called_once()


class ApiKeyInjectionTests(TestCase):
    def test_api_key_env_requires_no_forward_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "README.md").write_text("# demo\n", encoding="utf-8")

            with (
                patch.object(cli, "read_identity", return_value=GitIdentity("Ada", "ada@example.com")),
                patch.object(cli.config_agents, "_agent_host_paths", return_value=_agent_paths(project / "home")),
                patch.dict(os.environ, {"ANTHROPIC_API_KEY": "secret"}, clear=False),
            ):
                with self.assertRaises(SystemExit) as raised:
                    cli.main([
                        "--dry-run",
                        "--no-build",
                        "--agent",
                        "bash",
                        "--api-key-env",
                        "ANTHROPIC_API_KEY",
                        str(project),
                        "python:3.12-slim",
                    ])

            self.assertIn("require --no-forward-credentials", str(raised.exception))

    def test_api_key_env_requires_agent_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "README.md").write_text("# demo\n", encoding="utf-8")

            with (
                patch.object(cli, "read_identity", return_value=GitIdentity("Ada", "ada@example.com")),
                patch.object(cli.config_agents, "_agent_host_paths", return_value=_agent_paths(project / "home")),
                patch.dict(os.environ, {"ANTHROPIC_API_KEY": "secret"}, clear=False),
            ):
                with self.assertRaises(SystemExit) as raised:
                    cli.main([
                        "--dry-run",
                        "--no-forward-credentials",
                        "--api-key-env",
                        "ANTHROPIC_API_KEY",
                        str(project),
                        "python:3.12-slim",
                    ])

            self.assertIn("require --agent", str(raised.exception))

    def test_api_key_env_dry_run_redacts_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "README.md").write_text("# demo\n", encoding="utf-8")
            out = io.StringIO()

            with (
                patch.object(cli, "read_identity", return_value=GitIdentity("Ada", "ada@example.com")),
                patch.object(cli.config_agents, "_agent_host_paths", return_value=_agent_paths(project / "home")),
                patch.dict(os.environ, {"ANTHROPIC_API_KEY": "super-secret"}, clear=False),
                contextlib.redirect_stdout(out),
            ):
                rc = cli.main([
                    "--dry-run",
                    "--no-build",
                    "--no-forward-credentials",
                    "--agent",
                    "bash",
                    "--api-key-env",
                    "ANTHROPIC_API_KEY",
                    str(project),
                    "python:3.12-slim",
                ])

            self.assertEqual(rc, 0)
            output = out.getvalue()
            self.assertIn("ANTHROPIC_API_KEY=<redacted>", output)
            self.assertNotIn("super-secret", output)

    def test_api_key_env_file_parses_dotenv_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "# API keys",
                        "ANTHROPIC_API_KEY=sk-ant # local key",
                        "export AWS_ACCESS_KEY_ID='AKIA test'",
                        'AWS_SECRET_ACCESS_KEY="quoted secret"',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            values = cli._read_api_key_env_file(env_file)

        self.assertEqual(
            values,
            {
                "ANTHROPIC_API_KEY": "sk-ant",
                "AWS_ACCESS_KEY_ID": "AKIA test",
                "AWS_SECRET_ACCESS_KEY": "quoted secret",
            },
        )
