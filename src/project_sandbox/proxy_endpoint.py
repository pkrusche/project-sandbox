"""Credential-free endpoint interpretation shared by both proxy modes."""

from __future__ import annotations

import ipaddress
import re
import ssl
from dataclasses import dataclass
from typing import NoReturn
from urllib.parse import urlsplit, urlunsplit

from .local_service_network import HOSTNAME

_HOSTNAME = re.compile(
    r"(?=.{1,253}\.?$)(?!-)[a-zA-Z0-9-]{1,63}(?<!-)"
    r"(?:\.(?!-)[a-zA-Z0-9-]{1,63}(?<!-))*\.?"
)


@dataclass(frozen=True)
class ProxyEndpoint:
    scheme: str
    host: str
    port: int
    path: str
    local: bool

    @property
    def sandbox_host(self) -> str:
        return HOSTNAME if self.local else self.host

    def url(self, *, sandbox: bool = False, hostname: str = HOSTNAME) -> str:
        host = hostname if sandbox and self.local else self.host
        authority = f"[{host}]" if ":" in host else host
        return urlunsplit((self.scheme, f"{authority}:{self.port}", self.path, "", ""))

    @property
    def sandbox_url(self) -> str:
        return self.url(sandbox=True)


def parse(
    value: str,
    *,
    option: str,
    api_path: bool = False,
    allow_https_loopback: bool = False,
    allow_https_ip: bool = False,
) -> ProxyEndpoint:
    # urlsplit silently strips controls; reject them before parsing. Never echo
    # the supplied URL or parser exception, since either can contain userinfo.
    def reject(reason: str) -> NoReturn:
        raise SystemExit(f"{option}: {reason}")

    if any(ord(c) <= 32 or ord(c) == 127 for c in value):
        reject("URL must not contain whitespace or control characters")
    if "?" in value or "#" in value:
        reject("URL must not contain a query or fragment")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        reject("invalid URL authority or port")
    if parsed.scheme not in ("http", "https"):
        reject("URL must use http:// or https://")
    if "@" in parsed.netloc:
        reject("URL must not contain credentials")
    if not host or "%" in host or "\\" in parsed.netloc:
        reject("invalid hostname or scoped address")
    assert host is not None
    if parsed.netloc.endswith(":") or (port is not None and not 1 <= port <= 65535):
        reject("invalid port")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if parsed.netloc.startswith("[") or not _HOSTNAME.fullmatch(host):
            reject("invalid hostname")
        # Reject legacy numeric forms that resolvers can interpret as loopback
        # (127.1, decimal integers, octal or hexadecimal IPv4).
        if all(
            re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)", label)
            for label in host.rstrip(".").split(".")
        ):
            reject("use a canonical IP address")
        local = host.rstrip(".").lower() == "localhost"
        literal = False
    else:
        literal = True
        effective = getattr(address, "ipv4_mapped", None) or address
        if effective.is_unspecified or effective.is_multicast:
            reject("wildcard and multicast addresses are not allowed")
        local = effective.is_loopback
        if ":" in host and not parsed.netloc.startswith("["):
            reject("IPv6 addresses must be bracketed")
        # urlsplit accepts junk following a closing IPv6 bracket.
        if parsed.netloc.startswith("["):
            suffix = parsed.netloc.split("]", 1)[1]
            if suffix and not re.fullmatch(r":[0-9]+", suffix):
                reject("invalid bracketed authority")
    if local and parsed.scheme == "https" and not allow_https_loopback:
        reject("HTTPS loopback endpoints are unsupported; use HTTP local forwarding")
    # Node.js refuses IP addresses as TLS server names, so Node-based agents
    # cannot reach an HTTPS endpoint addressed by IP.
    if literal and parsed.scheme == "https" and not allow_https_ip:
        reject("HTTPS endpoints must use a DNS hostname, not an IP address")
    if not api_path and parsed.path not in ("", "/"):
        reject("URL must not contain a path other than /")
    return ProxyEndpoint(
        parsed.scheme,
        host,
        port or (443 if parsed.scheme == "https" else 80),
        (parsed.path.rstrip("/") if local else parsed.path) if api_path else "",
        local,
    )


def tls_context(ca_certificates: tuple[bytes, ...] = ()) -> ssl.SSLContext:
    """Extend system trust for this connection without modifying host trust."""
    context = ssl.create_default_context()
    for certificate in ca_certificates:
        context.load_verify_locations(cadata=certificate.decode("ascii"))
    return context
