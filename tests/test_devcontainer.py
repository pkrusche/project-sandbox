import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import devcontainer
from project_sandbox.git_identity import GitIdentity


def _render(
    project: Path,
    *,
    firewall_enabled: bool = True,
    build_context: Path | None = None,
) -> Path:
    return devcontainer.render(
        project,
        identity=GitIdentity("Ada", "ada@example.com"),
        firewall_enabled=firewall_enabled,
        memory="8g",
        cpus=4,
        extra_mounts=[],
        build_context=build_context,
    )


class HostMemoryTests(TestCase):
    def test_normalizes_units(self) -> None:
        self.assertEqual(devcontainer._host_memory("8g"), "8gb")
        self.assertEqual(devcontainer._host_memory("8gb"), "8gb")
        self.assertEqual(devcontainer._host_memory("512m"), "512mb")
        self.assertEqual(devcontainer._host_memory("512mb"), "512mb")

    def test_returns_none_for_empty_or_unrecognized(self) -> None:
        self.assertIsNone(devcontainer._host_memory(None))
        self.assertIsNone(devcontainer._host_memory(""))
        self.assertIsNone(devcontainer._host_memory("lots"))

    def test_megabyte_memory_renders_valid_hostrequirements(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".project-sandbox").mkdir()

            devcontainer.render(
                project,
                identity=GitIdentity("Ada", "ada@example.com"),
                firewall_enabled=True,
                memory="512m",
                cpus=4,
                extra_mounts=[],
            )
            spec = json.loads(
                (project / ".devcontainer" / "devcontainer.json").read_text()
            )

            self.assertIn("--memory=512m", spec["runArgs"])
            self.assertEqual(spec["hostRequirements"]["memory"], "512mb")


class DevcontainerTests(TestCase):
    def test_render_writes_valid_devcontainer_json_with_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".project-sandbox").mkdir()

            _render(project)
            spec = json.loads(
                (project / ".devcontainer" / "devcontainer.json").read_text()
            )

            self.assertEqual(spec["remoteUser"], "agent")
            self.assertNotIn("CLAUDE_CONFIG_DIR", spec["containerEnv"])
            self.assertEqual(
                spec["containerEnv"]["CLAUDE_SECURESTORAGE_CONFIG_DIR"],
                "/home/agent/.claude",
            )
            self.assertEqual(spec["build"]["dockerfile"], "../.project-sandbox/Dockerfile.devcontainer")
            self.assertEqual(spec["build"]["context"], "../.project-sandbox")
            self.assertIn("--cap-add=NET_ADMIN", spec["runArgs"])
            self.assertIn("--cap-add=NET_RAW", spec["runArgs"])
            self.assertIn(
                "sudo -n /usr/local/bin/project-sandbox-init-firewall",
                spec["postStartCommand"],
            )
            mounts = "\n".join(spec["mounts"])
            self.assertIn(
                "source=${localWorkspaceFolder}/.project-sandbox/claude-devcontainer,target=/project-sandbox-config/claude,type=bind,readonly",
                mounts,
            )
            self.assertIn(
                "target=/project-sandbox-secrets/claude,type=bind,readonly",
                mounts,
            )
            self.assertIn(
                "source=${localWorkspaceFolder}/.project-sandbox/codex-devcontainer,target=/project-sandbox-config/codex,type=bind,readonly",
                mounts,
            )
            self.assertIn(
                "source=${localWorkspaceFolder}/.project-sandbox/workspace-mask,target=/workspace/.project-sandbox,type=bind,readonly",
                mounts,
            )
            self.assertIn(
                "source=${localWorkspaceFolder}/.project-sandbox/workspace-mask,target=/workspace/.devcontainer,type=bind,readonly",
                mounts,
            )
            self.assertTrue((project / ".project-sandbox" / "workspace-mask").is_dir())
            self.assertNotIn("/home/agent/.claude/settings.json", mounts)
            self.assertNotIn("/home/agent/.claude.host", mounts)

    def test_no_forward_credentials_renders_credential_free_devcontainer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".project-sandbox").mkdir()

            devcontainer.render(
                project,
                identity=GitIdentity("Ada", "ada@example.com"),
                firewall_enabled=True,
                memory="8g",
                cpus=4,
                extra_mounts=[],
                forward_credentials=False,
            )
            mounts = "\n".join(
                json.loads(
                    (project / ".devcontainer" / "devcontainer.json").read_text()
                )["mounts"]
            )

            # No staged host tokens are wired...
            self.assertNotIn("/project-sandbox-secrets/", mounts)
            # ...but generated, non-secret config still is.
            self.assertIn("target=/project-sandbox-config/claude,type=bind,readonly", mounts)
            self.assertIn("target=/project-sandbox-config/codex,type=bind,readonly", mounts)

    def test_workspace_sandbox_mask_overrides_custom_mount(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".project-sandbox").mkdir()
            custom_mount = "source=/tmp/custom,target=/workspace/.project-sandbox,type=bind"

            devcontainer.render(
                project,
                identity=GitIdentity("Ada", "ada@example.com"),
                firewall_enabled=True,
                memory="8g",
                cpus=4,
                extra_mounts=[custom_mount],
            )
            spec = json.loads(
                (project / ".devcontainer" / "devcontainer.json").read_text()
            )

            mask_mount = (
                "source=${localWorkspaceFolder}/.project-sandbox/workspace-mask,"
                "target=/workspace/.project-sandbox,type=bind,readonly"
            )
            self.assertIn(custom_mount, spec["mounts"])
            self.assertIn(mask_mount, spec["mounts"])
            self.assertLess(spec["mounts"].index(custom_mount), spec["mounts"].index(mask_mount))
            self.assertIn(
                "${localWorkspaceFolder}/.project-sandbox/workspace-mask",
                spec["initializeCommand"],
            )

    def test_render_mounts_persistent_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".project-sandbox").mkdir()

            _render(project)
            spec = json.loads(
                (project / ".devcontainer" / "devcontainer.json").read_text()
            )

            mounts = "\n".join(spec["mounts"])
            # Both sources are directories (apple/container rejects file mounts).
            self.assertIn(
                "source=${localWorkspaceFolder}/.project-sandbox/history/shell,target=/home/agent/.bash_history.d,type=bind",
                mounts,
            )
            self.assertIn(
                "source=${localWorkspaceFolder}/.project-sandbox/history/claude_projects,target=/home/agent/.claude/projects,type=bind",
                mounts,
            )
            # HISTFILE redirects bash history into the mounted shell directory.
            self.assertEqual(
                spec["containerEnv"]["HISTFILE"],
                "/home/agent/.bash_history.d/bash_history",
            )

            # Host directories for the bind mounts must be created.
            history_dir = project / ".project-sandbox" / "history"
            self.assertTrue((history_dir / "shell").is_dir())
            self.assertTrue((history_dir / "claude_projects").is_dir())

    def test_initialize_command_recreates_missing_history_sources(self) -> None:
        # The history dir is gitignored, so the bind sources can be missing at
        # container-create time. initializeCommand runs on the host first and
        # must recreate them with the right types, or the mounts fail to start.
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".project-sandbox").mkdir()

            _render(project)
            spec = json.loads(
                (project / ".devcontainer" / "devcontainer.json").read_text()
            )
            command = spec["initializeCommand"]
            # Array (argv) form: each element is a literal argument, no shell.
            self.assertIsInstance(command, list)

            # Simulate a fresh/cleaned checkout: the gitignored history dir is gone.
            shutil.rmtree(project / ".project-sandbox" / "history")

            # Run the host command exactly as a devcontainer host would, with the
            # ${localWorkspaceFolder} variable resolved to the project root.
            resolved = [
                arg.replace("${localWorkspaceFolder}", str(project))
                for arg in command
            ]
            subprocess.run(resolved, check=True)

            history_dir = project / ".project-sandbox" / "history"
            self.assertTrue((history_dir / "shell").is_dir())
            self.assertTrue((history_dir / "claude_projects").is_dir())

    def test_initialize_command_is_array_form_safe_for_apostrophe_paths(self) -> None:
        # A workspace path containing an apostrophe must not break the host
        # initializeCommand. The array (argv) form passes each path as a single
        # literal argument, so no shell quoting can corrupt it.
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "ada's project"
            (project / ".project-sandbox").mkdir(parents=True)

            _render(project)
            spec = json.loads(
                (project / ".devcontainer" / "devcontainer.json").read_text()
            )
            command = spec["initializeCommand"]

            # Array form, not a shell string.
            self.assertIsInstance(command, list)
            self.assertEqual(command[:2], ["mkdir", "-p"])

            # The history paths appear as whole literal elements with the
            # apostrophe intact and no shell quoting wrapping the value.
            self.assertIn(
                "${localWorkspaceFolder}/.project-sandbox/history/shell",
                command,
            )
            self.assertIn(
                "${localWorkspaceFolder}/.project-sandbox/history/claude_projects",
                command,
            )

            # Simulate the host resolving ${localWorkspaceFolder} to the real
            # apostrophe-containing path and running the argv directly (no shell):
            # the correct directories must be created.
            shutil.rmtree(project / ".project-sandbox" / "history")
            resolved = [
                arg.replace("${localWorkspaceFolder}", str(project))
                for arg in command
            ]
            subprocess.run(resolved, check=True)

            history_dir = project / ".project-sandbox" / "history"
            self.assertTrue((history_dir / "shell").is_dir())
            self.assertTrue((history_dir / "claude_projects").is_dir())

    def test_render_history_dir_is_excluded_by_gitignore(self) -> None:
        import sys as _sys

        _sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from project_sandbox import cli

        with tempfile.TemporaryDirectory() as tmp:
            context_dir = Path(tmp)
            cli._write_project_sandbox_gitignore(context_dir)
            content = (context_dir / ".gitignore").read_text(encoding="utf-8")
            # history/ is already excluded by the leading "*" glob (nothing
            # negates it back in), so an explicit "history/" entry would be
            # redundant and is no longer written.
            self.assertIn("*\n", content)
            self.assertNotIn("history/", content)

    def test_render_creates_relative_symlinks_into_project_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".project-sandbox").mkdir()

            _render(project)
            dc_dir = project / ".devcontainer"

            for name in ("Dockerfile", "init-firewall.sh", "claude", "claude-devcontainer", "codex", "codex-devcontainer"):
                link = dc_dir / name
                self.assertTrue(link.is_symlink(), f"{name} is not a symlink")
                target = link.readlink()
                self.assertTrue(str(target).startswith("../.project-sandbox"))

    def test_render_overwrites_existing_devcontainer_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".project-sandbox").mkdir()
            spec_path = project / ".devcontainer" / "devcontainer.json"
            spec_path.parent.mkdir()
            spec_path.write_text('{"old":true}\n', encoding="utf-8")

            _render(project)
            spec = json.loads(spec_path.read_text(encoding="utf-8"))

            self.assertEqual(spec["remoteUser"], "agent")
            self.assertNotIn("old", spec)

    def test_render_overwrites_existing_claude_host_mount(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            dc_dir = project / ".devcontainer"
            (project / ".project-sandbox").mkdir()
            dc_dir.mkdir()
            spec_path = dc_dir / "devcontainer.json"
            spec_path.write_text(
                '{"mounts":["source=${localEnv:HOME}/.claude,target=/home/agent/.claude.host,type=bind,readonly"]}\n',
                encoding="utf-8",
            )

            _render(project)

            self.assertNotIn(
                "/home/agent/.claude.host",
                spec_path.read_text(encoding="utf-8"),
            )

    def test_render_overwrites_existing_missing_claude_secrets_mount(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            dc_dir = project / ".devcontainer"
            (project / ".project-sandbox").mkdir()
            dc_dir.mkdir()
            spec_path = dc_dir / "devcontainer.json"
            spec_path.write_text(
                '{"mounts":["source=${localWorkspaceFolder}/.project-sandbox/claude,target=/project-sandbox-config/claude,type=bind,readonly"]}\n',
                encoding="utf-8",
            )

            _render(project)

            self.assertIn(
                "/project-sandbox-secrets/claude",
                spec_path.read_text(encoding="utf-8"),
            )

    def test_render_overwrites_existing_claude_config_dir_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            dc_dir = project / ".devcontainer"
            (project / ".project-sandbox").mkdir()
            dc_dir.mkdir()
            spec_path = dc_dir / "devcontainer.json"
            spec_path.write_text(
                '{"containerEnv":{"CLAUDE_CONFIG_DIR":"/home/agent/.claude"}}\n',
                encoding="utf-8",
            )

            _render(project)

            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            self.assertNotIn("CLAUDE_CONFIG_DIR", spec["containerEnv"])

    def test_render_omits_capabilities_when_firewall_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".project-sandbox").mkdir()

            _render(project, firewall_enabled=False)
            spec = json.loads(
                (project / ".devcontainer" / "devcontainer.json").read_text()
            )

            self.assertNotIn("--cap-add=NET_ADMIN", spec["runArgs"])
            self.assertNotIn("project-sandbox-init-firewall", spec["postStartCommand"])

    def test_render_mounts_staged_agent_credentials_when_hosts_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            (project / ".project-sandbox").mkdir(parents=True)
            (fake_home / ".codex").mkdir(parents=True)
            (fake_home / ".config" / "opencode").mkdir(parents=True)
            credentials = {
                "claude": Path(tmp) / "secrets" / "claude",
                "codex": Path(tmp) / "secrets" / "codex",
                "opencode": Path(tmp) / "secrets" / "opencode",
            }

            with patch.object(devcontainer.Path, "home", return_value=fake_home):
                devcontainer.render(
                    project,
                    identity=GitIdentity("Ada", "ada@example.com"),
                    firewall_enabled=True,
                    memory="8g",
                    cpus=4,
                    extra_mounts=[],
                    credential_dirs=credentials,
                )

            spec = json.loads(
                (project / ".devcontainer" / "devcontainer.json").read_text()
            )
            mounts = "\n".join(spec["mounts"])
            self.assertIn(
                f"source={credentials['codex'].resolve(strict=False)},target=/project-sandbox-secrets/codex,type=bind,readonly",
                mounts,
            )
            self.assertIn(
                f"source={credentials['opencode'].resolve(strict=False)},target=/project-sandbox-secrets/opencode,type=bind,readonly",
                mounts,
            )
            self.assertNotIn("${localEnv:HOME}/.codex", mounts)
            self.assertNotIn("${localEnv:HOME}/.config/opencode", mounts)

    def test_initialize_command_recreates_missing_credential_dirs(self) -> None:
        # Staged credentials live under a tmp-style directory (see
        # config_agents.CREDENTIALS_ROOT) that a host reboot or tmp-reaper can
        # remove. Without recreating these sources, "Reopen in Container"
        # fails with a bind-mount error until the CLI is re-run from scratch.
        # initializeCommand must mkdir -p the credential dirs too, not just
        # the history/mask dirs, so the bind mounts always succeed (empty
        # dirs self-heal; real content still requires re-running the CLI).
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            (project / ".project-sandbox").mkdir(parents=True)
            (fake_home / ".codex").mkdir(parents=True)
            (fake_home / ".config" / "opencode").mkdir(parents=True)
            credentials = {
                "claude-devcontainer": Path(tmp) / "secrets" / "claude-devcontainer",
                "codex": Path(tmp) / "secrets" / "codex",
                "opencode": Path(tmp) / "secrets" / "opencode",
            }
            # Simulate credentials that were staged once but whose /tmp
            # directory has since been reaped/rebooted away.
            for path in credentials.values():
                path.mkdir(parents=True)

            with patch.object(devcontainer.Path, "home", return_value=fake_home):
                devcontainer.render(
                    project,
                    identity=GitIdentity("Ada", "ada@example.com"),
                    firewall_enabled=True,
                    memory="8g",
                    cpus=4,
                    extra_mounts=[],
                    credential_dirs=credentials,
                )

            spec = json.loads(
                (project / ".devcontainer" / "devcontainer.json").read_text()
            )
            command = spec["initializeCommand"]
            self.assertIsInstance(command, list)

            resolved_claude = credentials["claude-devcontainer"].resolve(strict=False).as_posix()
            resolved_codex = credentials["codex"].resolve(strict=False).as_posix()
            resolved_opencode = credentials["opencode"].resolve(strict=False).as_posix()
            self.assertIn(resolved_claude, command)
            self.assertIn(resolved_codex, command)
            self.assertIn(resolved_opencode, command)

            # Now actually reap the staged credential directories, as a
            # tmp-cleaner or reboot would, and confirm the host-run
            # initializeCommand recreates them so the bind mounts would
            # succeed.
            for path in credentials.values():
                shutil.rmtree(path)
                self.assertFalse(path.exists())

            resolved = [
                arg.replace("${localWorkspaceFolder}", str(project)) for arg in command
            ]
            subprocess.run(resolved, check=True)

            for path in credentials.values():
                self.assertTrue(path.is_dir())

    def test_initialize_command_omits_credential_dirs_without_forwarding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            (project / ".project-sandbox").mkdir(parents=True)
            credentials = {
                "claude-devcontainer": Path(tmp) / "secrets" / "claude-devcontainer",
                "codex": Path(tmp) / "secrets" / "codex",
                "opencode": Path(tmp) / "secrets" / "opencode",
            }

            with patch.object(devcontainer.Path, "home", return_value=fake_home):
                devcontainer.render(
                    project,
                    identity=GitIdentity("Ada", "ada@example.com"),
                    firewall_enabled=True,
                    memory="8g",
                    cpus=4,
                    extra_mounts=[],
                    credential_dirs=credentials,
                    forward_credentials=False,
                )

            spec = json.loads(
                (project / ".devcontainer" / "devcontainer.json").read_text()
            )
            command = spec["initializeCommand"]
            for path in credentials.values():
                self.assertNotIn(path.resolve(strict=False).as_posix(), command)
            # Only the history/mask dirs remain: mkdir, -p, and three targets.
            self.assertEqual(len(command), 5)

    def test_render_escapes_injection_in_strings(self) -> None:
        # Quotes, braces, and newlines in the project name, git identity, and an
        # extra --mount value must stay JSON string values: they must not close
        # a string and inject new devcontainer fields, nor corrupt the file.
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / 'pro"ject\n{"injected": true}'
            (project / ".project-sandbox").mkdir(parents=True)

            malicious_mount = (
                'source=/tmp/x,target=/x,type=bind","postStartCommand":"rm -rf /'
            )
            devcontainer.render(
                project,
                identity=GitIdentity(
                    'Ada"\n"injectedName":"x', 'a@b.com","injectedEmail":"x'
                ),
                firewall_enabled=True,
                memory="8g",
                cpus=4,
                extra_mounts=[malicious_mount],
            )

            spec_path = project / ".devcontainer" / "devcontainer.json"
            # The file must still be valid JSON despite the hostile input.
            spec = json.loads(spec_path.read_text(encoding="utf-8"))

            # Structure is intact and no injected keys appear anywhere.
            self.assertEqual(spec["remoteUser"], "agent")
            self.assertEqual(spec["postStartCommand"].count("rm -rf /"), 0)
            self.assertNotIn("injected", spec)
            self.assertNotIn("injectedName", spec["remoteEnv"])
            self.assertNotIn("injectedEmail", spec["remoteEnv"])

            # The malicious values survive intact as plain string values.
            self.assertTrue(spec["name"].startswith('pro"ject\n{"injected": true}'))
            self.assertEqual(
                spec["remoteEnv"]["PROJECT_SANDBOX_USER_NAME"],
                'Ada"\n"injectedName":"x',
            )
            self.assertEqual(
                spec["remoteEnv"]["PROJECT_SANDBOX_USER_EMAIL"],
                'a@b.com","injectedEmail":"x',
            )
            self.assertIn(malicious_mount, spec["mounts"])

    def test_render_sets_uv_offline_when_firewall_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".project-sandbox").mkdir()

            _render(project, firewall_enabled=True)
            spec = json.loads(
                (project / ".devcontainer" / "devcontainer.json").read_text()
            )

            self.assertEqual(spec["containerEnv"]["UV_OFFLINE"], "1")

    def test_render_omits_uv_offline_when_firewall_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".project-sandbox").mkdir()

            _render(project, firewall_enabled=False)
            spec = json.loads(
                (project / ".devcontainer" / "devcontainer.json").read_text()
            )

            self.assertNotIn("UV_OFFLINE", spec["containerEnv"])

    def test_render_can_use_project_root_build_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".project-sandbox").mkdir()

            _render(project, build_context=project)
            spec = json.loads(
                (project / ".devcontainer" / "devcontainer.json").read_text()
            )

            self.assertEqual(spec["build"]["dockerfile"], "../.project-sandbox/Dockerfile.devcontainer")
            self.assertEqual(spec["build"]["context"], "..")
