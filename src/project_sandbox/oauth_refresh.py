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


def refresh_host_token(agent: str, *, home: Path, dry_run: bool = False) -> None:
    """Ask the agent's own CLI to refresh its stored token in place. Never raises."""
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
            subprocess.run(
                command,
                check=False,
                capture_output=True,
                timeout=_REFRESH_TIMEOUT_SECONDS,
            )
    except Exception as exc:  # noqa: BLE001 - best-effort; must not block launch
        print(f"[W] Host {agent} token refresh skipped: {exc}")


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
