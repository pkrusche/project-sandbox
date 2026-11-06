"""Strict, implementation-neutral Internet proxy configuration."""

from __future__ import annotations

import ipaddress
import socket
import ssl
from dataclasses import dataclass

from . import build_proxy_forwarder, proxy_endpoint
from .container_cli import APPLE_CONTAINER, Runtime
from .local_service_network import HOSTNAME, LocalService

NO_PROXY_BASE = ("localhost", "127.0.0.1", "::1")


@dataclass(frozen=True)
class InternetProxy:
    original_url: str
    endpoint: proxy_endpoint.ProxyEndpoint

    @property
    def host(self) -> str:
        return self.endpoint.host

    @property
    def port(self) -> int:
        return self.endpoint.port

    @property
    def forwarded_url(self) -> str:
        return self.endpoint.sandbox_url

    @property
    def service(self) -> LocalService:
        if not self.endpoint.local:
            raise ValueError("Remote endpoints do not use local forwarding")
        return LocalService("Internet proxy", self.port, loopback_host=self.host)


def parse(value: str) -> InternetProxy:
    return InternetProxy(value, proxy_endpoint.parse(value, option="--internet-proxy"))


# Separates the runtime relay's port from the build forwarder for the same URL.
RELAY_PURPOSE = "internet-proxy:"


def relay_port(config: InternetProxy) -> int:
    return build_proxy_forwarder.preferred_port(config.endpoint, purpose=RELAY_PURPOSE)


def relay(
    config: InternetProxy,
    *,
    sidecar: bool = False,
    ca_certificates: tuple[bytes, ...] = (),
) -> build_proxy_forwarder.BuildProxyForwarder:
    """Bind the host-loopback relay to a remote proxy (Apple ``container``).

    Firewall mode binds the fixed port derived from the proxy URL, because the
    image's firewall admits only that port; a busy port is an error. In sidecar
    mode nothing baked into an image depends on the port, so each session
    binds any free loopback port and concurrent sessions using the same proxy
    no longer collide (design.md Decision 7).
    """
    if sidecar:
        return build_proxy_forwarder.BuildProxyForwarder(
            config.endpoint,
            ca_certificates=ca_certificates,
            port=0,
            fallback=True,
            label="Internet proxy",
            ca_option="--ca-cert",
        )
    return build_proxy_forwarder.BuildProxyForwarder(
        config.endpoint,
        ca_certificates=ca_certificates,
        port=relay_port(config),
        fallback=False,
        label="Internet proxy",
        ca_option="--ca-cert",
    )


def for_runtime(
    config: InternetProxy | None, runtime: Runtime | None
) -> InternetProxy | None:
    """Return the endpoint the sandbox connects to on ``runtime``.

    Apple container cannot route to VPN-only hosts, only to the host's loopback,
    so a remote proxy is reached through a host-loopback relay there. The relay
    speaks plain HTTP to the sandbox and re-originates TLS for an ``https://``
    proxy on the host. Its port is derived from the proxy URL because the
    firewall, which is baked into the image, admits only that port.
    """
    if config is None or runtime != APPLE_CONTAINER or config.endpoint.local:
        return config
    endpoint = proxy_endpoint.ProxyEndpoint(
        "http", "127.0.0.1", relay_port(config), "", True
    )
    return InternetProxy(config.original_url, endpoint)


# --egress sidecar: the sandbox's proxy is its sidecar, named in /etc/hosts.
SIDECAR_PROXY_URL = "http://egress-proxy:3128"


def environment(
    config: InternetProxy,
    *,
    bypass_local_services: bool,
    bypass_hosts: tuple[str, ...] = (),
) -> dict[str, str]:
    return _proxy_environment(
        config.forwarded_url,
        bypass_local_services=bypass_local_services,
        bypass_hosts=bypass_hosts,
    )


def sidecar_environment(
    *, bypass_local_services: bool, bypass_hosts: tuple[str, ...] = ()
) -> dict[str, str]:
    """Proxy variables for a sidecar session (design.md Decision 1).

    ``host.docker.internal`` keeps its NO_PROXY entry: in sidecar mode it
    resolves to the sidecar's local-service forwarders, not through Squid.
    """
    return _proxy_environment(
        SIDECAR_PROXY_URL,
        bypass_local_services=bypass_local_services,
        bypass_hosts=bypass_hosts,
    )


def _proxy_environment(
    proxy: str,
    *,
    bypass_local_services: bool,
    bypass_hosts: tuple[str, ...] = (),
) -> dict[str, str]:
    values = {
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
        "http_proxy": proxy,
        "https_proxy": proxy,
    }
    bypass = list(NO_PROXY_BASE)
    if bypass_local_services:
        bypass.append(HOSTNAME)
    bypass.extend(bypass_hosts)
    # curl matches bare IPv6 literals, while urllib matches bracketed URL
    # authorities. Include both so gateway requests stay direct in either client.
    for host in tuple(bypass):
        if ":" in host:
            address = ipaddress.IPv6Address(host)
            canonical = str(address)
            if address.ipv4_mapped is not None:
                # Python versions differ on whether mapped addresses retain a
                # dotted IPv4 suffix; WHATWG URLs always use hexadecimal groups.
                mapped = int(address.ipv4_mapped)
                canonical = f"::ffff:{mapped >> 16:x}:{mapped & 0xFFFF:x}"
            # WHATWG URL clients (including Node fetch) canonicalize IPv6.
            bypass.extend((canonical, f"[{host}]", f"[{canonical}]"))
    no_proxy = ",".join(dict.fromkeys(bypass))
    values.update({"NO_PROXY": no_proxy, "no_proxy": no_proxy})
    return values


def merge_environment(
    values: dict[str, str],
    config: InternetProxy,
    *,
    bypass_local_services: bool,
    bypass_hosts: tuple[str, ...] = (),
) -> dict[str, str]:
    """Add policy-owned proxy variables without allowing injected overrides."""
    return {
        **values,
        **environment(
            config,
            bypass_local_services=bypass_local_services,
            bypass_hosts=bypass_hosts,
        ),
    }


def preflight(
    config: InternetProxy,
    *,
    timeout: float = 2.0,
    ca_certificates: tuple[bytes, ...] = (),
) -> None:
    """Check TCP or verified TLS without contacting any proxy destination."""
    try:
        with socket.create_connection(
            (config.host, config.port), timeout=timeout
        ) as connection:
            if config.endpoint.scheme == "https":
                with proxy_endpoint.tls_context(ca_certificates).wrap_socket(
                    connection, server_hostname=config.host
                ):
                    pass
    except ssl.SSLCertVerificationError as exc:
        raise SystemExit("Internet proxy TLS certificate verification failed") from exc
    except socket.gaierror as exc:
        raise SystemExit("Internet proxy DNS resolution failed") from exc
    except OSError as exc:
        raise SystemExit(
            f"Internet proxy listener {config.original_url} is unavailable; "
            "start or troubleshoot the configured proxy before retrying."
        ) from exc
