"""Validation, authentication, and model discovery for agentgateway."""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit

HOSTNAME = "agent-proxy.project-sandbox.internal"
DEFAULT_KEY_ENV = "AGENTGATEWAY_API_KEY"
REDACTED = "[REDACTED]"


def validate_url(value: str) -> tuple[str, int]:
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in (
        "127.0.0.1",
        "localhost",
        "::1",
    ):
        raise SystemExit("--agent-proxy must be an HTTP loopback URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise SystemExit("--agent-proxy must contain a valid explicit port") from exc
    if port is None:
        raise SystemExit("--agent-proxy must contain an explicit port")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SystemExit(
            "--agent-proxy must not contain credentials, query, or fragment"
        )
    return parsed.path.rstrip("/") or "", port


def forwarded_url(value: str) -> str:
    path, port = validate_url(value)
    return urlunsplit(("http", f"{HOSTNAME}:{port}", path, "", ""))


def resolve_key(env_name: str, raw_key: str | None) -> tuple[str, str]:
    try:
        proc = subprocess.run(
            ["pass", "show", "agentgateway-api-key"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        key = (
            proc.stdout.splitlines()[0].strip()
            if proc.returncode == 0 and proc.stdout
            else ""
        )
    except (OSError, subprocess.TimeoutExpired):
        key = ""
    if key:
        return key, "pass"
    key = os.environ.get(env_name, "").strip()
    if key:
        return key, "environment"
    if raw_key and raw_key.strip():
        return raw_key.strip(), "command line"
    raise SystemExit(
        "No gateway key found in pass, the selected environment variable, or --agent-proxy-key"
    )


def discover_models(base_url: str, key: str, *, timeout: float = 10) -> list[str]:
    request = urllib.request.Request(
        base_url.rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise SystemExit(
                "Agent proxy rejected the gateway key (HTTP 401/403)"
            ) from None
        raise SystemExit(
            f"Agent proxy model discovery failed (HTTP {exc.code})"
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SystemExit(
            "Agent proxy is unavailable; start or troubleshoot agentgateway-locally"
        ) from exc
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise SystemExit("Agent proxy returned a malformed model catalog") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not data:
        raise SystemExit("Agent proxy returned an empty or malformed model catalog")
    models: list[str] = []
    for item in data:
        model = item.get("id") if isinstance(item, dict) else None
        if not isinstance(model, str) or not model.strip():
            raise SystemExit("Agent proxy returned an empty or malformed model ID")
        if model not in models:
            models.append(model)
    return models
