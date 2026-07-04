import datetime as dt
import os
import shlex
import signal
from pathlib import Path
import subprocess
import threading


def merged_env(env: dict[str, str] | None) -> dict[str, str] | None:
    """Return a subprocess env with ``env`` layered on top of the current
    process environment, or None (inherit as-is) when there is nothing to add.

    Lets a caller hand a child process a secret value without it ever
    appearing in argv (where it would be visible via `ps`/process listings):
    pass a bare name in argv and supply the value here, in the child's
    environment.
    """
    if not env:
        return None
    return {**os.environ, **env}


def default_log_path(project: Path, branch: str | None, agent: str, *, create: bool = True) -> Path:
    now = dt.datetime.now()
    # Include microseconds so two same-agent sessions started within the same
    # second do not resolve to the same log file (which run() opens with "w").
    ts = now.strftime("%Y%m%d-%H%M%S-%f")
    stem = f"{agent}-{branch.replace('/', '-') if branch else 'main'}-{ts}"
    log_dir = project / ".project-sandbox" / "sessions"
    if create:
        log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"{stem}.log"


def run(
    argv: list[str],
    *,
    log_path: Path,
    timeout: int | None = None,
    container_stop_argv: list[str] | None = None,
    dry_run: bool = False,
    verbose: bool = False,
    env: dict[str, str] | None = None,
) -> int:
    if dry_run:
        redirect = "2>&1 | tee" if verbose else ">"
        print(shlex.join(argv), redirect, shlex.quote(str(log_path)))
        if timeout:
            print(f"# timeout: {timeout}s")
        return 0

    with log_path.open("w", encoding="utf-8") as handle:
        # start_new_session puts the child in its own process group so a timeout
        # can signal the whole group (the `container` CLI and anything it spawns),
        # not just the immediate child.
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            start_new_session=True,
            # Merge in any extra values (e.g. injected API keys) rather than
            # baking them into argv; see merged_env.
            env=merged_env(env),
        )
        assert proc.stdout is not None
        output_thread = threading.Thread(
            target=_tee_output, args=(proc.stdout, handle, verbose), daemon=True
        )
        output_thread.start()
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate_process_group(proc, container_stop_argv=container_stop_argv)
            return 124
        except BaseException:
            # A KeyboardInterrupt (or any other parent-side exception) must not
            # leave the named headless container running. Tear down the process
            # group before re-raising.
            if proc.poll() is None:
                _terminate_process_group(proc, container_stop_argv=container_stop_argv)
            raise
        finally:
            output_thread.join(timeout=5)
            proc.stdout.close()


def _terminate_process_group(
    proc: "subprocess.Popen",
    *,
    container_stop_argv: list[str] | None = None,
) -> None:
    """Terminate proc's process group (SIGTERM, then SIGKILL), falling back to
    signaling just the child if the group lookup or kill fails.

    When container_stop_argv is given, it is run first to ask the container
    runtime to stop the named container directly. This is the reliable path:
    the runtime sends SIGTERM to the container's PID 1 and, after a short
    grace period, force-kills it — causing the 'container run' / 'docker run'
    CLI to exit on its own, so the subsequent SIGTERM to the process group
    is usually a no-op and the 30 s SIGKILL grace is never reached.
    """
    if container_stop_argv:
        try:
            subprocess.run(
                container_stop_argv,
                timeout=15,
                check=False,
                capture_output=True,
            )
        except Exception:  # noqa: BLE001 — best-effort; fall through to signal path
            pass

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


def _tee_output(stream, handle, verbose: bool = True) -> None:
    for line in stream:
        if verbose:
            print(line, end="")
        handle.write(line)
        handle.flush()


def count_lines(path: Path) -> int:
    """Count lines in a file without loading it all into memory.

    A trailing unterminated line (no final newline) is still counted, matching
    the behavior of iterating over the file's lines.
    """
    try:
        with path.open("rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0
