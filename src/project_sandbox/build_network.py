"""Explicit build-only proxy environment, independent of runtime networking."""

from . import proxy_endpoint

PROXY_NAMES = ("http_proxy", "https_proxy", "no_proxy")
NO_PROXY = "localhost,127.0.0.1,::1"


def parse(value: str) -> proxy_endpoint.ProxyEndpoint:
    """Validate a credential-free HTTP(S) proxy URL without echoing it."""
    return proxy_endpoint.parse(
        value, option="--build-proxy", allow_https_loopback=True
    )


def environment(url: str) -> dict[str, str]:
    """Lowercase and uppercase build proxy variables for one proxy URL."""
    values = {"http_proxy": url, "https_proxy": url, "no_proxy": NO_PROXY}
    return {
        **values,
        **{name.upper(): value for name, value in values.items()},
    }


def docker_env(value: str) -> str:
    """Quote literal Dockerfile ENV data (which uses Docker, not shell, parsing)."""
    return (
        '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$") + '"'
    )
