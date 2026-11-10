import contextlib
import io
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import cli
from project_sandbox.git_identity import GitIdentity


class LaunchLeavesGitignoreAloneTests(TestCase):
    """A launch writes nothing to the project's root .gitignore (or creates one)."""

    def _launch(self, project: Path, *argv: str) -> int:
        with (
            patch.object(
                cli, "read_identity", return_value=GitIdentity("Ada", "ada@example.com")
            ),
            patch.object(cli.config_agents, "sync_credentials", return_value={}),
            patch.object(
                cli.container_cli,
                "select_runtime",
                return_value=cli.container_cli.DOCKER,
            ),
            patch.object(cli.container_cli, "ensure_system_started", return_value=0),
            patch.object(cli.container_cli, "host_build_identity", return_value=None),
            patch.object(cli.container_cli, "image_exists", return_value=True),
            patch.object(cli.container_cli, "build_image", return_value=0),
            patch.object(cli.container_cli, "run", return_value=0),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return cli.main([str(project), "python:3.12-slim", *argv])

    def test_existing_root_gitignore_is_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            original = b"node_modules/\n.env"  # no trailing newline on purpose
            (project / ".gitignore").write_bytes(original)

            self.assertEqual(self._launch(project, "--agent", "bash"), 0)

            self.assertEqual((project / ".gitignore").read_bytes(), original)
            self.assertFalse((project / ".project-sandbox").exists())

    def test_no_root_gitignore_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)

            self.assertEqual(self._launch(project, "--agent", "bash"), 0)

            self.assertFalse((project / ".gitignore").exists())
            # The only thing a launch leaves behind: an empty, self-ignoring
            # .devcontainer/ to mount the mask on.
            self.assertEqual(
                sorted(p.name for p in (project / ".devcontainer").iterdir()),
                [".gitignore"],
            )
            self.assertEqual(
                (project / ".devcontainer" / ".gitignore").read_text(encoding="utf-8"),
                "*\n",
            )
            self.assertEqual(
                sorted(p.name for p in project.iterdir()), [".devcontainer"]
            )

    def test_old_marker_lines_are_left_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            old = (
                "# project-sandbox — do not commit agent secrets\n"
                ".project-sandbox/\n.devcontainer/\n"
            )
            (project / ".gitignore").write_text(old, encoding="utf-8")

            self.assertEqual(self._launch(project, "--agent", "bash"), 0)

            self.assertEqual((project / ".gitignore").read_text(encoding="utf-8"), old)

    def test_gitignore_helpers_are_gone(self) -> None:
        self.assertFalse(hasattr(cli, "_update_project_gitignore"))
        self.assertFalse(hasattr(cli, "_write_project_sandbox_gitignore"))


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
