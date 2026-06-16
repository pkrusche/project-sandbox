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
        self.assertIn("--runtime", help_text)
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
            "PROJECT_SANDBOX_PROMPT_FILE=/workspace/.project-sandbox-prompt",
            out,
        )
        self.assertIn("target=/workspace/.project-sandbox-prompt,readonly", out)
        self.assertNotIn("PROJECT_SANDBOX_PROMPT=echo ok", out)

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

            cmd, log_path, unsupervised = cli._build_session_command(
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
                f"type=bind,source={prompt_file.resolve()},target=/workspace/.project-sandbox-prompt,readonly",
                cmd,
            )
            self.assertIn(
                "PROJECT_SANDBOX_PROMPT_FILE=/workspace/.project-sandbox-prompt",
                cmd,
            )
            self.assertNotIn("PROJECT_SANDBOX_PROMPT=echo ok", cmd)

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

            cmd, log_path, unsupervised = cli._build_session_command(
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
            bash_history = history_dir / "bash_history"
            claude_projects = history_dir / "claude_projects"

            self.assertFalse(unsupervised)
            self.assertIsNone(log_path)
            # history files/dirs must be created
            self.assertTrue(bash_history.exists())
            self.assertTrue(claude_projects.is_dir())
            # bash_history must be mounted at /root/.bash_history
            self.assertIn(
                f"type=bind,source={bash_history.resolve()},target=/root/.bash_history",
                cmd,
            )
            # claude_projects dir must be mounted at /root/.claude/projects
            self.assertIn(
                f"type=bind,source={claude_projects.resolve()},target=/root/.claude/projects",
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

            cmd, log_path, unsupervised = cli._build_session_command(
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
            self.assertNotIn("/root/.bash_history", " ".join(cmd))
            self.assertNotIn("/root/.claude/projects", " ".join(cmd))

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
