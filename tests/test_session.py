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
