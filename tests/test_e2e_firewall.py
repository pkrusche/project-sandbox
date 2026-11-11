"""Validate firewall e2e grading without requiring sudo or a container runtime."""

import argparse
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from project_sandbox.cli import build_parser

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit = load("firewall_audit", "scripts/fixtures/firewall.py")
runner = load("e2e_firewall", "scripts/e2e-firewall.py")


def result(code: int, output: str = "sudo: a password is required\n"):
    return subprocess.CompletedProcess([], code, stdout="", stderr=output)


class FirewallAuditTests(unittest.TestCase):
    def test_sudo_denial_is_recorded_and_noninteractive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(audit.subprocess, "run", return_value=result(1)) as run:
                audit.check_sudo(Path(tmp))
                self.assertEqual(
                    run.call_args_list[0].args[0], ["sudo", "-n", "-l", audit.SCRIPT]
                )
                self.assertEqual(
                    run.call_args_list[1].args[0], ["sudo", "-n", audit.SCRIPT]
                )
                self.assertIn(
                    "password is required", (Path(tmp) / "sudo-attempt.log").read_text()
                )

    def test_rejects_permission_retained_successful_execution_and_guard_only_denial(
        self,
    ) -> None:
        for responses, message in (
            ([result(0)], "permission remains"),
            ([result(1), result(0, "Firewall initialized.")], "permission remains"),
            (
                [
                    result(1),
                    result(1, "ERROR: Firewall initialization already attempted"),
                ],
                "only its reinitialization guard",
            ),
            (
                [result(1), result(1, "sudo: unable to execute: No such file")],
                "unexpected reason",
            ),
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as tmp:
                with patch.object(audit.subprocess, "run", side_effect=responses):
                    with self.assertRaisesRegex(RuntimeError, message):
                        audit.check_sudo(Path(tmp))

    def test_initialization_requires_revocation_not_an_unreadable_sudoers_directory(
        self,
    ) -> None:
        with patch.object(audit.os, "geteuid", return_value=1000):
            with patch.object(
                Path, "stat", return_value=SimpleNamespace(st_uid=0, st_mode=0o700)
            ):
                with patch.object(Path, "lstat", side_effect=FileNotFoundError):
                    audit.check_initialization()
                with patch.object(Path, "lstat", return_value=object()):
                    with self.assertRaisesRegex(RuntimeError, "entry survived"):
                        audit.check_initialization()
                with patch.object(Path, "lstat", side_effect=PermissionError):
                    with self.assertRaises(PermissionError):
                        audit.check_initialization()

    def test_root_or_missing_initialization_cannot_pass(self) -> None:
        with patch.object(audit.os, "geteuid", return_value=0):
            with self.assertRaisesRegex(RuntimeError, "unprivileged"):
                audit.check_initialization()
        with patch.object(audit.os, "geteuid", return_value=1000):
            with patch.object(Path, "stat", side_effect=FileNotFoundError):
                with self.assertRaises(FileNotFoundError):
                    audit.check_initialization()


class FirewallRunnerTests(unittest.TestCase):
    def test_fresh_run_cannot_reuse_previous_success_marker(self) -> None:
        args = argparse.Namespace(runtime="docker", base_image="python:3.12-slim")
        for attempt in (1, 2):
            with self.subTest(attempt=attempt), tempfile.TemporaryDirectory() as tmp:
                project = Path(tmp)
                # A leftover marker from any session must not count as success.
                for name in ("firewall.ok", "firewall-1.ok", "firewall-2.ok"):
                    (project / name).write_text("initialized; sudo revoked\n")
                with patch.object(runner.subprocess, "run") as run:
                    with self.assertRaises(FileNotFoundError):
                        runner.run_session(
                            project, project, args, "fixture:tag", attempt
                        )
                    argv = run.call_args.args[0]
                    parsed = build_parser().parse_args(argv[3:])
                    self.assertEqual(parsed.project, str(project))
                    self.assertEqual(parsed.base_image, args.base_image)
                    self.assertEqual(argv[argv.index("--agent") + 1], "bash")
                    self.assertEqual(argv[argv.index("--egress") + 1], "firewall")
                    self.assertNotIn("--no-firewall", argv)
                    self.assertEqual("--no-build" in argv, attempt == 2)
                    # Each session writes its own marker, so no file the host
                    # deleted is re-created (Podman machine shared-folder cache).
                    self.assertEqual(
                        argv[argv.index("--prompt-text") + 1],
                        f"python3 -I /workspace/firewall-audit.py firewall-{attempt}.ok",
                    )

    def test_session_error_includes_underlying_log(self) -> None:
        args = argparse.Namespace(runtime="docker", base_image="python:3.12-slim")

        def fail(argv, **kwargs):
            kwargs["stdout"].write("firewall startup failed: diagnostic from runtime\n")
            raise subprocess.CalledProcessError(1, argv)

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            with patch.object(runner.subprocess, "run", side_effect=fail):
                with self.assertRaises(SystemExit) as error:
                    runner.run_session(project, project, args, "fixture:tag", 1)
            self.assertIn("session 1 exited 1", str(error.exception))
            self.assertIn("diagnostic from runtime", str(error.exception))
            self.assertIn(
                "diagnostic from runtime", (project / "session-1.log").read_text()
            )

    def test_suite_runs_by_default_and_requires_containers_and_builds(self) -> None:
        for args, status in (
            (("--runtime", "docker"), "RUN"),
            (("--runtime", "podman", "--only", "firewall"), "RUN"),
            (("--runtime", "apple-container", "--only", "firewall"), "RUN"),
            (("--runtime", "chroot", "--only", "firewall"), "SKIP"),
            (("--runtime", "docker", "--no-build", "--only", "firewall"), "SKIP"),
        ):
            with self.subTest(args=args):
                proc = subprocess.run(
                    [str(ROOT / "scripts/run-e2e-tests.sh"), "--plan", *args],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertRegex(proc.stdout, rf"firewall\s+{status}")
