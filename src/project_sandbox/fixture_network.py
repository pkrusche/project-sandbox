"""Host address selection for opt-in network test fixtures."""

from __future__ import annotations

import contextlib
import ipaddress
import json
import secrets
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass

from .local_service_network import APPLE_LOCALHOST_ADDRESS

# Names each VM-backed runtime gives the host inside its containers. On macOS
# they resolve to a private address that the runtime forwards to the host's
# loopback interface.
HOST_ALIASES = {
    "docker": "host.docker.internal",
    "podman": "host.containers.internal",
}
LOOPBACK = "127.0.0.1"
RESOLVE_TIMEOUT = 120

# Apple container fixture forwarder: e2e-cleanup.py removes leftovers only when
# both the name prefix and the label match.
FORWARDER_PREFIX = "project-sandbox-e2e-forwarder-"
FORWARDER_LABEL = "io.project-sandbox.e2e-fixture"
FORWARDER_LABEL_VALUE = "forwarder"
FORWARDER_READY_TIMEOUT = 60.0
# Stdlib-only TCP relay run with ``python3 -c`` inside the forwarder container:
# ``LISTEN_HOST TARGET_HOST LISTEN_PORT:TARGET_PORT...``. Each direction is
# half-closed on EOF so request/response streams end cleanly.
FORWARDER_SCRIPT = """\
import signal, socket, sys, threading

listen_host, target_host, specs = sys.argv[1], sys.argv[2], sys.argv[3:]


def relay(client, port):
    try:
        upstream = socket.create_connection((target_host, port), timeout=10)
    except OSError:
        client.close()
        return
    upstream.settimeout(None)
    lock, open_directions = threading.Lock(), [2]

    def pipe(source, target):
        try:
            while data := source.recv(65536):
                target.sendall(data)
        except OSError:
            pass
        try:
            target.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        with lock:
            open_directions[0] -= 1
            last = open_directions[0] == 0
        if last:
            client.close()
            upstream.close()

    threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()
    pipe(client, upstream)


def serve(listener, port):
    while True:
        client, _ = listener.accept()
        threading.Thread(target=relay, args=(client, port), daemon=True).start()


listeners = []
for spec in specs:
    listen_port, target_port = (int(part) for part in spec.split(":"))
    listener = socket.create_server((listen_host, listen_port), backlog=64)
    listeners.append((listener, target_port))
for listener, target_port in listeners:
    threading.Thread(target=serve, args=(listener, target_port), daemon=True).start()
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
print("ready", flush=True)
while True:
    signal.pause()
"""


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
    Fixtures that need Apple container to avoid the LAN address use
    :func:`apple_forwarder` instead.
    """
    if platform == "darwin" and runtime in HOST_ALIASES:
        return FixtureHost(alias_address(runtime, image, run=run), LOOPBACK)
    address = default_host()
    return FixtureHost(address, address)


def loopback_ports(count: int) -> tuple[int, ...]:
    """Pick ``count`` distinct free loopback ports (released before returning).

    Fixtures rebind them per scenario; their servers set SO_REUSEADDR.
    """
    with contextlib.ExitStack() as stack:
        sockets = [
            stack.enter_context(socket.create_server((LOOPBACK, 0)))
            for _ in range(count)
        ]
        return tuple(sock.getsockname()[1] for sock in sockets)


def _container_address(inspect_stdout: str) -> str:
    """The IPv4 address of the single container in ``container inspect`` output."""
    entry = json.loads(inspect_stdout or "[]")[0]
    # Current releases nest networks in ``status``; older ones list them flat.
    status = entry.get("status")
    networks = (status if isinstance(status, dict) else {}).get("networks")
    for network in networks or entry.get("networks") or []:
        value = str(network.get("ipv4Address", "")).split("/", 1)[0]
        address = ipaddress.ip_address(value)
        if address.version == 4 and not (address.is_loopback or address.is_unspecified):
            return str(address)
    raise ValueError("no IPv4 address")


@contextlib.contextmanager
def apple_forwarder(
    image: str,
    ports: Sequence[int],
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    connect: Callable[..., socket.socket] = socket.create_connection,
    timeout: float = FORWARDER_READY_TIMEOUT,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Generator[FixtureHost, None, None]:
    """Reach host-loopback fixtures on ``ports`` through an Apple container.

    Apple container's counterpart of a VM runtime's host-loopback alias. The
    alias address 203.0.113.113 (see docs/apple-container-dns.md) is only
    redirected for container traffic, and the CLI's host-side relay to a remote
    Internet proxy needs an address the host reaches as well. So a throwaway
    container relays each port on its own address to the same port behind that
    alias, while the fixtures listen on host loopback. Both the host and other
    containers reach the container address without touching the Mac's own
    LAN address, whose local traffic security software may intercept.
    """
    name = FORWARDER_PREFIX + secrets.token_hex(4)
    command = [
        "container",
        "run",
        "--detach",
        "--rm",
        "--name",
        name,
        "--label",
        f"{FORWARDER_LABEL}={FORWARDER_LABEL_VALUE}",
        image,
        "python3",
        "-c",
        FORWARDER_SCRIPT,
        "0.0.0.0",
        APPLE_LOCALHOST_ADDRESS,
        *(f"{port}:{port}" for port in ports),
    ]
    advice = "specify --fixture-host."

    def capture(argv):
        return run(
            argv,
            capture_output=True,
            text=True,
            timeout=RESOLVE_TIMEOUT,
            check=False,
        )

    try:
        started = capture(command)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(
            f"Cannot start the fixture forwarder ({exc}); {advice}"
        ) from exc
    if started.returncode != 0:
        detail = (started.stderr or started.stdout or "").strip()[-500:]
        raise ValueError(
            f"Cannot start the fixture forwarder in {image} (exit "
            f"{started.returncode}: {detail}); {advice}"
        )
    try:
        deadline = clock() + timeout
        # A detached container is reported before its network is attached.
        while True:
            try:
                address = _container_address(
                    capture(["container", "inspect", name]).stdout
                )
                break
            except (OSError, subprocess.TimeoutExpired, ValueError, LookupError) as exc:
                if clock() >= deadline:
                    raise ValueError(
                        f"Cannot find the address of fixture forwarder {name} "
                        f"({exc}); {advice}"
                    ) from exc
                sleep(0.2)
        pending = list(ports)
        while pending:
            try:
                connect((address, pending[0]), timeout=2).close()
                pending.pop(0)
            except OSError as exc:
                if clock() >= deadline:
                    try:
                        logs = capture(["container", "logs", name]).stdout or ""
                    except (OSError, subprocess.TimeoutExpired):
                        logs = ""
                    raise ValueError(
                        f"Fixture forwarder {name} at {address} did not listen on "
                        f"port {pending[0]} ({exc}); logs: {logs.strip()[-500:]!r}; "
                        f"{advice}"
                    ) from None
                sleep(0.2)
        yield FixtureHost(address, LOOPBACK)
    finally:
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            capture(["container", "delete", "--force", name])
