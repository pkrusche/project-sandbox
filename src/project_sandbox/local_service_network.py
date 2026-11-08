"""Runtime-specific, loopback-safe access to host-local TCP services."""

from __future__ import annotations

import ipaddress
import json
import subprocess
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from .container_cli import APPLE_CONTAINER, CHROOT, DOCKER, PODMAN, Runtime

if TYPE_CHECKING:
    from . import local_forwarder

HOSTNAME = "host.docker.internal"
PORT = 11434
# Apple container redirects this TEST-NET-3 address to the host's loopback.
APPLE_LOCALHOST_ADDRESS = "203.0.113.113"
APPLE_SETUP_COMMAND = (
    f"sudo container system dns create {HOSTNAME} --localhost {APPLE_LOCALHOST_ADDRESS}"
)
APPLE_DELETE_COMMAND = f"sudo container system dns delete {HOSTNAME}"
APPLE_RESTART_COMMAND = "container system stop && container system start"
PASTA_STRATEGY = "podman-pasta-forward"
# Docker Engine: one shared, leased forwarder per (gateway, port), see
# local_forwarder (share-local-service-forwarders).
BRIDGE_STRATEGY = "linux-bridge-forwarder"
# pasta otherwise copies the host's own addresses into the container, so a
# service on the host's LAN address (a proxy, a gateway) is unreachable from
# builds and sandboxes. 198.18.0.0/15 is reserved for benchmarking (RFC 2544),
# so it is unlikely to shadow a network the sandbox must reach.
PASTA_ADDRESS = "198.18.0.2"
PASTA_PREFIX = 24
PASTA_GATEWAY = "198.18.0.1"


@dataclass(frozen=True)
class LocalService:
    """Host-loopback service that must be forwarded into the sandbox."""

    label: str
    port: int
    protocol: str = "tcp"
    loopback_host: str = "127.0.0.1"

    def __post_init__(self) -> None:
        if self.protocol != "tcp":
            raise ValueError(f"Unsupported local-service protocol: {self.protocol}")
        if not 1 <= self.port <= 65535:
            raise ValueError(f"Invalid {self.label} loopback port: {self.port}")


@dataclass
class ForwardingPlan:
    strategy: str
    endpoint: str | None = None
    add_host: str | None = None
    # This session's lease on the shared bridge forwarder (BRIDGE_STRATEGY).
    lease: local_forwarder.Lease | None = None
    hostname: str = HOSTNAME
    port: int = PORT
    label: str = "Ollama"
    loopback_host: str = "127.0.0.1"

    def start(self) -> None:
        """Attach to (or start) the shared bridge forwarder and lease it.

        The forwarder is detached and shared with concurrent sessions; it exits
        on its own shortly after the last lease is released.
        """
        if self.strategy != BRIDGE_STRATEGY or self.lease is not None:
            return
        if not self.endpoint:
            raise SystemExit(f"Internal error: {self.label} bridge endpoint is missing")
        # Imported here: local_forwarder -> egress_relay -> proxy_endpoint
        # imports this module.
        from . import local_forwarder

        self.lease = local_forwarder.attach(
            self.endpoint,
            self.port,
            target_host=self.loopback_host,
            label=self.label,
        )

    def close(self) -> None:
        """Release this session's lease; the forwarder itself is never killed."""
        lease, self.lease = self.lease, None
        if lease is not None:
            lease.release()

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def prepare(
    runtime: Runtime,
    *,
    dry_run: bool = False,
    hostname: str = HOSTNAME,
    port: int = PORT,
    label: str = "Ollama",
    loopback_host: str = "127.0.0.1",
) -> ForwardingPlan:
    """Select and validate the safest forwarding strategy for ``runtime``."""
    hostname = HOSTNAME
    if runtime == CHROOT:
        return ForwardingPlan(
            "chroot-shared-loopback",
            endpoint="127.0.0.1",
            add_host=f"{hostname}:127.0.0.1",
            hostname=hostname,
            port=port,
            label=label,
            loopback_host=loopback_host,
        )

    if runtime == APPLE_CONTAINER:
        return ForwardingPlan(
            "apple-configured-host-alias",
            hostname=hostname,
            port=port,
            label=label,
            loopback_host=loopback_host,
        )

    info = {} if dry_run else _runtime_info(runtime)
    if runtime == PODMAN and _podman_uses_local_pasta(info):
        # pasta copies the host's addresses into the container, and its
        # host-gateway alias reaches the host's LAN address rather than its
        # loopback. Forward exactly the planned ports to host loopback instead.
        return ForwardingPlan(
            PASTA_STRATEGY,
            add_host=f"{hostname}:127.0.0.1",
            hostname=hostname,
            port=port,
            label=label,
            loopback_host=loopback_host,
        )
    if runtime == PODMAN and not dry_run:
        # Runtime selection already refused rootful podman; never fall back to
        # a bridge listener if that check was bypassed.
        if _podman_security(info).get("rootless") is not True:
            raise SystemExit(
                "project-sandbox supports rootless podman only; podman reports "
                "a rootful (or unknown) setup."
            )
        _require_podman_host_gateway(info)
        return ForwardingPlan(
            "podman-native-host-alias",
            add_host=f"{hostname}:host-gateway",
            hostname=hostname,
            port=port,
            label=label,
            loopback_host=loopback_host,
        )
    if runtime == DOCKER and _docker_is_desktop(info):
        return ForwardingPlan(
            "docker-desktop-host-alias",
            add_host=f"{hostname}:host-gateway",
            hostname=hostname,
            port=port,
            label=label,
            loopback_host=loopback_host,
        )
    if dry_run:
        return ForwardingPlan(
            "runtime-probe-required",
            hostname=hostname,
            port=port,
            label=label,
            loopback_host=loopback_host,
        )

    endpoint = _bridge_gateway(runtime)
    _validate_endpoint(endpoint)
    _validate_bindable(endpoint, port=port, label=label, loopback_host=loopback_host)
    return ForwardingPlan(
        BRIDGE_STRATEGY,
        endpoint=endpoint,
        add_host=f"{hostname}:{endpoint}",
        hostname=hostname,
        port=port,
        label=label,
        loopback_host=loopback_host,
    )


def prepare_services(
    runtime: Runtime,
    services: list[LocalService],
    *,
    dry_run: bool = False,
) -> list[ForwardingPlan]:
    """Prepare ordered plans, rejecting ambiguous duplicate host ports."""
    ports: set[int] = set()
    for service in services:
        if service.port in ports:
            raise SystemExit(
                f"Duplicate local-service port {service.port} requested by {service.label}"
            )
        ports.add(service.port)
    if not services:
        return []

    first = services[0]
    base = prepare(
        runtime,
        dry_run=dry_run,
        hostname=HOSTNAME,
        port=first.port,
        label=first.label,
        loopback_host=first.loopback_host,
    )
    if base.strategy == PASTA_STRATEGY:
        for service in services:
            if not _is_ipv4_loopback(service.loopback_host):
                raise SystemExit(
                    f"{service.label} listens on {service.loopback_host}, but "
                    "rootless podman forwards host-local services over IPv4 "
                    "loopback only; listen on 127.0.0.1 instead."
                )
    plans = [base]
    for service in services[1:]:
        # Runtime strategy and hostname endpoint are sandbox-wide decisions.
        # Docker Engine still needs one check and one shared forwarder per port.
        if base.strategy == BRIDGE_STRATEGY:
            assert base.endpoint is not None
            _validate_bindable(
                base.endpoint,
                port=service.port,
                label=service.label,
                loopback_host=service.loopback_host,
            )
        plans.append(
            ForwardingPlan(
                base.strategy,
                endpoint=base.endpoint,
                hostname=base.hostname,
                port=service.port,
                label=service.label,
                loopback_host=service.loopback_host,
            )
        )
    return plans


def uses_local_pasta(runtime: Runtime) -> bool:
    """Whether ``runtime`` is rootless podman networking with pasta on this host."""
    return runtime == PODMAN and _podman_uses_local_pasta(_runtime_info(runtime))


def network_argument(plans: list[ForwardingPlan], *, pasta: bool = False) -> str | None:
    """The ``--network`` value for a build or session, or None for the default.

    With pasta, the container gets its own address instead of a copy of the
    host's. pasta's ``-T`` forwards each listed container-loopback port to the
    same host-loopback port and nothing else, so the port list is the boundary.
    ``-4`` keeps pasta off IPv6, whose host-side connection attempt fails on
    affected pasta releases before IPv4 is tried.
    """
    ports = [plan.port for plan in plans if plan.strategy == PASTA_STRATEGY]
    if not (pasta or ports):
        return None
    options = ["-a", PASTA_ADDRESS, "-n", str(PASTA_PREFIX), "-g", PASTA_GATEWAY]
    if ports:
        options += ["-4", *(f"-T,{port}:{port}" for port in ports)]
    return "pasta:" + ",".join(options)


def describe(plan: ForwardingPlan) -> str:
    if plan.strategy == BRIDGE_STRATEGY and plan.endpoint:
        upstream = (
            f"[{plan.loopback_host}]"
            if ":" in plan.loopback_host
            else plan.loopback_host
        )
        suffix = (
            f" ({plan.endpoint}:{plan.port} -> {upstream}:{plan.port}, "
            "shared with concurrent sessions)"
        )
    else:
        suffix = f" ({plan.endpoint})" if plan.endpoint else ""
    return f"{plan.label} forwarding strategy: {plan.strategy}{suffix}"


def apple_setup_notice(label: str) -> str:
    """Explain the administrator-managed Apple localhost forwarding setup."""
    return (
        f"[W] {label} forwarding with Apple container requires this one-time setup:\n"
        f"    {APPLE_SETUP_COMMAND}\n"
        "This DNS/PF change can disrupt container Internet access until runtime "
        "networking is rebuilt. Restart the container system afterward with:\n"
        f"    {APPLE_RESTART_COMMAND}\n"
        "If the alias already exists but connections time out instead of being "
        "refused, it resolves without redirecting; delete and re-create it, then "
        "restart again:\n"
        f"    {APPLE_DELETE_COMMAND}"
    )


def _runtime_info(runtime: Runtime) -> dict:
    proc = subprocess.run(
        [runtime.binary, "info", "--format", "json"],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"Could not inspect {runtime.name} networking: "
            f"{proc.stderr.strip() or 'runtime info failed'}"
        )
    try:
        value = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Could not parse {runtime.name} info JSON") from exc
    return value if isinstance(value, dict) else {}


def _podman_security(info: dict) -> dict:
    host = info.get("host", {})
    security = host.get("security", {}) if isinstance(host, dict) else {}
    return security if isinstance(security, dict) else {}


def _podman_uses_local_pasta(info: dict) -> bool:
    """Rootless podman on this Linux host networking containers with pasta.

    podman machine and remote connections are excluded: their pasta runs in a
    VM or on another host, whose loopback is not this host's.
    """
    host = info.get("host", {})
    if not isinstance(host, dict) or sys.platform != "linux":
        return False
    return (
        _podman_security(info).get("rootless") is True
        and host.get("rootlessNetworkCmd") == "pasta"
    )


def _is_ipv4_loopback(value: str) -> bool:
    try:
        return ipaddress.ip_address(value) == ipaddress.ip_address("127.0.0.1")
    except ValueError:
        return False


def _require_podman_host_gateway(info: dict) -> None:
    """Reject podman releases whose ``--add-host`` cannot resolve ``host-gateway``.

    Podman 4.3 fails at container creation with "invalid IP address in
    add-host". There is no safe alternative to reach the host loopback
    (rootless slirp4netns blocks it unless allow_host_loopback is enabled, which
    would expose every host loopback service), so fail early and clearly. The
    cut-off is conservative; an unreadable version is assumed to be supported.
    """
    version = str(info.get("version", {}).get("Version", ""))
    try:
        parsed = tuple(int(part) for part in version.split(".")[:2])
    except ValueError:
        return
    if len(parsed) == 2 and parsed < (5, 0):
        raise SystemExit(
            f"podman {version} cannot resolve --add-host host-gateway, which "
            "forwarding a host-local service (Ollama, agent proxy) requires; "
            "upgrade to podman >= 5.0."
        )


def _docker_is_desktop(info: dict) -> bool:
    operating_system = str(
        info.get("OperatingSystem", info.get("operatingSystem", ""))
    ).lower()
    return "docker desktop" in operating_system


def _bridge_gateway(runtime: Runtime) -> str:
    proc = subprocess.run(
        [
            runtime.binary,
            "network",
            "inspect",
            "bridge",
            "--format",
            "{{(index .IPAM.Config 0).Gateway}}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    endpoint = proc.stdout.strip()
    if proc.returncode != 0 or not endpoint:
        raise SystemExit(
            f"Could not discover a host-bindable {runtime.name} bridge gateway; "
            "this runtime mode cannot safely forward a host-loopback service."
        )
    return endpoint


def _validate_endpoint(value: str, *, allow_documentation: bool = False) -> None:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise SystemExit(
            f"Unsafe local-service forwarding endpoint: {value!r}"
        ) from exc
    documentation = any(
        address in network
        for network in (
            ipaddress.ip_network("192.0.2.0/24"),
            ipaddress.ip_network("198.51.100.0/24"),
            ipaddress.ip_network("203.0.113.0/24"),
        )
    )
    if (
        address.is_unspecified
        or address.is_loopback
        or address.is_multicast
        or (not address.is_private and not (allow_documentation and documentation))
    ):
        raise SystemExit(f"Unsafe local-service forwarding endpoint: {value}")


def _validate_bindable(
    endpoint: str,
    *,
    port: int = PORT,
    label: str = "Ollama",
    loopback_host: str = "127.0.0.1",
) -> None:
    """Early, pre-build check: ``endpoint:port`` is free or already served by a
    live project-sandbox forwarder to ``loopback_host:port``.

    A foreign listener fails here, before the image build; attaching repeats
    the check under the forwarder's control lock.
    """
    from . import local_forwarder  # see ForwardingPlan.start

    local_forwarder.check_available(
        endpoint, port, target_host=loopback_host, label=label
    )
