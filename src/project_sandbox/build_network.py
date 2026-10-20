"""Explicit build-only proxy environment, independent of runtime networking."""

from collections.abc import Mapping

from . import proxy_endpoint

PROXY_NAMES = ("http_proxy", "https_proxy", "all_proxy", "no_proxy")


def from_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Normalize a single snapshot; an explicitly empty lowercase value wins."""
    values = {}
    for name in PROXY_NAMES:
        value = environment.get(name, environment.get(name.upper(), ""))
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise SystemExit(
                f"--build-proxy-from-env: {name} contains control characters"
            )
        if name != "no_proxy" and value:
            validate_url(value, name=name)
        values[name] = value
        values[name.upper()] = value
    if not any(values[name] for name in PROXY_NAMES[:-1]):
        raise SystemExit(
            "--build-proxy-from-env: no proxy URL is set in the environment"
        )
    return values


def validate_url(value: str, *, name: str) -> None:
    # Build endpoints are used verbatim: no runtime loopback forwarding occurs.
    proxy_endpoint.parse(
        value, option=f"--build-proxy-from-env: {name}", allow_https_loopback=True
    )


def docker_env(value: str) -> str:
    """Quote literal Dockerfile ENV data (which uses Docker, not shell, parsing)."""
    return (
        '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$") + '"'
    )
