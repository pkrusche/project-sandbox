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
    refresh: bool = False,
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
        refresh=refresh,
    )


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
            self.assertEqual(spec["build"]["dockerfile"], "../.project-sandbox/Dockerfile")
            self.assertEqual(spec["build"]["context"], "../.project-sandbox")
            self.assertIn("--cap-add=NET_ADMIN", spec["runArgs"])
            self.assertIn("--cap-add=NET_RAW", spec["runArgs"])
            self.assertIn(
                "sudo -n /usr/local/bin/project-sandbox-init-firewall",
                spec["postStartCommand"],
            )
            mounts = "\n".join(spec["mounts"])
            self.assertIn(
                "source=${localWorkspaceFolder}/.project-sandbox/claude,target=/project-sandbox-config/claude,type=bind,readonly",
                mounts,
            )
            self.assertIn(
                "source=${localWorkspaceFolder}/.project-sandbox/codex,target=/project-sandbox-config/codex,type=bind,readonly",
                mounts,
            )
            self.assertNotIn("/home/agent/.claude/settings.json", mounts)

    def test_render_creates_relative_symlinks_into_project_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".project-sandbox").mkdir()

            _render(project)
            dc_dir = project / ".devcontainer"

            for name in ("Dockerfile", "init-firewall.sh", "claude", "codex"):
                link = dc_dir / name
                self.assertTrue(link.is_symlink(), f"{name} is not a symlink")
                target = link.readlink()
                self.assertTrue(str(target).startswith("../.project-sandbox"))

    def test_render_is_idempotent_without_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".project-sandbox").mkdir()

            _render(project)
            spec_path = project / ".devcontainer" / "devcontainer.json"
            mtime = spec_path.stat().st_mtime_ns

            _render(project)
            self.assertEqual(spec_path.stat().st_mtime_ns, mtime)

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

    def test_render_mounts_opencode_and_copilot_hosts_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            (project / ".project-sandbox").mkdir(parents=True)
            (fake_home / ".config" / "opencode").mkdir(parents=True)
            (fake_home / ".copilot").mkdir(parents=True)

            with patch.object(devcontainer.Path, "home", return_value=fake_home):
                _render(project, refresh=True)

            spec = json.loads(
                (project / ".devcontainer" / "devcontainer.json").read_text()
            )
            mounts = "\n".join(spec["mounts"])
            self.assertIn(
                "source=${localEnv:HOME}/.config/opencode,target=/home/agent/.config/opencode.host,type=bind,readonly",
                mounts,
            )
            self.assertIn(
                "source=${localEnv:HOME}/.copilot,target=/home/agent/.copilot.host,type=bind,readonly",
                mounts,
            )

    def test_render_can_use_project_root_build_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".project-sandbox").mkdir()

            _render(project, build_context=project)
            spec = json.loads(
                (project / ".devcontainer" / "devcontainer.json").read_text()
            )

            self.assertEqual(spec["build"]["dockerfile"], "../.project-sandbox/Dockerfile")
            self.assertEqual(spec["build"]["context"], "..")
