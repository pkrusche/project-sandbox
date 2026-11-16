"""Explicit build-only proxy environment, independent of runtime networking.

The proxy reaches image builds only as ``--build-arg http_proxy=...`` values.
Every generated build stage that runs commands declares the names in
:data:`ARG_NAMES` with ``ARG`` (no default), so ``RUN`` instructions see them as
environment variables on every builder: BuildKit exports undeclared proxy
build-args on its own, Podman/Buildah does not. ``ARG`` values are not kept in
the image config, so the proxy never reaches the runtime environment. Declared
values do enter the ``RUN`` layer cache key, so a changed proxy URL or
forwarder port rebuilds those layers; without ``--build-proxy`` the ARGs stay
unset and change nothing.
"""

import ipaddress
import re
from collections.abc import Iterable

from . import proxy_endpoint

PROXY_NAMES = ("http_proxy", "https_proxy", "no_proxy")
# Declared in every generated build stage; matches environment()'s keys.
ARG_NAMES = (*PROXY_NAMES, *(name.upper() for name in PROXY_NAMES))
NO_PROXY = "localhost,127.0.0.1,::1"

_HOST_LABEL = r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
_HOST_PATTERN = re.compile(
    rf"\.?{_HOST_LABEL}(?:\.{_HOST_LABEL})*\.?(?::[0-9]{{1,5}})?\Z"
)


def parse(value: str) -> proxy_endpoint.ProxyEndpoint:
    """Validate a credential-free HTTP(S) proxy URL without echoing it."""
    # The host forwarder, not a Node client, terminates upstream TLS.
    return proxy_endpoint.parse(
        value,
        option="--build-proxy",
        allow_https_loopback=True,
        allow_https_ip=True,
    )


def parse_no_proxy(values: Iterable[str]) -> tuple[str, ...]:
    """Validate ``--build-no-proxy`` values (repeatable, comma-separated).

    Accepts host names, leading-dot domain suffixes, IP literals, and CIDR
    networks; host names may carry a ``:port``. Wildcards that would disable
    the proxy entirely are rejected.
    """
    entries: list[str] = []
    for value in values:
        for raw in value.split(","):
            entry = raw.strip()
            if not entry:
                raise SystemExit("--build-no-proxy entries must not be empty")
            if not _valid_no_proxy_entry(entry):
                raise SystemExit(
                    f"--build-no-proxy entry {entry!r} must be a host name, "
                    ".domain suffix, IP address, or CIDR network"
                )
            entries.append(entry)
    return tuple(dict.fromkeys(entries))


def _valid_no_proxy_entry(entry: str) -> bool:
    try:
        ipaddress.ip_network(entry.strip("[]"), strict=False)
    except ValueError:
        pass
    else:
        return True
    return bool(_HOST_PATTERN.match(entry))


def environment(url: str, no_proxy_extra: Iterable[str] = ()) -> dict[str, str]:
    """Lowercase and uppercase build proxy variables for one proxy URL."""
    no_proxy = ",".join(dict.fromkeys([*NO_PROXY.split(","), *no_proxy_extra]))
    values = {"http_proxy": url, "https_proxy": url, "no_proxy": no_proxy}
    return {
        **values,
        **{name.upper(): value for name, value in values.items()},
    }


def build_args(env: dict[str, str]) -> list[str]:
    """``--build-arg`` argv for the predefined proxy variables."""
    args: list[str] = []
    for name, value in env.items():
        args += ["--build-arg", f"{name}={value}"]
    return args


def redacted_build_args(env: dict[str, str]) -> list[str]:
    """Like :func:`build_args`, with values hidden for previews."""
    return build_args(dict.fromkeys(env, "<redacted>"))
