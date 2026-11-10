"""Host selection and token resolution for the GitHub Copilot CLI agent.

Copilot authenticates with a GitHub token, not an agent-specific credential
file, so the token is resolved on the host at every launch:

1. ``pass show <entry>`` — preferably a fine-grained PAT with only the
   "Copilot Requests" permission.
2. ``gh auth token --hostname <host>`` — the GitHub CLI's token, which is often
   broadly scoped; callers print a warning whenever this source is used.

The resolved token is staged as a 0600 file outside the repository and mounted
read-only into Copilot containers only (see container_cli.build_mount_specs).
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from . import config_agents, host_tools

DEFAULT_HOST = "github.com"
DEFAULT_PASS_ENTRY = "copilot-github-token"
SECRETS_TARGET = "/project-sandbox-secrets/copilot"
CONFIG_TARGET = "/project-sandbox-config/copilot"
TOKEN_FILENAME = "token"
COPILOT_HOME = "/home/agent/.copilot"

SOURCE_PASS = "pass"
SOURCE_GH = "gh"

# A gpg-agent pinentry may need the user to unlock the key, so `pass` gets a
# generous timeout; `gh auth token` only reads local state.
PASS_TIMEOUT_SECONDS = 120
GH_TIMEOUT_SECONDS = 15

_TENANT_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.ghe\.com$")
_CLASSIC_PAT_PREFIX = "ghp_"

Runner = Callable[..., subprocess.CompletedProcess]


def validate_host(value: str) -> str:
    """Normalize ``--copilot-host`` and reject anything but GitHub.com/GHE.com."""
    host = value.strip().lower().rstrip(".")
    if host == DEFAULT_HOST or _TENANT_RE.fullmatch(host):
        return host
    raise SystemExit(
        f"Unsupported --copilot-host {value!r}: use github.com (personal, "
        "Business, and Enterprise Cloud accounts) or <tenant>.ghe.com (GitHub "
        "Enterprise Cloud with data residency). Self-hosted GitHub Enterprise "
        "Server is not supported."
    )


def is_tenant(host: str) -> bool:
    return host != DEFAULT_HOST


def default_pass_entry(host: str) -> str:
    """Per-host default entry, so a github.com PAT is never sent to a tenant."""
    return DEFAULT_PASS_ENTRY if not is_tenant(host) else f"{DEFAULT_PASS_ENTRY}/{host}"


def tenant_hosts(host: str) -> tuple[str, ...]:
    """Hosts a GHE.com tenant needs beyond the --allow-github set.

    GitHub's allowlist reference lists ``SUBDOMAIN.ghe.com`` and the wildcard
    ``*.SUBDOMAIN.ghe.com``; the firewall pins resolved IPs per name, so these
    are the concrete names Copilot CLI uses (see the change design, Decision 2).
    """
    if not is_tenant(host):
        return ()
    return (
        host,
        f"api.{host}",
        f"copilot-api.{host}",
        f"copilot-telemetry-service.{host}",
    )


def container_env(host: str) -> list[str]:
    """Runtime environment for a Copilot container (no secrets)."""
    env = [f"COPILOT_HOME={COPILOT_HOME}", "COPILOT_AUTO_UPDATE=false"]
    if is_tenant(host):
        env.append(f"COPILOT_GH_HOST={host}")
    return env


def scope_warning(entry: str) -> str:
    return (
        "[W] Copilot is using your GitHub CLI token, which may have repo or "
        "workflow scope, while GitHub is reachable from the sandbox; see "
        "docs/security.md. To narrow it, store a fine-grained PAT with only "
        f'the "Copilot Requests" permission in pass: pass insert {entry}'
    )


def resolve_token(
    host: str,
    explicit_entry: str | None = None,
    *,
    run: Runner | None = None,
) -> tuple[str, str]:
    """Return ``(token, source)`` where source is ``"pass"`` or ``"gh"``.

    An explicit ``--copilot-token-pass`` entry never falls back to ``gh``. The
    default entry falls back quietly when ``pass`` or the entry is missing.
    """
    run = run or host_tools.run
    entry = explicit_entry or default_pass_entry(host)
    token = _pass_token(entry, explicit=explicit_entry is not None, run=run)
    if token is not None:
        return _checked(token, f"pass entry {entry!r}"), SOURCE_PASS
    token = _gh_token(host, run=run)
    if token is not None:
        return _checked(token, f"gh auth token --hostname {host}"), SOURCE_GH
    raise SystemExit(
        f"No GitHub Copilot token found for {host}. Either store a fine-grained "
        'PAT with only the "Copilot Requests" permission in pass '
        f"(pass insert {entry}), or log in with the GitHub CLI "
        f"(gh auth login --hostname {host})."
    )


def _pass_token(entry: str, *, explicit: bool, run: Runner) -> str | None:
    try:
        proc = run(
            ["pass", "show", entry],
            check=False,
            capture_output=True,
            text=True,
            timeout=PASS_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        if explicit:
            raise SystemExit(
                f"--copilot-token-pass {entry}: the pass command is not installed"
            ) from None
        return None
    except subprocess.TimeoutExpired:
        # Not "missing": the store exists but could not be unlocked in time.
        # Do not silently widen the token scope by falling back to gh.
        raise SystemExit(
            f"Timed out after {PASS_TIMEOUT_SECONDS}s reading pass entry {entry!r} "
            "for the Copilot token (is the gpg key unlocked?)"
        ) from None
    token = _first_line(proc.stdout) if proc.returncode == 0 else None
    if token is None and explicit:
        raise SystemExit(
            f"--copilot-token-pass {entry}: pass show returned no token; not "
            "falling back to gh for an explicitly named entry"
        )
    return token


def _gh_token(host: str, *, run: Runner) -> str | None:
    try:
        proc = run(
            ["gh", "auth", "token", "--hostname", host],
            check=False,
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return _first_line(proc.stdout)


def _first_line(output: str | None) -> str | None:
    if not output:
        return None
    lines = output.splitlines()
    token = lines[0].strip() if lines else ""
    return token or None


def _checked(token: str, origin: str) -> str:
    if token.startswith(_CLASSIC_PAT_PREFIX):
        raise SystemExit(
            f"The token from {origin} is a classic personal access token (ghp_), "
            "which GitHub Copilot CLI does not support. Use a fine-grained PAT "
            'with the "Copilot Requests" permission instead.'
        )
    if any(ch.isspace() for ch in token):
        raise SystemExit(f"The token from {origin} contains whitespace")
    return token


def staged_token_dir(project_sandbox_dir: Path) -> Path:
    return config_agents.credentials_dir(project_sandbox_dir, "copilot")


def stage_token(project_sandbox_dir: Path, token: str) -> Path:
    """Write the token to a fresh 0600 file in a 0700 dir outside the repo."""
    directory = staged_token_dir(project_sandbox_dir)
    config_agents.ensure_private_dir(directory)
    path = directory / TOKEN_FILENAME
    path.unlink(missing_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    return directory


def remove_staged_token(project_sandbox_dir: Path) -> None:
    """Best-effort removal at session end; the file is rewritten every launch."""
    try:
        (staged_token_dir(project_sandbox_dir) / TOKEN_FILENAME).unlink(missing_ok=True)
    except OSError:
        pass
