import datetime as dt
import os
import shlex
import signal
from pathlib import Path
import subprocess
import threading


def default_log_path(project: Path, branch: str | None, agent: str, *, create: bool = True) -> Path:
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    stem = f"{agent}-{branch.replace('/', '-') if branch else 'main'}-{ts}"
    log_dir = project / ".project-sandbox" / "sessions"
    if create:
        log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"{stem}.log"


def run(argv: list[str], *, log_path: Path, timeout: int | None = None, dry_run: bool = False) -> int:
    if dry_run:
        print(shlex.join(argv), "2>&1 | tee", shlex.quote(str(log_path)))
        if timeout:
            print(f"# timeout: {timeout}s")
        return 0

    with log_path.open("w", encoding="utf-8") as handle:
        # start_new_session puts the child in its own process group so a timeout
        # can signal the whole group (the `container` CLI and anything it spawns),
        # not just the immediate child.
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        assert proc.stdout is not None
        output_thread = threading.Thread(target=_tee_output, args=(proc.stdout, handle), daemon=True)
        output_thread.start()
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate_process_group(proc)
            return 124
        finally:
            output_thread.join(timeout=5)
            proc.stdout.close()


def _terminate_process_group(proc: "subprocess.Popen") -> None:
    """Terminate proc's process group (SIGTERM, then SIGKILL), falling back to
    signaling just the child if the group lookup or kill fails."""
    try:
        pgid: int | None = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        pgid = None

    _signal_group_or_child(proc, pgid, signal.SIGTERM)
    try:
        proc.wait(timeout=30)
        return
    except subprocess.TimeoutExpired:
        pass
    _signal_group_or_child(proc, pgid, signal.SIGKILL)
    proc.wait()


def _signal_group_or_child(proc: "subprocess.Popen", pgid: int | None, sig: int) -> None:
    if pgid is not None:
        try:
            os.killpg(pgid, sig)
            return
        except (ProcessLookupError, OSError):
            pass
    try:
        proc.send_signal(sig)
    except (ProcessLookupError, OSError):
        pass


def _tee_output(stream, handle) -> None:
    for line in stream:
        print(line, end="")
        handle.write(line)
        handle.flush()
