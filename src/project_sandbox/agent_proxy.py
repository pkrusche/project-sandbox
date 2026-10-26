"""Validation, authentication, and model discovery for agentgateway."""

from __future__ import annotations

import errno
import json
import os
import socket
import ssl
import subprocess
import urllib.error
import urllib.request

from . import proxy_endpoint
from .local_service_network import HOSTNAME

DEFAULT_KEY_ENV = "AGENTGATEWAY_API_KEY"
REDACTED = "[REDACTED]"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep the gateway bearer key on the configured origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def parse(value: str) -> proxy_endpoint.ProxyEndpoint:
    return proxy_endpoint.parse(value, option="--agent-proxy", api_path=True)


def validate_url(value: str) -> tuple[str, int]:
    endpoint = parse(value)
    return endpoint.path, endpoint.port


def forwarded_url(value: str, *, hostname: str = HOSTNAME) -> str:
    return parse(value).url(sandbox=True, hostname=hostname)


def loopback_host(value: str) -> str | None:
    endpoint = parse(value)
    return endpoint.host if endpoint.local else None


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


def discover_models(
    base_url: str,
    key: str,
    *,
    timeout: float = 10,
    ca_certificates: tuple[bytes, ...] = (),
) -> list[str]:
    request = urllib.request.Request(
        base_url.rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
    )
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(
                context=proxy_endpoint.tls_context(ca_certificates)
            ),
            _NoRedirectHandler(),
        )
        with opener.open(request, timeout=timeout) as response:
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
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        if isinstance(reason, ssl.SSLCertVerificationError):
            raise SystemExit(
                "Agent proxy TLS certificate verification failed"
            ) from None
        if isinstance(reason, socket.gaierror):
            raise SystemExit("Agent proxy DNS resolution failed") from None
        # Name the failure without echoing exception text, which may carry
        # request details such as the bearer key.
        detail = type(reason if isinstance(reason, BaseException) else exc).__name__
        if isinstance(reason, OSError) and reason.errno in errno.errorcode:
            detail += f" {errno.errorcode[reason.errno]}"
        raise SystemExit(
            f"Agent proxy is unavailable ({detail}); check gateway connectivity "
            "and TLS certificate trust/name"
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
