import contextlib
import io
import json
import os
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from project_sandbox import observability


class ObservabilityTests(TestCase):
    def test_container_name_is_derived_from_session_id(self) -> None:
        self.assertEqual(
            observability.container_name("20260813T120000-AbC123"),
            "project-sandbox-20260813t120000-abc123",
        )

    def test_records_complete_session_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"XDG_STATE_HOME": tmp}
        ):
            path = observability.start_record(
                session_id="session-1",
                container="project-sandbox-session-1",
                project=Path("/project"),
                workspace=Path("/workspace"),
                agent="codex",
                runtime="docker",
            )
            observability.finish_record(path, exit_code=0)

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                observability.print_sessions(as_json=True)
            records = json.loads(out.getvalue())

        self.assertEqual(records[0]["status"], "completed")
        self.assertEqual(records[0]["exit_code"], 0)
        self.assertEqual(records[0]["container_name"], "project-sandbox-session-1")

    def test_dead_running_record_is_listed_as_orphaned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"XDG_STATE_HOME": tmp}
        ):
            path = observability.start_record(
                session_id="lost",
                container="project-sandbox-lost",
                project=Path("/project"),
                workspace=Path("/workspace"),
                agent="claude",
                runtime="apple-container",
            )
            record = json.loads(path.read_text())
            record["pid"] = 999_999_999
            path.write_text(json.dumps(record))
            self.assertEqual(observability.list_records()[0]["status"], "orphaned")

    def test_rate_limit_detection_covers_structured_429(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "session.log"
            log.write_text('{"error":{"status":429}}\n')
            self.assertTrue(observability.log_is_rate_limited(log))

    def test_unrelated_failure_is_not_rate_limited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "session.log"
            log.write_text("authentication failed\n")
            self.assertFalse(observability.log_is_rate_limited(log))
