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
            self.assertIn(".project-sandbox/", content)
            self.assertIn(".devcontainer/", content)

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
            self.assertIn(".project-sandbox/", content)
            self.assertIn(".devcontainer/", content)

    def test_does_not_duplicate_preexisting_project_sandbox_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            # Project already ignores .project-sandbox/ but without our marker.
            (project / ".gitignore").write_text(
                "node_modules/\n.project-sandbox/\n", encoding="utf-8"
            )

            cli._update_project_gitignore(project)
            content = (project / ".gitignore").read_text(encoding="utf-8")

            self.assertEqual(content.count(".project-sandbox/\n"), 1)

    def test_adds_missing_project_sandbox_path_to_existing_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            existing = (
                "# project-sandbox — do not commit agent secrets\n"
                ".project-sandbox/claude/.credentials.json\n"
            )
            (project / ".gitignore").write_text(existing, encoding="utf-8")

            cli._update_project_gitignore(project)
            content = (project / ".gitignore").read_text(encoding="utf-8")

            self.assertIn(".project-sandbox/", content)

    def test_adds_missing_devcontainer_path_to_existing_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            existing = (
                "# project-sandbox — do not commit agent secrets\n"
                ".project-sandbox/\n"
            )
            (project / ".gitignore").write_text(existing, encoding="utf-8")

            cli._update_project_gitignore(project)
            content = (project / ".gitignore").read_text(encoding="utf-8")

            self.assertIn(".devcontainer/", content)


class RepoGitignoreTests(TestCase):
    def test_uv_lock_is_not_ignored(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        entries = {
            line.strip()
            for line in (repo_root / ".gitignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }

        self.assertNotIn("uv.lock", entries)


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
            ):
                self.assertIn(keep, content)
