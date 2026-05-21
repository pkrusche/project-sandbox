import sys
import tempfile
from pathlib import Path
from unittest import TestCase


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import cli


class UpdateProjectGitignoreTests(TestCase):
    def test_creates_gitignore_with_secret_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)

            cli._update_project_gitignore(project)
            content = (project / ".gitignore").read_text(encoding="utf-8")

            self.assertIn("# project-sandbox — do not commit agent secrets", content)
            self.assertIn(".project-sandbox/claude/.credentials.json", content)
            self.assertIn(".project-sandbox/claude/.claude.json", content)
            self.assertIn(".project-sandbox/codex/auth.json", content)

    def test_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)

            cli._update_project_gitignore(project)
            first = (project / ".gitignore").read_text(encoding="utf-8")
            cli._update_project_gitignore(project)
            second = (project / ".gitignore").read_text(encoding="utf-8")

            self.assertEqual(first, second)

    def test_appends_to_existing_gitignore_without_clobbering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            existing = "node_modules/\n.env\n"
            (project / ".gitignore").write_text(existing, encoding="utf-8")

            cli._update_project_gitignore(project)
            content = (project / ".gitignore").read_text(encoding="utf-8")

            self.assertTrue(content.startswith(existing))
            self.assertIn(".project-sandbox/claude/.credentials.json", content)


class WriteProjectSandboxGitignoreTests(TestCase):
    def test_whitelists_committed_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)

            cli._write_project_sandbox_gitignore(context)
            content = (context / ".gitignore").read_text(encoding="utf-8")

            self.assertIn("*\n", content)
            for keep in (
                "!claude/settings.json",
                "!codex/config.toml",
                "!init-firewall.sh",
                "!Dockerfile",
                "!entrypoint.sh",
                "!bin/run-claude",
                "!bin/run-codex",
                "!bin/run-opencode",
                "!bin/run-copilot",
            ):
                self.assertIn(keep, content)
