import contextlib
import io
import sys
import tempfile
from pathlib import Path
from unittest import TestCase


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
