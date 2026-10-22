"""Host address selection for opt-in network test fixtures."""

import ipaddress
import socket


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
