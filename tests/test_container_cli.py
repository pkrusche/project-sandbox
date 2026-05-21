import sys
import tempfile
from pathlib import Path
from unittest import TestCase
import contextlib
import io

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox.container_cli import build_image, build_run_argv
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
        self.assertIn(
            f"type=bind,source={root / 'claude'},target=/project-sandbox-config/claude,readonly",
            cmd,
        )
        self.assertIn(
            f"type=bind,source={root / 'codex'},target=/project-sandbox-config/codex,readonly",
            cmd,
        )
        self.assertNotIn(
            f"type=bind,source={root / 'claude/settings.json'},target=/home/agent/.claude/settings.json,readonly",
            cmd,
        )
        self.assertEqual(
            cmd[-3:], ["project-sandbox:test", "project-sandbox-run", "claude-headless"]
        )

    def test_build_run_argv_mounts_opencode_and_copilot_homes_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            opencode_home = root / ".config" / "opencode"
            copilot_home = root / ".copilot"
            opencode_home.mkdir(parents=True)
            copilot_home.mkdir(parents=True)

            cmd = build_run_argv(
                image="project-sandbox:test",
                project_abs=root / "workspace",
                claude_cfg=root / "claude/settings.json",
                codex_cfg=root / "codex/config.toml",
                claude_home_host=None,
                codex_home_host=None,
                opencode_home_host=opencode_home,
                copilot_home_host=copilot_home,
                identity=GitIdentity("Ada Lovelace", "ada@example.com"),
                memory="8g",
                cpus=4,
                extra_mounts=[],
                agent="copilot",
                firewall_enabled=False,
                interactive=True,
            )

        self.assertIn(
            f"type=bind,source={opencode_home},target=/home/agent/.config/opencode.host,readonly",
            cmd,
        )
        self.assertIn(
            f"type=bind,source={copilot_home},target=/home/agent/.copilot.host,readonly",
            cmd,
        )

    def test_build_image_can_use_generated_dockerfile_with_project_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = root / ".project-sandbox"
            context.mkdir()
            out = io.StringIO()

            with contextlib.redirect_stdout(out):
                rc = build_image(
                    context_dir=context,
                    image_tag="project-sandbox:test",
                    build_context=root,
                    dockerfile_path=context / "Dockerfile",
                    dry_run=True,
                )

        self.assertEqual(rc, 0)
        self.assertEqual(
            out.getvalue().strip(),
            f"container build -t project-sandbox:test -f {context / 'Dockerfile'} {root}",
        )
