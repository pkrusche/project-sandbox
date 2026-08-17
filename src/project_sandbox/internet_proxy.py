"""Strict, implementation-neutral Internet proxy configuration."""

from __future__ import annotations

import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

from .local_service_network import HOSTNAME, LocalService

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
NO_PROXY_BASE = ("localhost", "127.0.0.1", "::1")


@dataclass(frozen=True)
class InternetProxy:
    original_url: str
    host: str
    port: int

    @property
    def forwarded_url(self) -> str:
        return f"http://{HOSTNAME}:{self.port}"

    @property
    def service(self) -> LocalService:
        return LocalService("Internet proxy", self.port, loopback_host=self.host)


def parse(value: str) -> InternetProxy:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise SystemExit(f"Invalid --internet-proxy URL: {exc}") from exc
    if parsed.scheme != "http":
        raise SystemExit("--internet-proxy must use http://")
    if parsed.hostname not in LOOPBACK_HOSTS:
        raise SystemExit(
            "--internet-proxy must use loopback host 127.0.0.1, localhost, or ::1"
        )
    if parsed.username is not None or parsed.password is not None:
        raise SystemExit("--internet-proxy URL must not contain credentials")
    if port is None:
        raise SystemExit("--internet-proxy requires an explicit valid port")
    if parsed.path not in ("", "/"):
        raise SystemExit("--internet-proxy URL must not contain a path")
    if parsed.query or parsed.fragment:
        raise SystemExit("--internet-proxy URL must not contain a query or fragment")
    return InternetProxy(value, parsed.hostname, port)


def environment(config: InternetProxy, *, bypass_local_services: bool) -> dict[str, str]:
    proxy = config.forwarded_url
    values = {
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
        "http_proxy": proxy,
        "https_proxy": proxy,
    }
    bypass = list(NO_PROXY_BASE)
    if bypass_local_services:
        bypass.append(HOSTNAME)
    no_proxy = ",".join(bypass)
    values.update({"NO_PROXY": no_proxy, "no_proxy": no_proxy})
    return values


def preflight(config: InternetProxy, *, timeout: float = 2.0) -> None:
    """Prove only that the configured loopback listener accepts TCP."""
    try:
        with socket.create_connection((config.host, config.port), timeout=timeout):
            pass
    except OSError as exc:
        raise SystemExit(
            f"Internet proxy listener {config.original_url} is unavailable; "
            "start or troubleshoot internet-proxy-locally before retrying."
        ) from exc
