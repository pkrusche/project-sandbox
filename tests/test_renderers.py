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
            )
            claude = config_claude.render(context)
            codex = config_codex.render(context)
            fw = firewall.render(
                context,
                extra_domains=["internal.example.com"],
            )

            self.assertIn(
                "FROM python:3.12-slim",
                (context / "Dockerfile").read_text(encoding="utf-8"),
            )
            docker_text = (context / "Dockerfile").read_text(encoding="utf-8")
            self.assertIn("npm install -g @anthropic-ai/claude-code", docker_text)
            self.assertIn("npm install -g @openai/codex", docker_text)
            self.assertIn("npm install -g opencode-ai", docker_text)
            self.assertIn("npm install -g @github/copilot", docker_text)
            self.assertIn("bypassPermissions", claude.read_text(encoding="utf-8"))
            codex_text = codex.read_text(encoding="utf-8")
            self.assertIn(
                'approval_policy = "never"', codex_text
            )
            self.assertIn("[analytics]\nenabled = false", codex_text)
            self.assertIn("[feedback]\nenabled = false", codex_text)
            firewall_text = fw.read_text(encoding="utf-8")
            self.assertIn('"api.openai.com"', firewall_text)
            self.assertNotIn("statsig", firewall_text)
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
                opencode_home_host_abs=project / ".config/opencode",
                copilot_home_host_abs=project / ".copilot",
                firewall_enabled=True,
                agent="claude",
                extra_envs=["KEY=value with spaces"],
            )

            text = out.read_text(encoding="utf-8")
            self.assertIn("'type=bind,source=", text)
            self.assertIn("project with spaces", text)
            self.assertIn("--env 'KEY=value with spaces'", text)
            self.assertIn("/home/agent/.config/opencode.host", text)
            self.assertIn("/home/agent/.copilot.host", text)

    def test_launcher_firewall_enabled_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run-claude"
            launcher.render(
                output=out,
                image_tag="project-sandbox:test",
                memory="8g",
                cpus=4,
                project_abs=Path(tmp) / "proj",
                claude_settings_abs=Path(tmp) / "settings.json",
                codex_config_abs=Path(tmp) / "config.toml",
                claude_home_host_abs=None,
                codex_home_host_abs=None,
                firewall_enabled=True,
                agent="claude",
                extra_envs=[],
            )
            text = out.read_text(encoding="utf-8")
            # Default: firewall on, _NO_FIREWALL starts at 0
            self.assertIn("_NO_FIREWALL=0", text)
            # Runtime --no-firewall parsing must be present
            self.assertIn('"$_arg" = "--no-firewall"', text)
            # NET_ADMIN is added only when _NO_FIREWALL is 0
            self.assertIn('if [ "$_NO_FIREWALL" = "0" ]', text)
            self.assertIn("NET_ADMIN", text)
            # PROJECT_SANDBOX_NO_FIREWALL=1 is set only when _NO_FIREWALL is 1
            self.assertIn('if [ "$_NO_FIREWALL" = "1" ]', text)
            self.assertIn("PROJECT_SANDBOX_NO_FIREWALL=1", text)

    def test_launcher_firewall_disabled_baked_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run-claude"
            launcher.render(
                output=out,
                image_tag="project-sandbox:test",
                memory="8g",
                cpus=4,
                project_abs=Path(tmp) / "proj",
                claude_settings_abs=Path(tmp) / "settings.json",
                codex_config_abs=Path(tmp) / "config.toml",
                claude_home_host_abs=None,
                codex_home_host_abs=None,
                firewall_enabled=False,
                agent="claude",
                extra_envs=[],
            )
            text = out.read_text(encoding="utf-8")
            # Generated with firewall off: default is _NO_FIREWALL=1
            self.assertIn("_NO_FIREWALL=1", text)
            # NET_ADMIN guard and env var are still emitted (runtime toggle works both ways)
            self.assertIn("NET_ADMIN", text)
            self.assertIn("PROJECT_SANDBOX_NO_FIREWALL=1", text)

    def test_dockerfile_renderer_can_skip_agent_installs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            dockerfile.render(
                context,
                base_image="python:3.12-slim",
                install_agents=("codex",),
            )
            text = (context / "Dockerfile").read_text(encoding="utf-8")
            self.assertNotIn("@anthropic-ai/claude-code", text)
            self.assertIn("@openai/codex", text)
            self.assertNotIn("opencode-ai", text)
            self.assertNotIn("@github/copilot", text)

    def test_entrypoint_supports_all_headless_agents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            dockerfile.render_entrypoint(context)
            text = (context / "entrypoint.sh").read_text(encoding="utf-8")
            self.assertIn("claude-headless", text)
            self.assertIn("codex-headless", text)
            self.assertIn("opencode-headless", text)
            self.assertIn("copilot-headless", text)
