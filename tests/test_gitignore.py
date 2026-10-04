import subprocess
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
                "# project-sandbox — do not commit agent secrets\n.project-sandbox/\n"
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
            for line in (repo_root / ".gitignore")
            .read_text(encoding="utf-8")
            .splitlines()
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

    def test_history_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            subprocess.run(
                ["git", "init", str(project)], check=True, capture_output=True
            )
            context = project / ".project-sandbox"
            context.mkdir()
            cli._write_project_sandbox_gitignore(context)
            history = context / "history" / "session.json"
            history.parent.mkdir()
            history.write_text("{}")
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(project),
                    "-c",
                    "core.excludesFile=/dev/null",
                    "check-ignore",
                    "--no-index",
                    ".project-sandbox/history/session.json",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                result.stdout.strip(), ".project-sandbox/history/session.json"
            )


class GitignoreMechanismInteractionTests(TestCase):
    """_update_project_gitignore unconditionally ignores the whole
    .project-sandbox/ directory from the project's root .gitignore. Git never
    descends into an already-ignored directory, so the "!"-negation patterns
    written by _write_project_sandbox_gitignore into the nested
    .project-sandbox/.gitignore can never re-include anything while that
    root-level rule is in place. This is inherent to how the two mechanisms
    interact, so the nested file must clearly document that its negations
    only matter if a user manually removes/edits the root rule.
    """

    def test_nested_gitignore_documents_dependency_on_root_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            context = project / ".project-sandbox"
            context.mkdir()
            cli._write_project_sandbox_gitignore(context)
            nested_content = (context / ".gitignore").read_text(encoding="utf-8")
            cli._update_project_gitignore(project)
            root_content = (project / ".gitignore").read_text(encoding="utf-8")

            self.assertIn(".project-sandbox/", root_content)
            self.assertIn(
                "only have an effect if you have manually removed", nested_content
            )
            self.assertIn("!Dockerfile", nested_content)
