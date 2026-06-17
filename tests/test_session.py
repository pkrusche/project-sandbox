import contextlib
import io
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

    def test_default_log_path_can_be_computed_without_creating_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            path = session.default_log_path(project, "agent/demo", "claude", create=False)

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

    def test_count_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.log"
            path.write_text("a\nb\nc\n", encoding="utf-8")
            self.assertEqual(session.count_lines(path), 3)
            self.assertEqual(session.count_lines(Path(tmp) / "missing.log"), 0)

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

    def test_terminate_process_group_runs_container_stop_before_signalling(self) -> None:
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
