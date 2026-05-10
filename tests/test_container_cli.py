import sys
import tempfile
from pathlib import Path
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox.container_cli import build_run_argv
from project_sandbox.git_identity import GitIdentity


class ContainerCliTests(TestCase):
    def test_build_run_argv_uses_arg_list_for_headless_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cmd = build_run_argv(
                image="project-sandbox:test",
                project_abs=root / "workspace",
                claude_cfg=root / "claude/settings.json",
                codex_cfg=root / "codex/config.toml",
                claude_home_host=root / "missing-claude",
                codex_home_host=root / "missing-codex",
                identity=GitIdentity("Ada Lovelace", "ada@example.com"),
                memory="8g",
                cpus=4,
                extra_mounts=[
                    "type=bind,source=/tmp/prompt.txt,target=/workspace/prompt,readonly"
                ],
                agent="claude-headless",
                firewall_enabled=True,
                interactive=False,
                extra_env=["PROJECT_SANDBOX_PROMPT=fix the tests"],
            )

        self.assertNotIn("-it", cmd)
        self.assertIn("--cap-add", cmd)
        self.assertIn("NET_ADMIN", cmd)
        self.assertIn("PROJECT_SANDBOX_PROMPT=fix the tests", cmd)
        self.assertEqual(
            cmd[-3:], ["project-sandbox:test", "project-sandbox-run", "claude-headless"]
        )
