import sys
import tempfile
from pathlib import Path
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import config_claude, config_codex, dockerfile, firewall, launcher


class RendererTests(TestCase):
    def test_config_and_firewall_renderers_write_expected_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)

            dockerfile.render(
                context,
                base_image="python:3.12-slim",
                install_claude=True,
                install_codex=False,
            )
            claude = config_claude.render(context)
            codex = config_codex.render(context)
            fw = firewall.render(
                context,
                allow_openai=True,
                extra_domains=["internal.example.com"],
            )

            self.assertIn(
                "FROM python:3.12-slim",
                (context / "Dockerfile").read_text(encoding="utf-8"),
            )
            self.assertIn("bypassPermissions", claude.read_text(encoding="utf-8"))
            self.assertIn(
                'approval_policy = "never"', codex.read_text(encoding="utf-8")
            )
            firewall_text = fw.read_text(encoding="utf-8")
            self.assertIn('"api.openai.com"', firewall_text)
            self.assertIn('"internal.example.com"', firewall_text)

    def test_launcher_shell_quotes_paths_with_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project with spaces"
            project.mkdir()
            out = root / "run-claude"

            launcher.render(
                output=out,
                image_tag="project-sandbox:test",
                memory="8g",
                cpus=4,
                project_abs=project,
                claude_settings_abs=project / ".project-sandbox/claude/settings.json",
                codex_config_abs=project / ".project-sandbox/codex/config.toml",
                claude_home_host_abs=None,
                codex_home_host_abs=None,
                firewall_enabled=True,
                agent="claude",
                extra_envs=["KEY=value with spaces"],
            )

            text = out.read_text(encoding="utf-8")
            self.assertIn("'type=bind,source=", text)
            self.assertIn("project with spaces", text)
            self.assertIn("--env 'KEY=value with spaces'", text)
