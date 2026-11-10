"""One-time migration of a legacy ``<project>/.project-sandbox`` (task 7.1)."""

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import cli, observability, paths, state_migration
from project_sandbox.git_identity import GitIdentity


class MigrationTests(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.project = self.root / "project"
        self.legacy = self.project / ".project-sandbox"
        (self.legacy / "history" / "shell").mkdir(parents=True)
        (self.legacy / "history" / "shell" / "bash_history").write_text("ls\n")
        (self.legacy / "history" / "claude_projects" / "-workspace").mkdir(parents=True)
        (
            self.legacy / "history" / "claude_projects" / "-workspace" / "s.jsonl"
        ).write_text("{}\n")
        (self.legacy / "sessions").mkdir()
        (self.legacy / "sessions" / "claude-main-1.log").write_text("log\n")
        (self.legacy / "sessions" / "claude-main-1.md").write_text("# t\n")
        (self.legacy / "api-keys.env").write_text("KEY=secret\n")
        (self.legacy / "pi").mkdir()
        (self.legacy / "pi" / "models.json").write_text('{"apiKey": "secret"}\n')
        (self.legacy / "opencode").mkdir()
        (self.legacy / "opencode" / "opencode.json").write_text('{"apiKey": "x"}\n')
        (self.legacy / "Dockerfile").write_text("FROM old\n")
        (self.legacy / ".build-state.json").write_text("{}\n")
        (self.legacy / "workspace-mask").mkdir()
        (self.legacy / ".gitignore").write_text("*\n")

    def _state(self):
        state = paths.open_project_state(self.project)
        self.addCleanup(state.close)
        return state

    def _run(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = state_migration.run(self.project, self._state())
        return result, out.getvalue()

    def test_moves_history_and_logs_and_removes_stale_secrets(self) -> None:
        result, out = self._run()

        self.assertIsNotNone(result)
        state = paths.project_state_path(self.project)
        self.assertEqual((state / "history/shell/bash_history").read_text(), "ls\n")
        self.assertEqual(
            (state / "history/claude_projects/-workspace/s.jsonl").read_text(), "{}\n"
        )
        self.assertEqual((state / "sessions/claude-main-1.log").read_text(), "log\n")
        self.assertTrue((state / "sessions/claude-main-1.md").is_file())
        # Everything was generated, so the legacy directory is gone.
        self.assertFalse(self.legacy.exists())
        self.assertIn("removed stale secrets", out)
        for name in ("api-keys.env", "pi/models.json", "opencode/opencode.json"):
            self.assertIn(name, out)
        marker = json.loads((state / "migrated").read_text())
        self.assertEqual(marker["unknown"], [])

    def test_unknown_files_are_kept_and_named(self) -> None:
        (self.legacy / "notes.txt").write_text("mine\n")
        (self.legacy / "custom").mkdir()

        _result, out = self._run()

        self.assertEqual(
            sorted(p.name for p in self.legacy.iterdir()), ["custom", "notes.txt"]
        )
        self.assertIn("notes.txt", out)
        self.assertIn("custom", out)
        self.assertFalse((self.legacy / "api-keys.env").exists())

    def test_symlinked_history_is_removed_and_its_target_untouched(self) -> None:
        import shutil

        outside = self.root / "outside"
        outside.mkdir()
        (outside / "bash_history").write_text("victim\n")
        shutil.rmtree(self.legacy / "history")
        (self.legacy / "history").symlink_to(outside)
        (self.legacy / "claude").symlink_to(outside)

        self._run()

        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["bash_history"])
        self.assertEqual((outside / "bash_history").read_text(), "victim\n")
        state = paths.project_state_path(self.project)
        self.assertFalse((state / "history" / "shell" / "bash_history").exists())
        self.assertFalse((self.legacy / "history").is_symlink())

    def test_symlinked_file_inside_history_is_not_read(self) -> None:
        outside = self.root / "secret.txt"
        outside.write_text("host secret\n")
        target = self.legacy / "history" / "shell" / "bash_history"
        target.unlink()
        target.symlink_to(outside)

        self._run()

        state = paths.project_state_path(self.project)
        self.assertFalse((state / "history" / "shell" / "bash_history").exists())
        self.assertEqual(outside.read_text(), "host secret\n")

    def test_postponed_while_an_older_session_is_running(self) -> None:
        records = observability.state_dir()
        records.mkdir(parents=True)
        (records / "old.json").write_text(
            json.dumps(
                {
                    "session_id": "old",
                    "project_path": str(self.project),
                    "workspace_path": str(self.project),
                    "status": "running",
                    "pid": os.getpid(),
                }
            )
        )

        result, out = self._run()

        self.assertIsNone(result)
        self.assertIn("Postponing migration", out)
        self.assertIn("old", out)
        self.assertTrue((self.legacy / "api-keys.env").is_file())
        self.assertTrue((self.legacy / "history" / "shell" / "bash_history").is_file())
        self.assertFalse((paths.project_state_path(self.project) / "migrated").exists())

    def test_runs_only_once(self) -> None:
        self._run()
        self.legacy.mkdir()
        (self.legacy / "api-keys.env").write_text("NEW=1\n")

        result, out = self._run()

        self.assertIsNone(result)
        self.assertEqual(out, "")
        self.assertTrue((self.legacy / "api-keys.env").is_file())

    def test_plan_changes_nothing(self) -> None:
        before = sorted(
            str(p.relative_to(self.project)) for p in self.project.rglob("*")
        )

        plan = state_migration.plan(self.project)

        self.assertIn("api-keys.env", plan.secrets)
        self.assertIn("history/shell/bash_history", plan.moves)
        after = sorted(
            str(p.relative_to(self.project)) for p in self.project.rglob("*")
        )
        self.assertEqual(before, after)
        self.assertFalse(paths.state_root_path().exists())

    def test_dry_run_reports_the_plan_and_writes_nothing(self) -> None:
        out = io.StringIO()
        before = sorted(
            str(p.relative_to(self.project)) for p in self.project.rglob("*")
        )
        with (
            patch.object(cli, "read_identity", return_value=GitIdentity("A", "a@b.c")),
            contextlib.redirect_stdout(out),
        ):
            rc = cli.main(
                [
                    "--dry-run",
                    "--no-build",
                    "--agent",
                    "bash",
                    str(self.project),
                    "python:3.12-slim",
                ]
            )
        self.assertEqual(rc, 0)
        self.assertIn(
            "Would migrate: remove stale secrets: api-keys.env", out.getvalue()
        )
        self.assertIn("Would migrate: move 4 history/log file(s)", out.getvalue())
        after = sorted(
            str(p.relative_to(self.project)) for p in self.project.rglob("*")
        )
        self.assertEqual(before, after)
        self.assertFalse(paths.state_root_path().exists())
        self.assertFalse(paths.runtime_root_path().exists())

    def test_launch_migrates_before_staging(self) -> None:
        out = io.StringIO()
        with (
            patch.object(cli, "read_identity", return_value=GitIdentity("A", "a@b.c")),
            patch.object(cli.config_agents, "sync_credentials", return_value={}),
            contextlib.redirect_stdout(out),
        ):
            rc = cli.main([str(self.project), "python:3.12-slim"])
        self.assertEqual(rc, 0)
        self.assertIn("Migrated", out.getvalue())
        self.assertFalse(self.legacy.exists())


class LegacyCredentialStagingTests(TestCase):
    def test_old_per_project_credential_staging_is_removed_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            parent = root / "tmp"
            parent.mkdir()
            with patch.object(state_migration, "LEGACY_CREDENTIALS_PARENT", parent):
                staged = state_migration.legacy_credentials_dir(project)
                (staged / "claude").mkdir(parents=True)
                os.chmod(staged.parent, 0o700)
                (staged / "claude" / ".credentials.json").write_text("{}")
                other = staged.parent / "0123456789abcdef"
                other.mkdir()

                plan = state_migration.plan(project)
                self.assertEqual(plan.legacy_credentials, staged)
                self.assertTrue(staged.exists())  # planning changes nothing

                out = io.StringIO()
                with (
                    paths.open_project_state(project) as state,
                    contextlib.redirect_stdout(out),
                ):
                    state_migration.run(project, state)

                self.assertFalse(staged.exists())
                self.assertTrue(other.is_dir())  # another project's tree stays
                self.assertIn(
                    "Removed credentials staged by an earlier version", out.getvalue()
                )
