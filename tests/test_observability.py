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
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_STATE_HOME": tmp}),
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
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_STATE_HOME": tmp}),
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

    def test_timeout_keeps_its_exit_code_despite_429_in_log(self) -> None:
        # A transient, recovered-from 429 in the log must not reclassify a
        # timeout (or a success) as a rate-limit failure.
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "session.log"
            log.write_text("HTTP 429 Too Many Requests; retrying\n")
            self.assertTrue(observability.is_rate_limited_failure(1, log))
            self.assertFalse(
                observability.is_rate_limited_failure(
                    observability.TIMEOUT_EXIT_CODE, log
                )
            )
            self.assertFalse(observability.is_rate_limited_failure(0, log))

    def test_failure_without_log_is_not_rate_limited(self) -> None:
        self.assertFalse(observability.is_rate_limited_failure(1, None))

    def test_pi_json_stream_failure_is_detected(self) -> None:
        # Pi exits 0 in --mode json even when the turn errored out, so the
        # stream itself is the only evidence the run failed.
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "pi.log"
            log.write_text(
                'firewall ready\n{"type": "session", "id": "pi-1"}\n'
                '{"type": "message_end", "message": {"role": "assistant", '
                '"stopReason": "error", "errorMessage": "boom"}}\n',
                encoding="utf-8",
            )
            self.assertTrue(observability.pi_json_stream_failed(log))

    def test_pi_json_stream_success_is_not_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "pi.log"
            log.write_text(
                '{"type": "message_end", "message": {"role": "assistant", '
                '"stopReason": "error"}}\n'
                '{"type": "message_end", "message": {"role": "assistant", '
                '"stopReason": "stop"}}\n',
                encoding="utf-8",
            )
            # Pi retries through errored turns; only the last one decides.
            self.assertFalse(observability.pi_json_stream_failed(log))

    def test_pi_json_stream_without_events_is_not_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "pi.log"
            log.write_text("firewall ready\n", encoding="utf-8")
            self.assertFalse(observability.pi_json_stream_failed(log))
        self.assertFalse(observability.pi_json_stream_failed(None))

    def test_recovered_early_429_does_not_reclassify_a_later_failure(self) -> None:
        # Only the end of the log decides: a 429 the agent retried through and
        # recovered from must not turn an unrelated failure into exit 75, which
        # tells an orchestrator to retry without counting the attempt.
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "session.log"
            filler = "x" * (observability._RATE_LIMIT_TAIL_BYTES + 1024)
            log.write_text(f"HTTP 429 Too Many Requests; retrying\n{filler}\nfailed\n")
            self.assertFalse(observability.log_is_rate_limited(log))
            self.assertFalse(observability.is_rate_limited_failure(1, log))

    def test_terminal_429_in_a_long_log_is_still_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "session.log"
            filler = "x" * (observability._RATE_LIMIT_TAIL_BYTES + 1024)
            log.write_text(f"{filler}\nrate_limit_error\n")
            self.assertTrue(observability.is_rate_limited_failure(1, log))

    def test_pi_json_run_failure_is_detected_despite_exit_zero(self) -> None:
        # `pi --mode json` returns 0 even when the last turn ended in an error,
        # so the log is the only signal that the run actually failed.
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "session.log"
            log.write_text(
                "starting container\n"
                + json.dumps({"type": "session", "id": "pi-1", "cwd": "/workspace"})
                + "\n"
                + json.dumps(
                    {
                        "type": "message_end",
                        "message": {
                            "role": "assistant",
                            "content": [],
                            "stopReason": "error",
                            "errorMessage": "Connection error.",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            self.assertTrue(observability.pi_json_run_failed(log))

    def test_pi_failure_before_a_successful_retry_is_not_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "session.log"
            log.write_text(
                json.dumps(
                    {
                        "type": "message_end",
                        "message": {"role": "assistant", "stopReason": "error"},
                    }
                )
                + "\n"
                + json.dumps({"type": "auto_retry_start", "attempt": 1})
                + "\n"
                + json.dumps(
                    {
                        "type": "message_end",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "done"}],
                            "stopReason": "stop",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            self.assertFalse(observability.pi_json_run_failed(log))

    def test_pi_failure_detection_ignores_other_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "session.log"
            log.write_text("plain agent output\nnot json\n", encoding="utf-8")

            self.assertFalse(observability.pi_json_run_failed(log))
        self.assertFalse(observability.pi_json_run_failed(None))

    def test_interrupted_session_is_not_left_running(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_STATE_HOME": tmp}),
        ):
            path = observability.start_record(
                session_id="ctrl-c",
                container="project-sandbox-ctrl-c",
                project=Path("/project"),
                workspace=Path("/workspace"),
                agent="claude",
                runtime="docker",
            )
            observability.finish_record(path, exit_code=1, status="interrupted")
            record = observability.list_records()[0]

        self.assertEqual(record["status"], "interrupted")
        self.assertIsNotNone(record["ended_at"])

    def test_unwritable_state_dir_degrades_to_no_record(self) -> None:
        # Recording is observability, not part of running the agent: it must
        # never turn a working session into a traceback.
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_STATE_HOME": tmp}),
        ):
            # A file where the state directory should be: mkdir/write both fail.
            (Path(tmp) / "project-sandbox").write_text("", encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                path = observability.start_record(
                    session_id="doomed",
                    container=None,
                    project=Path("/project"),
                    workspace=Path("/workspace"),
                    agent="codex",
                    runtime="docker",
                )
                # The caller closes the record unconditionally; None is a no-op.
                observability.finish_record(path, exit_code=0)

            self.assertIsNone(path)
            self.assertIn("Could not record session doomed", out.getvalue())

    def test_relative_state_home_is_ignored(self) -> None:
        # An invalid XDG_STATE_HOME must not scatter records into the CWD.
        with patch.dict(os.environ, {"XDG_STATE_HOME": "relative/state"}):
            self.assertTrue(observability.state_dir().is_absolute())
        with patch.dict(os.environ, {"XDG_STATE_HOME": ""}):
            self.assertTrue(observability.state_dir().is_absolute())
