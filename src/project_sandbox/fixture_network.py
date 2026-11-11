"""Host address selection for opt-in network test fixtures."""

from __future__ import annotations

import ipaddress
import socket
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

# Names each VM-backed runtime gives the host inside its containers. On macOS
# they resolve to a private address that the runtime forwards to the host's
# loopback interface.
HOST_ALIASES = {
    "docker": "host.docker.internal",
    "podman": "host.containers.internal",
}
LOOPBACK = "127.0.0.1"
RESOLVE_TIMEOUT = 120


@dataclass(frozen=True)
class FixtureHost:
    """Where a fixture listens on the host and how containers address it.

    ``address`` is what sandboxes and image builds connect to; ``bind`` is the
    host address the fixture listens on. They differ when a runtime's
    host-loopback alias reaches the fixture: the sandbox then sees an ordinary
    non-loopback address, while the host only sees loopback traffic, which
    host-side filters of traffic to the machine's own LAN address leave alone.
    """

    address: str
    bind: str

    @property
    def via_alias(self) -> bool:
        return self.address != self.bind


def default_host() -> str:
    """Ask the OS for an outbound IPv4 source address without sending packets."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            address = str(probe.getsockname()[0])
        parsed = ipaddress.ip_address(address)
        if parsed.is_loopback or parsed.is_unspecified or parsed.is_multicast:
            raise ValueError("no usable source address")
        return address
    except (OSError, ValueError) as exc:
        raise ValueError(
            "Cannot detect a local fixture IP; specify --proxy-fixture-host in the "
            "test runner or --fixture-host in the standalone harness."
        ) from exc


def alias_address(
    runtime: str,
    image: str,
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str:
    """The IPv4 address ``runtime``'s host alias resolves to inside ``image``."""
    alias = HOST_ALIASES[runtime]
    try:
        result = run(
            [runtime, "run", "--rm", image, "getent", "ahostsv4", alias],
            capture_output=True,
            text=True,
            timeout=RESOLVE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(
            f"Cannot resolve {alias} in a {runtime} container ({exc}); "
            "specify --fixture-host."
        ) from exc
    fields = (result.stdout or "").split()
    try:
        address = ipaddress.ip_address(fields[0]) if result.returncode == 0 else None
    except (IndexError, ValueError):
        address = None
    if (
        address is None
        or address.version != 4
        or address.is_loopback
        or address.is_unspecified
    ):
        detail = (result.stderr or result.stdout or "").strip()[-500:]
        raise ValueError(
            f"Cannot resolve {alias} to a non-loopback IPv4 address in a "
            f"{runtime} container (exit {result.returncode}: {detail}); "
            "specify --fixture-host."
        )
    return str(address)


def runtime_host(
    runtime: str,
    image: str,
    *,
    platform: str = sys.platform,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> FixtureHost:
    """The default fixture host for ``runtime`` on this machine.

    On macOS, Docker Desktop and Podman run containers in a VM that forwards a
    host alias to the Mac's loopback, so fixtures listen on loopback and
    containers use the alias's address. That keeps fixtures off the Mac's LAN
    address, whose local traffic security software may intercept (a connection
    opens but no data arrives). The host cannot reach the alias address, so
    callers must not check it from the host. Apple container and other
    platforms use the outbound source address for both: Apple container
    reaches remote proxies through a host relay, which needs a host route.
    """
    if platform == "darwin" and runtime in HOST_ALIASES:
        return FixtureHost(alias_address(runtime, image, run=run), LOOPBACK)
    address = default_host()
    return FixtureHost(address, address)
