import json
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
            self.assertNotIn("/home/agent/.claude/settings.json", mounts)
            self.assertNotIn("/home/agent/.claude.host", mounts)

    def test_render_mounts_persistent_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".project-sandbox").mkdir()

            _render(project)
            spec = json.loads(
                (project / ".devcontainer" / "devcontainer.json").read_text()
            )

            mounts = "\n".join(spec["mounts"])
            self.assertIn(
                "source=${localWorkspaceFolder}/.project-sandbox/history/bash_history,target=/home/agent/.bash_history,type=bind",
                mounts,
            )
            self.assertIn(
                "source=${localWorkspaceFolder}/.project-sandbox/history/claude_projects,target=/home/agent/.claude/projects,type=bind",
                mounts,
            )

            # Host targets for the bind mounts must be created.
            history_dir = project / ".project-sandbox" / "history"
            self.assertTrue((history_dir / "bash_history").exists())
            self.assertTrue((history_dir / "claude_projects").is_dir())

    def test_render_history_dir_is_excluded_by_gitignore(self) -> None:
        import sys as _sys

        _sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from project_sandbox import cli

        with tempfile.TemporaryDirectory() as tmp:
            context_dir = Path(tmp)
            cli._write_project_sandbox_gitignore(context_dir)
            content = (context_dir / ".gitignore").read_text(encoding="utf-8")
            self.assertIn("history/", content)

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
