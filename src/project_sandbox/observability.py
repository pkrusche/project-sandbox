"""Persistent, machine-readable records for direct sandbox sessions."""

import json
import os
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RATE_LIMIT_EXIT_CODE = 75  # EX_TEMPFAIL: callers should retry after a backoff.
TIMEOUT_EXIT_CODE = 124  # Matches session.run()'s timeout exit code.
# How much of the end of a session log is searched for rate-limit markers.
_RATE_LIMIT_TAIL_BYTES = 32_768


def new_session_id() -> str:
    """Return a sortable session id without exposing project or branch names."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:12]}"


def container_name(session_id: str) -> str:
    """Map a reported session id to the exact runtime container name."""
    return f"project-sandbox-{session_id.lower()}"


def state_dir() -> Path:
    # Per the XDG base directory spec, a relative (or empty) XDG_STATE_HOME is
    # invalid and must be ignored — honouring it would scatter session records
    # into whatever directory the CLI happened to be started from.
    configured = os.environ.get("XDG_STATE_HOME", "")
    base = (
        Path(configured)
        if configured.startswith("/")
        else Path.home() / ".local" / "state"
    )
    return base / "project-sandbox" / "sessions"


def start_record(
    *,
    session_id: str,
    container: str | None,
    project: Path,
    workspace: Path,
    agent: str,
    runtime: str,
) -> Path | None:
    """Open a session record, or return ``None`` if it cannot be written.

    Recording is observability, not part of running the agent: an unwritable
    state directory (read-only or unset ``HOME``, a full disk) must degrade to
    "no record for this session" rather than abort a run whose image is already
    built. Every other entry point in this module is best-effort for the same
    reason.
    """
    record = {
        "session_id": session_id,
        "container_name": container,
        "project_path": str(project),
        "workspace_path": str(workspace),
        "agent": agent,
        "runtime": runtime,
        "status": "running",
        "pid": os.getpid(),
        "started_at": _now(),
        "ended_at": None,
        "exit_code": None,
        "rate_limited": False,
    }
    try:
        # state_dir() itself can fail: Path.home() raises when HOME is unset and
        # the user has no passwd entry.
        path = state_dir() / f"{session_id}.json"
        _write(path, record)
    except (OSError, RuntimeError) as exc:
        print(f"[W] Could not record session {session_id}: {exc}")
        return None
    return path


def finish_record(
    path: Path | None,
    *,
    exit_code: int,
    rate_limited: bool = False,
    status: str | None = None,
) -> None:
    """Close out a record. ``status`` overrides the exit-code-derived default.

    Callers pass an explicit status for sessions that never reached their
    normal end (an interrupt, or an exception on the way out): leaving those
    records as ``running`` would make orphan detection depend on PID liveness
    alone, which a reused PID can defeat indefinitely.
    """
    if path is None:
        return
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    record.update(
        status=status or ("rate_limited" if rate_limited else "completed"),
        ended_at=_now(),
        exit_code=exit_code,
        rate_limited=rate_limited,
    )
    try:
        _write(path, record)
    except OSError as exc:
        print(f"[W] Could not update session record {path}: {exc}")


def list_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        directory = state_dir()
    except RuntimeError:  # no resolvable home; nothing was ever recorded
        return records
    if not directory.exists():
        return records
    for path in sorted(directory.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if record.get("status") == "running" and not _pid_exists(record.get("pid")):
            record["status"] = "orphaned"
        records.append(record)
    return records


def print_sessions(*, as_json: bool) -> None:
    records = list_records()
    if as_json:
        print(json.dumps(records, separators=(",", ":"), sort_keys=True))
        return
    if not records:
        print("No recorded sessions.")
        return
    print("SESSION ID\tSTATUS\tCONTAINER\tWORKSPACE")
    for item in records:
        print(
            f"{item.get('session_id', '-')}\t{item.get('status', '-')}\t"
            f"{item.get('container_name') or '-'}\t{item.get('workspace_path', '-')}"
        )


def is_rate_limited_failure(exit_code: int, log_path: Path | None) -> bool:
    """Classify a finished session as a rate-limit failure.

    Timeouts keep their 124 exit code even when the log mentions a 429: agents
    routinely recover from transient rate limits, and an orchestrator must not
    be told to retry-without-counting a run that is simply too slow.
    """
    if exit_code in (0, TIMEOUT_EXIT_CODE) or log_path is None:
        return False
    return log_is_rate_limited(log_path)


def log_is_rate_limited(path: Path) -> bool:
    """Recognize common provider/agent representations of HTTP 429 failures.

    Only the tail of the log is inspected. Agents retry through transient 429s,
    so a marker from the middle of a long run says nothing about why the
    session ended — and reporting exit 75 for a run that actually failed on,
    say, a test error tells an orchestrator to retry it without counting the
    attempt.
    """
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - _RATE_LIMIT_TAIL_BYTES))
            text = handle.read().decode("utf-8", errors="replace").lower()
    except OSError:
        return False
    markers = (
        "status code: 429",
        'status_code":429',
        'status":429',
        "http 429",
        "too many requests",
        "rate_limit_error",
        "rate limit exceeded",
    )
    return any(marker in text for marker in markers)


def _pid_exists(value: object) -> bool:
    if not isinstance(value, int) or value <= 0:
        return False
    try:
        os.kill(value, 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    return True


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
