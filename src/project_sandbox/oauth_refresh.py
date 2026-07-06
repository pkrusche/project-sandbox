"""Refresh a host agent token before staging, by delegating to the agent's own CLI.

Rather than reimplementing an undocumented OAuth endpoint, we ask the agent's own
command-line tool to validate/refresh its stored login. The tool then persists the
refreshed credential in its own store (``~/.claude/.credentials.json`` or the macOS
Keychain for Claude; ``~/.codex/auth.json`` for Codex) using its maintained,
correct logic — which `sync_credentials` then stages. This avoids the fragile parts
of a hand-rolled refresh (endpoint/client-id drift, Keychain ACLs, partial writes).

Best-effort: any failure (tool missing, network error, timeout) is reported and
swallowed so it can never block a launch. Runs under a per-agent host lock so
concurrent project-sandbox launches do not race on the single-use refresh token.
"""

import contextlib
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

# Per-agent: the host config dir and the command that makes the tool validate and
# (if needed) refresh its own stored token. Claude refreshes lazily, so `auth
# status` is the lightest trigger; Codex's CI/CD guidance is to let it refresh
# auth.json in place when run. Agents without an entry (e.g. opencode) are skipped.
_AGENTS: dict[str, tuple[str, list[str]]] = {
    "claude": (".claude", ["claude", "auth", "status"]),
    "codex": (".codex", ["codex", "login", "status"]),
}
_LOCK_NAME = ".project-sandbox-refresh.lock"
_REFRESH_TIMEOUT_SECONDS = 30
# Substrings (matched case-insensitively) that a CLI's stderr tends to contain when
# invoked with a subcommand it doesn't recognize -- i.e. the pinned version renamed
# or removed the subcommand this module delegates to, as opposed to the ordinary
# "not logged in" case that a real login-status check reports.
_UNRECOGNIZED_COMMAND_MARKERS = (
    "unknown command",
    "no such command",
    "unrecognized command",
    "not a valid command",
    "unrecognized subcommand",
)


def refresh_host_token(
    agent: str, *, home: Path, dry_run: bool = False, verbose: bool = False
) -> None:
    """Ask the agent's own CLI to refresh its stored token in place. Never raises.

    Failures are swallowed on purpose (a refresh problem must never block a
    launch), but when the delegate subcommand itself looks broken or missing --
    as opposed to the ordinary "not logged in" / "nothing to refresh" outcomes --
    a one-time diagnostic is printed when ``verbose`` so the problem doesn't stay
    invisible forever.
    """
    if dry_run:
        return
    entry = _AGENTS.get(agent)
    if entry is None:
        return
    config_dir_name, command = entry
    if shutil.which(command[0]) is None:
        return
    config_dir = home / config_dir_name
    if not config_dir.exists():
        return  # agent not set up on this host; nothing to refresh
    try:
        with _host_lock(config_dir):
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                timeout=_REFRESH_TIMEOUT_SECONDS,
            )
    except FileNotFoundError as exc:
        # shutil.which() found the binary moments ago, but exec failed anyway
        # (removed/broken symlink mid-run) -- the delegate itself is the
        # problem, not the login state, so surface it distinctly from the
        # generic best-effort swallow below.
        if verbose:
            print(
                f"[W] Host {agent} token refresh delegate {command[0]!r} "
                f"disappeared before it could run: {exc}"
            )
        return
    except Exception as exc:  # noqa: BLE001 - best-effort; must not block launch
        print(f"[W] Host {agent} token refresh skipped: {exc}")
        return
    if verbose and result.returncode != 0:
        stderr_text = result.stderr.decode(errors="replace") if result.stderr else ""
        if any(
            marker in stderr_text.lower() for marker in _UNRECOGNIZED_COMMAND_MARKERS
        ):
            print(
                f"[W] Host {agent} token refresh delegate {' '.join(command)!r} was "
                f"not recognized by the installed CLI (exit {result.returncode}); "
                "the pinned version may have renamed or removed this subcommand."
            )


@contextlib.contextmanager
def _host_lock(config_dir: Path) -> Iterator[None]:
    """Serialize host refreshes across concurrent project-sandbox launches."""
    try:
        import fcntl
    except ImportError:  # non-POSIX: best-effort without a lock
        yield
        return
    try:
        handle = (config_dir / _LOCK_NAME).open("w")
    except OSError:
        yield
        return
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()
