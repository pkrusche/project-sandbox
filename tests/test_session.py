import contextlib
import io
import shlex
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import session


class SessionTests(TestCase):
    def test_timeout_is_enforced_by_python(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "session.log"
            rc = session.run(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                log_path=log_path,
                timeout=1,
            )

            self.assertEqual(rc, 124)
            self.assertTrue(log_path.exists())

    def test_default_log_path_can_be_computed_without_creating_directories(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            path = session.default_log_path(
                project, "agent/demo", "claude", create=False
            )

            self.assertEqual(path.parent, project / ".project-sandbox" / "sessions")
            self.assertFalse(path.parent.exists())

    def test_non_verbose_run_writes_log_without_echoing_to_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "session.log"
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = session.run(
                    [sys.executable, "-c", "print('hello from agent')"],
                    log_path=log_path,
                    verbose=False,
                )

            self.assertEqual(rc, 0)
            self.assertIn("hello from agent", log_path.read_text(encoding="utf-8"))
            self.assertNotIn("hello from agent", out.getvalue())

    def test_env_argument_is_merged_into_child_environment(self) -> None:
        """A value passed via env= must reach the child process even though it
        never appears in argv (so a secret injected this way isn't visible via
        `ps`/process listings)."""
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "session.log"
            argv = [
                sys.executable,
                "-c",
                "import os; print(os.environ['MY_SECRET'])",
            ]
            rc = session.run(
                argv,
                log_path=log_path,
                env={"MY_SECRET": "top-secret-value"},
            )

            self.assertEqual(rc, 0)
            self.assertIn("top-secret-value", log_path.read_text(encoding="utf-8"))
            self.assertNotIn("top-secret-value", " ".join(argv))

    def test_verbose_run_echoes_to_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "session.log"
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                session.run(
                    [sys.executable, "-c", "print('streamed line')"],
                    log_path=log_path,
                    verbose=True,
                )

            self.assertIn("streamed line", out.getvalue())

    def test_run_closes_child_stdin(self) -> None:
        proc = Mock()
        proc.pid = 1234
        proc.wait.return_value = 0
        proc.stdout = io.StringIO("")

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "session.log"
            with patch(
                "project_sandbox.session.subprocess.Popen", return_value=proc
            ) as popen:
                session.run(["cmd"], log_path=log_path)

        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_count_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.log"
            path.write_text("a\nb\nc\n", encoding="utf-8")
            self.assertEqual(session.count_lines(path), 3)
            self.assertEqual(session.count_lines(Path(tmp) / "missing.log"), 0)

    def test_count_lines_counts_trailing_unterminated_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.log"
            # No trailing newline after "c" — still counts as a line.
            path.write_text("a\nb\nc", encoding="utf-8")
            self.assertEqual(session.count_lines(path), 3)

    def test_dry_run_prints_single_space_before_redirect_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "session.log"
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = session.run(
                    ["cmd"],
                    log_path=log_path,
                    dry_run=True,
                )

            self.assertEqual(rc, 0)
            expected = f"cmd > {shlex.quote(str(log_path))}\n"
            self.assertEqual(out.getvalue(), expected)
            self.assertNotIn("  ", out.getvalue())

    def test_run_survives_invalid_utf8_in_agent_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "session.log"
            out = io.StringIO()
            # Write an invalid UTF-8 byte (a lone continuation byte) between
            # two valid markers, then flush stdout as a binary stream.
            script = "import sys\nsys.stdout.buffer.write(b'before-"
            script += "\\x80-after\\n')\nsys.stdout.buffer.flush()\n"
            with contextlib.redirect_stdout(out):
                rc = session.run(
                    [sys.executable, "-c", script],
                    log_path=log_path,
                    verbose=True,
                )

            self.assertEqual(rc, 0)
            logged = log_path.read_text(encoding="utf-8", errors="replace")
            self.assertIn("before-", logged)
            self.assertIn("-after", logged)
            self.assertIn("�", logged)
            self.assertIn("�", out.getvalue())

    def test_timeout_terminates_process_group_then_kills_on_grace_timeout(self) -> None:
        proc = Mock()
        proc.pid = 1234
        proc.wait.side_effect = [subprocess.TimeoutExpired(["cmd"], timeout=30), 0]

        with (
            patch("project_sandbox.session.os.getpgid", return_value=5678),
            patch("project_sandbox.session.os.killpg") as killpg,
        ):
            session._terminate_process_group(proc)

        self.assertEqual(
            killpg.call_args_list,
            [
                ((5678, signal.SIGTERM),),
                ((5678, signal.SIGKILL),),
            ],
        )
        proc.send_signal.assert_not_called()
        self.assertEqual(proc.wait.call_args_list[0].kwargs["timeout"], 30)

    def test_run_terminates_container_on_keyboard_interrupt(self) -> None:
        proc = Mock()
        proc.pid = 1234
        proc.wait.side_effect = KeyboardInterrupt()
        proc.poll.return_value = None
        proc.stdout = io.StringIO("")

        stop_argv = ["docker", "stop", "project-sandbox-abc123"]

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "session.log"
            with (
                patch("project_sandbox.session.subprocess.Popen", return_value=proc),
                patch("project_sandbox.session._terminate_process_group") as terminate,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    session.run(
                        ["cmd"],
                        log_path=log_path,
                        container_stop_argv=stop_argv,
                    )

        terminate.assert_called_once_with(proc, container_stop_argv=stop_argv)

    def test_default_log_path_is_unique_within_the_same_second(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            first = session.default_log_path(
                project, "agent/demo", "claude", create=False
            )
            second = session.default_log_path(
                project, "agent/demo", "claude", create=False
            )

            self.assertNotEqual(first, second)

    def test_terminate_process_group_runs_container_stop_before_signalling(
        self,
    ) -> None:
        proc = Mock()
        proc.pid = 1234
        proc.wait.return_value = 0

        stop_argv = ["docker", "stop", "--time", "5", "project-sandbox-abc123"]
        calls: list[list[str]] = []

        def fake_run(argv, **_kwargs):
            calls.append(argv)
            result = Mock()
            result.returncode = 0
            return result

        with (
            patch("project_sandbox.session.os.getpgid", return_value=5678),
            patch("project_sandbox.session.os.killpg"),
            patch("project_sandbox.session.subprocess.run", side_effect=fake_run),
        ):
            session._terminate_process_group(proc, container_stop_argv=stop_argv)

        self.assertEqual(calls, [stop_argv])
