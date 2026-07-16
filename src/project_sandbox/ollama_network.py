"""Runtime-specific, loopback-safe access to a host Ollama server."""

from __future__ import annotations

import ipaddress
import json
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass

from .container_cli import APPLE_CONTAINER, DOCKER, PODMAN, Runtime

HOSTNAME = "ollama.project-sandbox.internal"
PORT = 11434
APPLE_SETUP_COMMAND = (
    "sudo container system dns create "
    f"{HOSTNAME} --localhost 203.0.113.113"
)


@dataclass
class ForwardingPlan:
    strategy: str
    endpoint: str | None = None
    add_host: str | None = None
    proxy: subprocess.Popen[str] | None = None

    def start(self) -> None:
        if self.strategy != "linux-bridge-socat":
            return
        if not self.endpoint:
            raise SystemExit("Internal error: Ollama bridge endpoint is missing")
        socat = shutil.which("socat")
        if socat is None:
            raise SystemExit(
                "--pi-ollama requires socat for this Linux bridge runtime; "
                "install socat and retry."
            )
        argv = [
            socat,
            f"TCP-LISTEN:{PORT},bind={self.endpoint},reuseaddr,fork",
            f"TCP:127.0.0.1:{PORT}",
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
            raise SystemExit(f"Could not start Ollama socat proxy: {exc}") from exc
        time.sleep(0.1)
        if self.proxy.poll() is not None:
            stderr = self.proxy.stderr.read().strip() if self.proxy.stderr else ""
            detail = f": {stderr}" if stderr else ""
            raise SystemExit(f"Ollama socat proxy failed to start{detail}")

    def close(self) -> None:
        proc = self.proxy
        self.proxy = None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def __enter__(self) -> ForwardingPlan:
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def prepare(runtime: Runtime, *, dry_run: bool = False) -> ForwardingPlan:
    """Select and validate the safest forwarding strategy for ``runtime``."""
    if runtime == APPLE_CONTAINER:
        if dry_run:
            return ForwardingPlan("apple-preconfigured-localhost-dns")
        try:
            endpoint = socket.gethostbyname(HOSTNAME)
        except socket.gaierror as exc:
            raise SystemExit(_apple_setup_error()) from exc
        _validate_endpoint(endpoint, allow_documentation=True)
        return ForwardingPlan("apple-preconfigured-localhost-dns", endpoint=endpoint)

    info = {} if dry_run else _runtime_info(runtime)
    if runtime == PODMAN and _podman_is_rootless_or_machine(info):
        return ForwardingPlan(
            "podman-native-host-alias", add_host=f"{HOSTNAME}:host-gateway"
        )
    if runtime == DOCKER and _docker_is_desktop(info):
        return ForwardingPlan(
            "docker-desktop-host-alias", add_host=f"{HOSTNAME}:host-gateway"
        )
    if dry_run:
        return ForwardingPlan("runtime-probe-required")

    endpoint = _bridge_gateway(runtime)
    _validate_endpoint(endpoint)
    _validate_bindable(endpoint)
    return ForwardingPlan(
        "linux-bridge-socat",
        endpoint=endpoint,
        add_host=f"{HOSTNAME}:{endpoint}",
    )


def describe(plan: ForwardingPlan) -> str:
    suffix = f" ({plan.endpoint})" if plan.endpoint else ""
    return f"Ollama forwarding strategy: {plan.strategy}{suffix}"


def _apple_setup_error() -> str:
    return (
        "Apple container localhost forwarding is not configured for "
        f"{HOSTNAME}. Run this command yourself, then retry:\n  "
        f"{APPLE_SETUP_COMMAND}\n"
        "This changes macOS DNS/packet-filter state and may disable Private Relay; "
        "project-sandbox will never invoke sudo or change it automatically."
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
            "this runtime mode cannot safely forward loopback Ollama."
        )
    return endpoint


def _validate_endpoint(value: str, *, allow_documentation: bool = False) -> None:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise SystemExit(f"Unsafe Ollama forwarding endpoint: {value!r}") from exc
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
        raise SystemExit(f"Unsafe Ollama forwarding endpoint: {value}")


def _validate_bindable(endpoint: str) -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((endpoint, PORT))
    except OSError as exc:
        raise SystemExit(
            f"Cannot bind the Ollama proxy to {endpoint}:{PORT}: {exc}"
        ) from exc
    finally:
        probe.close()
