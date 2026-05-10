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


class CliTests(TestCase):
    def test_help_includes_core_options(self) -> None:
        parser = cli.build_parser()
        with self.assertRaises(SystemExit) as raised, contextlib.redirect_stdout(io.StringIO()) as stdout:
            parser.parse_args(["--help"])

        self.assertEqual(raised.exception.code, 0)
        help_text = stdout.getvalue()
        self.assertIn("--dry-run", help_text)
        self.assertIn("--branch", help_text)

    def test_dry_run_does_not_write_project_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "README.md").write_text("# demo\n", encoding="utf-8")

            with patch.object(cli, "read_identity", return_value=GitIdentity("Ada", "ada@example.com")):
                rc = cli.main(["--dry-run", "--no-build", str(project), "python:3.12-slim"])

            self.assertEqual(rc, 0)
            self.assertFalse((project / ".project-sandbox").exists())
            self.assertFalse((project / ".gitignore").exists())

    def test_branch_is_temporarily_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as raised:
                cli.main(["--branch", "agent/demo", tmp, "python:3.12-slim"])

        self.assertIn("temporarily disabled", str(raised.exception))
