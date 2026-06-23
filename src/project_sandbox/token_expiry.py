"""Read the expiry of staged agent credentials.

The point of this module is to let the host decide how long a sandbox session
may run *before* the in-container agent would need to refresh its OAuth token.
Refresh tokens are single-use and rotate, so a refresh inside an ephemeral
``--rm`` container is lost on exit (and, with parallel containers, races and
invalidates the host login). Bounding the session under the token's own expiry
sidesteps the refresh entirely. The authoritative lifetime is the expiry baked
into the staged credential, never a hardcoded TTL.

Pure parsing only: no network, and token values are never logged.
"""

import base64
import datetime as dt
import json
from pathlib import Path

_HEADLESS_SUFFIX = "-headless"


def _base_agent(agent: str) -> str:
    if agent.endswith(_HEADLESS_SUFFIX):
        return agent[: -len(_HEADLESS_SUFFIX)]
    return agent


def staged_token_expiry(
    credential_dirs: dict[str, Path], agent: str
) -> dt.datetime | None:
    """Return the staged token expiry (aware UTC) for the agent that will run.

    ``bash`` may invoke any agent interactively, so it reports the first staged
    credential with a readable expiry (claude, then codex, then opencode). An
    unreadable/malformed credential, or one with no tracked expiry, yields
    ``None`` (caller treats expiry as unknown and stays silent).
    """
    base = _base_agent(agent)
    if base == "bash":
        candidates = ("claude", "codex", "opencode")
    elif base in ("claude", "codex", "opencode"):
        candidates = (base,)
    else:
        candidates = ()
    for name in candidates:
        cred_dir = credential_dirs.get(name)
        if cred_dir is None:
            continue
        expiry = _READERS[name](cred_dir)
        if expiry is not None:
            return expiry
    return None


def remaining(expiry: dt.datetime, now: dt.datetime | None = None) -> dt.timedelta:
    now = now or dt.datetime.now(tz=dt.timezone.utc)
    return expiry - now


def _claude_expiry(cred_dir: Path) -> dt.datetime | None:
    data = _read_json(cred_dir / ".credentials.json")
    if not isinstance(data, dict):
        return None
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    expires_at_ms = oauth.get("expiresAt")
    if not isinstance(expires_at_ms, (int, float)) or isinstance(expires_at_ms, bool):
        return None
    return _to_datetime(expires_at_ms / 1000)


# Codex stores a JWT access token; its expiry is the `exp` claim.
def _codex_expiry(cred_dir: Path) -> dt.datetime | None:
    data = _read_json(cred_dir / "auth.json")
    if not isinstance(data, dict):
        return None
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        return None
    access = tokens.get("access_token")
    if not isinstance(access, str):
        return None
    exp = _jwt_exp(access)
    if exp is None:
        return None
    return _to_datetime(exp)


# OpenCode is multi-provider: auth.json maps provider -> {type, access, refresh,
# expires(ms)}. Only OAuth providers expire (and rotate single-use refresh tokens,
# so they carry the same host-logout hazard); report the soonest such expiry.
# `expires <= 0` is a sentinel for "no tracked expiry" (e.g. github-copilot, whose
# long-lived token mints short session tokens and is not at risk).
def _opencode_expiry(cred_dir: Path) -> dt.datetime | None:
    data = _read_json(cred_dir / ".local" / "share" / "opencode" / "auth.json")
    if not isinstance(data, dict):
        return None
    soonest: dt.datetime | None = None
    for entry in data.values():
        if not isinstance(entry, dict) or entry.get("type") != "oauth":
            continue
        expires_ms = entry.get("expires")
        if not isinstance(expires_ms, (int, float)) or isinstance(expires_ms, bool):
            continue
        if expires_ms <= 0:
            continue
        expiry = _to_datetime(expires_ms / 1000)
        if expiry is not None and (soonest is None or expiry < soonest):
            soonest = expiry
    return soonest


def _jwt_exp(token: str) -> float | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + padding))
    except (ValueError, json.JSONDecodeError):
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    if isinstance(exp, (int, float)) and not isinstance(exp, bool):
        return float(exp)
    return None


def _to_datetime(epoch_seconds: float) -> dt.datetime | None:
    # A garbage expiry (absurd or out-of-range value) must not crash the launch.
    try:
        return dt.datetime.fromtimestamp(epoch_seconds, tz=dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


_READERS = {
    "claude": _claude_expiry,
    "codex": _codex_expiry,
    "opencode": _opencode_expiry,
}
