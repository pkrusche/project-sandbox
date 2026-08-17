"""Runtime-specific, loopback-safe access to host-local TCP services."""

from __future__ import annotations

import ipaddress
import json
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from typing import Self

from .container_cli import APPLE_CONTAINER, CHROOT, DOCKER, PODMAN, Runtime

HOSTNAME = "host.docker.internal"
APPLE_HOSTNAME = HOSTNAME
PORT = 11434
APPLE_SETUP_COMMAND = (
    f"sudo container system dns create {APPLE_HOSTNAME} --localhost 203.0.113.113"
)
APPLE_RESTART_COMMAND = "container system stop && container system start"


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
    proxy: subprocess.Popen[str] | None = None
    hostname: str = HOSTNAME
    port: int = PORT
    label: str = "Ollama"
    loopback_host: str = "127.0.0.1"

    def start(self) -> None:
        if self.strategy != "linux-bridge-socat":
            return
        if not self.endpoint:
            raise SystemExit(f"Internal error: {self.label} bridge endpoint is missing")
        socat = shutil.which("socat")
        if socat is None:
            raise SystemExit(
                f"{self.label} requires socat for this Linux bridge runtime; "
                "install socat and retry."
            )
        upstream = (
            f"TCP6:[{self.loopback_host}]:{self.port}"
            if ":" in self.loopback_host
            else f"TCP:{self.loopback_host}:{self.port}"
        )
        argv = [
            socat,
            f"TCP-LISTEN:{self.port},bind={self.endpoint},reuseaddr,fork",
            upstream,
        ]
        try:
            self.proxy = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            raise SystemExit(
                f"Could not start {self.label} socat proxy: {exc}"
            ) from exc
        time.sleep(0.1)
        if self.proxy.poll() is not None:
            stderr = self.proxy.stderr.read().strip() if self.proxy.stderr else ""
            detail = f": {stderr}" if stderr else ""
            raise SystemExit(f"{self.label} socat proxy failed to start{detail}")

    def close(self) -> None:
        proc = self.proxy
        self.proxy = None
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

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
    hostname = forwarding_hostname(runtime, hostname)
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
    if runtime == PODMAN and _podman_is_rootless_or_machine(info):
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
    _validate_bindable(endpoint, port=port, label=label)
    return ForwardingPlan(
        "linux-bridge-socat",
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
    plans: list[ForwardingPlan] = []
    for service in services:
        if service.port in ports:
            raise SystemExit(
                f"Duplicate local-service port {service.port} requested by {service.label}"
            )
        ports.add(service.port)
        plan = prepare(
            runtime,
            dry_run=dry_run,
            hostname=HOSTNAME,
            port=service.port,
            label=service.label,
            loopback_host=service.loopback_host,
        )
        # A sandbox needs only one hostname mapping even though Linux needs a
        # separate managed socat listener for each port.
        if plans and plan.add_host == plans[0].add_host:
            plan.add_host = None
        plans.append(plan)
    return plans


def forwarding_hostname(runtime: Runtime, fallback: str) -> str:
    """Return the runtime-native name used to reach a host-loopback service."""
    return HOSTNAME


def describe(plan: ForwardingPlan) -> str:
    suffix = f" ({plan.endpoint})" if plan.endpoint else ""
    return f"{plan.label} forwarding strategy: {plan.strategy}{suffix}"


def apple_setup_notice(label: str) -> str:
    """Explain the administrator-managed Apple localhost forwarding setup."""
    return (
        f"[W] {label} forwarding with Apple container requires this one-time setup:\n"
        f"    {APPLE_SETUP_COMMAND}\n"
        "This DNS change might disable network connectivity. Restart the container "
        "system afterward with:\n"
        f"    {APPLE_RESTART_COMMAND}"
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


def _podman_is_rootless_or_machine(info: dict) -> bool:
    host = info.get("host", {})
    security = host.get("security", {}) if isinstance(host, dict) else {}
    return bool(
        security.get("rootless")
        or host.get("rootless")
        or info.get("remoteSocket")
        or info.get("version", {}).get("RemoteSocket")
    )


def _docker_is_desktop(info: dict) -> bool:
    operating_system = str(
        info.get("OperatingSystem", info.get("operatingSystem", ""))
    ).lower()
    return "docker desktop" in operating_system


def _bridge_gateway(runtime: Runtime) -> str:
    network = "bridge" if runtime == DOCKER else "podman"
    proc = subprocess.run(
        [
            runtime.binary,
            "network",
            "inspect",
            network,
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
        raise SystemExit(f"Unsafe local-service forwarding endpoint: {value!r}") from exc
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
    endpoint: str, *, port: int = PORT, label: str = "Ollama"
) -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((endpoint, port))
    except OSError as exc:
        raise SystemExit(
            f"Cannot bind the {label} proxy to {endpoint}:{port}: {exc}"
        ) from exc
    finally:
        probe.close()
