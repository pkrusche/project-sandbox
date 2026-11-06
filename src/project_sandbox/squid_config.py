"""Render the egress sidecar's ``squid.conf`` (design.md Decisions 2, 5, 7).

Pipeline for a CONNECT request:

1. ``http_access``: port, IP-literal, allowlist and (direct mode only)
   private/metadata-address checks. Denials are ``TCP_RESET``: Squid would
   otherwise answer ``200`` and bump to serve the error (spike 0.2). The cause
   is logged through a ``note sbx_cause`` annotation.
2. ``ssl_bump`` step 1 peeks at the ClientHello. Step 2 terminates unless the
   SNI is present and equals the CONNECT host (``sni-match`` helper), bumps
   the intercepted hosts (``api.github.com`` for Copilot's token exchange) and
   splices everything else.
3. Non-TLS bytes fail the peek and are not tunnelled
   (``on_unsupported_protocol respond``).

Plain-HTTP and intercepted requests that are denied get a labelled 403 page
(``project-sandbox-egress: <cause>``).

With a parent proxy, every request goes to the ``cache_peer`` (``never_direct``)
and the DNS-based private-address checks are omitted, because corporate networks
often resolve external names only at the proxy.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from dataclasses import dataclass

from . import egress_policy, local_service_network
from .templating import get_template

LISTEN_PORT = egress_policy.SIDECAR_PROXY_PORT
# Read-only host input mount (egress/<runtime>/sidecar/ on the host).
INPUT_DIR = "/project-sandbox-egress"
# Paths inside the sidecar (staged by the entrypoint into tmpfs).
STATE_DIR = "/run/egress-sidecar"
PID_FILE = f"{STATE_DIR}/squid.pid"
CA_CERT = f"{STATE_DIR}/ca.pem"
CA_KEY = f"{STATE_DIR}/ca-key.pem"
SSL_DB = f"{STATE_DIR}/ssl_db"
UPSTREAM_CA = f"{STATE_DIR}/upstream-ca.pem"
ERROR_DIRECTORY = "/etc/squid/sbx-errors"
SNI_HELPER = "/usr/local/libexec/sni-match"
# Every access-log line starts with this token, so the denial count can
# separate it from cache.log lines on the same stream. Lines end with the
# client IP (``client=%>a``), the sandbox that made the request.
LOG_PREFIX = "SBX"


@dataclass(frozen=True)
class Cause:
    label: str  # logged as sbx_cause and shown in pages
    page: str | None  # error page for plain-HTTP/intercepted denials
    summary: str  # one line for the denial page and the session summary


# Labelled denial causes (spec "Denials are observable"). SNI mismatch and
# non-TLS tunnels are TLS-level and only appear in the log (``bump=terminate``
# and ``peek ERR_PROTOCOL_UNKNOWN``).
CAUSES: dict[str, Cause] = {
    c.label: c
    for c in (
        Cause(
            "host-not-allowed",
            "ERR_SBX_HOST_NOT_ALLOWED",
            "the host is not on the egress allowlist",
        ),
        Cause(
            "ip-literal",
            "ERR_SBX_IP_LITERAL",
            "IP-literal destinations are not allowed; use a hostname",
        ),
        Cause(
            "port-not-allowed",
            "ERR_SBX_PORT_NOT_ALLOWED",
            "only HTTPS on port 443 and HTTP on port 80 are allowed",
        ),
        Cause(
            "private-address",
            "ERR_SBX_PRIVATE_ADDRESS",
            "the host resolves to a private, loopback, link-local or metadata address",
        ),
        Cause(
            "path-not-allowed",
            "ERR_SBX_PATH_NOT_ALLOWED",
            "this path is not allowed on an intercepted host",
        ),
        Cause(
            "plain-http-intercepted",
            "ERR_SBX_PLAIN_HTTP_INTERCEPTED",
            "this host is only reachable over HTTPS",
        ),
        Cause(
            "sni-mismatch",
            None,
            "the TLS SNI was missing or differed from the CONNECT host",
        ),
        Cause("non-tls-tunnel", None, "the tunnel did not carry TLS"),
    )
}

# Destinations a request must never reach in direct mode (resolved-address
# check): unspecified, private, CGNAT, loopback, link-local (incl. cloud
# metadata 169.254.169.254), benchmarking, multicast, reserved, and IPv6
# unique-local, link-local, multicast and NAT64-mapped forms.
PRIVATE_DESTINATIONS: tuple[str, ...] = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "224.0.0.0/4",
    "240.0.0.0/4",
    "::/128",
    "::1/128",
    # No ::ffff:0:0/96: Squid stores IPv4 as mapped addresses, so that range
    # would mean "all of IPv4"; the IPv4 ranges above already cover mapped forms.
    "64:ff9b::/96",
    "fc00::/7",
    "fe80::/10",
    "ff00::/8",
)

_HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(?:\.(?!-)[a-z0-9-]{1,63}(?<!-))*$"
)
# urlpath_regex entries are inserted verbatim; keep them to a safe alphabet.
_PATH_REGEX_RE = re.compile(r"^\^[A-Za-z0-9_/.\-?$]+$")


@dataclass(frozen=True)
class Upstream:
    """The parent proxy as the sidecar reaches it."""

    host: str
    port: int
    tls: bool = False
    ca_file: bool = False  # an upstream CA bundle is staged at UPSTREAM_CA


def upstream_for(
    endpoint, *, relay_port: int | None = None, ca_file: bool = False
) -> Upstream:
    """Map a parsed ``--internet-proxy`` endpoint to what the sidecar dials.

    - ``relay_port``: Apple ``container`` reaches a remote proxy through the
      host relay, which speaks plain HTTP and re-originates TLS on the host.
    - A loopback proxy on the host is reached at ``host.docker.internal``.
    - Otherwise the sidecar connects to the proxy directly.
    """
    if relay_port is not None:
        return Upstream(
            local_service_network.HOSTNAME, relay_port, tls=False, ca_file=ca_file
        )
    host = local_service_network.HOSTNAME if endpoint.local else endpoint.host
    return Upstream(
        host, endpoint.port, tls=endpoint.scheme == "https", ca_file=ca_file
    )


def _squid_domain(pattern: str) -> str:
    """``*.example.com`` -> ``.example.com`` (Squid's subdomain form)."""
    bare = pattern.removeprefix("*.")
    if not _HOST_RE.fullmatch(bare):
        raise ValueError(f"refusing to render invalid host {pattern!r}")
    return "." + bare if pattern.startswith("*.") else bare


def _validate_upstream(upstream: Upstream) -> None:
    host = upstream.host
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not _HOST_RE.fullmatch(host.lower().rstrip(".")):
            raise ValueError(
                f"refusing to render invalid upstream host {host!r}"
            ) from None
    if not 1 <= upstream.port <= 65535:
        raise ValueError(f"invalid upstream port {upstream.port}")


def render(
    policy: egress_policy.EffectivePolicy,
    *,
    upstream: Upstream | None = None,
    api_paths: Iterable[str] = egress_policy.COPILOT_API_PATHS,
) -> str:
    allowed = [_squid_domain(h) for h in policy.allow]
    bumped = [_squid_domain(h) for h in policy.bump_set]
    paths = list(api_paths) if bumped else []
    for path in paths:
        if not _PATH_REGEX_RE.fullmatch(path):
            raise ValueError(f"refusing to render unsafe path regex {path!r}")
    if bumped and not paths:
        raise ValueError("intercepted hosts need at least one allowed path")
    if upstream is not None:
        _validate_upstream(upstream)
    text = get_template("egress-sidecar/squid.conf.j2", trim_blocks=True).render(
        listen_port=LISTEN_PORT,
        ca_cert=CA_CERT,
        ca_key=CA_KEY,
        ssl_db=SSL_DB,
        pid_file=PID_FILE,
        upstream_ca=UPSTREAM_CA,
        error_directory=ERROR_DIRECTORY,
        sni_helper=SNI_HELPER,
        log_prefix=LOG_PREFIX,
        # An empty dstdomain ACL is an error; "invalid" (RFC 6761) never matches.
        allowed=allowed or ["invalid"],
        bumped=bumped,
        paths=paths,
        upstream=upstream,
        private_destinations=PRIVATE_DESTINATIONS,
        causes=CAUSES,
    )
    return text if text.endswith("\n") else text + "\n"


def error_pages() -> dict[str, str]:
    """Squid error page templates for the labelled causes (name -> HTML)."""
    template = get_template("egress-sidecar/error-page.html.j2")
    return {
        cause.page: template.render(cause=cause) + "\n"
        for cause in CAUSES.values()
        if cause.page
    }
