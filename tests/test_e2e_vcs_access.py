"""Check the runtime audit's grading and aggregate-suite wiring without a runtime."""

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from project_sandbox.cli import build_parser

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "e2e_vcs_access", ROOT / "scripts/e2e-vcs-access.py"
)
assert SPEC and SPEC.loader
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


class VcsAccessAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        audit.git(self.project, "init")
        (self.project / "README.md").write_text("fixture\n")
        audit.git(self.project, "add", "README.md")
        audit.git(self.project, "commit", "-m", "initial")
        self.initial = audit.git(self.project, "rev-parse", "HEAD")

    def make_full_commit(self) -> None:
        (self.project / "full-access.txt").write_text("committed in container\n")
        audit.git(self.project, "add", "full-access.txt")
        audit.git(self.project, "commit", "-m", audit.SUBJECT)
        (self.project / "full-commit.txt").write_text(
            audit.git(self.project, "rev-parse", "HEAD")
        )
        (self.project / "full.ok").write_text("ok\n")

    def test_full_requires_persistent_commit_not_just_source_edit(self) -> None:
        (self.project / "full-access.txt").write_text("committed in container\n")
        (self.project / "full.ok").write_text("ok\n")
        with self.assertRaisesRegex(RuntimeError, "did not create a commit"):
            audit.verify_full(self.project, self.initial)
        self.make_full_commit()
        head = audit.verify_full(self.project, self.initial)
        self.assertNotEqual(head, self.initial)
        (self.project / "full-commit.txt").write_text(self.initial)
        with self.assertRaisesRegex(RuntimeError, "did not remain"):
            audit.verify_full(self.project, self.initial)

    def test_read_only_requires_titles_and_unchanged_metadata(self) -> None:
        before = audit.metadata(self.project)
        titles = audit.git(self.project, "log", "--format=%s")
        (self.project / "read-only.ok").write_text("ok\n")
        (self.project / "read-only-edit.txt").write_text("source stays writable\n")
        (self.project / "commit-titles.txt").write_text(titles)
        audit.verify_restricted(self.project, "read-only", self.initial, before, titles)
        (self.project / "commit-titles.txt").write_text("wrong title\n")
        with self.assertRaisesRegex(RuntimeError, "titles did not persist"):
            audit.verify_restricted(
                self.project, "read-only", self.initial, before, titles
            )
        (self.project / "commit-titles.txt").write_text(titles)
        (self.project / ".git/config").write_text("tampered\n")
        # Compare metadata before invoking Git with the now invalid config.
        with patch.object(audit, "git", return_value=self.initial):
            with self.assertRaisesRegex(RuntimeError, "changed host metadata"):
                audit.verify_restricted(
                    self.project, "read-only", self.initial, before, titles
                )

    def test_read_only_rejects_successful_commit(self) -> None:
        before = audit.metadata(self.project)
        titles = audit.git(self.project, "log", "--format=%s")
        (self.project / "read-only.ok").write_text("ok\n")
        self.make_full_commit()
        with self.assertRaisesRegex(RuntimeError, "changed HEAD"):
            audit.verify_restricted(
                self.project, "read-only", self.initial, before, titles
            )

    def test_missing_completion_marker_fails_even_when_head_unchanged(self) -> None:
        with self.assertRaises(FileNotFoundError):
            audit.verify_restricted(
                self.project, "hidden", self.initial, audit.metadata(self.project), ""
            )

    def test_sessions_use_bash_real_runtime_and_reuse_image(self) -> None:
        args = argparse.Namespace(
            runtime="docker", base_image="python:3.12-slim", timeout=120
        )
        for mode in ("full", "read-only", "hidden"):
            with self.subTest(mode=mode), patch.object(audit.subprocess, "run") as run:
                audit.run_session(self.project, self.project, args, "fixture:tag", mode)
                argv = run.call_args.args[0]
                parsed = build_parser().parse_args(argv[3:])
                self.assertEqual(parsed.project, str(self.project))
                self.assertEqual(parsed.base_image, args.base_image)
                self.assertEqual(argv[argv.index("--agent") + 1], "bash")
                self.assertEqual(argv[argv.index("--runtime") + 1], "docker")
                self.assertEqual(argv[argv.index("--vcs-access") + 1], mode)
                self.assertEqual("--no-build" in argv, mode != "full")
                self.assertNotIn("--branch", argv)
                self.assertTrue(run.call_args.kwargs["check"])

    def test_session_error_includes_underlying_log(self) -> None:
        args = argparse.Namespace(
            runtime="docker", base_image="python:3.12-slim", timeout=120
        )

        def fail(argv, **kwargs):
            kwargs["stdout"].write("container build failed: diagnostic from runtime\n")
            raise subprocess.CalledProcessError(2, argv)

        with patch.object(audit.subprocess, "run", side_effect=fail):
            with self.assertRaises(SystemExit) as error:
                audit.run_session(
                    self.project, self.project, args, "fixture:tag", "full"
                )
        self.assertIn("full session exited 2", str(error.exception))
        self.assertIn("diagnostic from runtime", str(error.exception))
        self.assertIn(
            "diagnostic from runtime", (self.project / "full.log").read_text()
        )


class HiddenAuditTests(unittest.TestCase):
    def test_hidden_checks_all_original_files_and_unexpected_contents(self) -> None:
        script = (ROOT / "scripts/fixtures/vcs-access.sh").read_text()
        # Execute the very same Python check used inside the container.
        check = (
            script.split("  hidden)\n", 1)[1]
            .split("<<'PY'\n", 1)[1]
            .split("\nPY\n", 1)[0]
        )
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            for name in (".git", ".jj"):
                (project / name).mkdir()
            paths = [
                ".git/HEAD",
                ".git/vcs-access-canary",
                ".jj/vcs-access-canary",
                ".jj/repo/unusual-secret",
            ]
            (project / "metadata-files.json").write_text(json.dumps(paths))

            def run() -> subprocess.CompletedProcess:
                return subprocess.run(
                    [sys.executable, "-I", "-c", check],
                    cwd=project,
                    capture_output=True,
                    text=True,
                    check=False,
                )

            self.assertEqual(run().returncode, 0)
            leak = project / paths[-1]
            leak.parent.mkdir()
            leak.write_text("secret")
            result = run()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("original metadata visible", result.stderr)
            leak.unlink()
            leak.parent.rmdir()
            (project / ".git/unexpected").write_text("secret")
            self.assertIn("nonempty metadata mask", run().stderr)


class SuitePlanTests(unittest.TestCase):
    def test_suite_runs_by_default_and_requires_containers_and_builds(self) -> None:
        for args, status in (
            (("--runtime", "docker"), "RUN"),
            (("--runtime", "podman", "--only", "vcs-access"), "RUN"),
            (("--runtime", "apple-container", "--only", "vcs-access"), "RUN"),
            (("--runtime", "chroot", "--only", "vcs-access"), "SKIP"),
            (("--runtime", "docker", "--no-build", "--only", "vcs-access"), "SKIP"),
        ):
            with self.subTest(args=args):
                result = subprocess.run(
                    [str(ROOT / "scripts/run-e2e-tests.sh"), "--plan", *args],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertRegex(result.stdout, rf"vcs-access\s+{status}")
