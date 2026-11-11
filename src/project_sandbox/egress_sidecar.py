"""The global egress sidecar of each runtime (egress-sidecar-macos, -linux).

One container ``project-sandbox-egress`` serves every ``--egress sidecar``
sandbox of a runtime. It publishes Squid's port on exactly one host address
(:class:`Strategy`, egress-sidecar-linux design.md Decision 1):

- Apple container, Docker Desktop, Podman machine, rootless Podman with
  slirp4netns: host loopback (``127.0.0.1:<[egress.sidecar] port>``); sandboxes
  reach it through the firewall-mode local-service forwarding.
- Docker Engine: the default bridge's gateway (``172.17.0.1:<port>``); sandboxes
  map ``host.docker.internal`` to the gateway, with no per-session listener.
- Rootless Podman with pasta: host loopback, with the sidecar itself on a pasta
  network; sandboxes get the port in their pasta ``-T`` list.
- Rootless Docker: rejected.

It is configured only by ``egress.toml`` and runs until
``project-sandbox-egress down``.

Host state, per runtime (design.md Decision 9)::

    egress/<runtime>/
    ├─ sidecar/        mounted read-only into the sidecar at /project-sandbox-egress
    │  ├─ squid.conf  upstream-ca.pem
    │  └─ ca.pem  ca-key.pem (0600)
    ├─ state.json      a cache of what the sidecar runs; every decision re-inspects
    ├─ relay.lock relay.pid relay.port relay.log   (egress_relay: the Apple relay
    │                                                 or the Docker Engine parent forwarder)
    └─ sessions/<sid>/sandbox/   one session's inputs, mounted into that sandbox

:func:`ensure` (launches and ``up``), :func:`down`, :func:`restart` and
:func:`reload` serialize on one lock, so two launches never create two
sidecars. Desired state is split into a reload fingerprint (the rendered
``squid.conf``: applied live) and a restart fingerprint (image, Copilot/CA
constraints, upstream, port, resources, log options: warned about, never applied
implicitly, because a restart breaks every running sandbox). The published port
is the exception a launch cannot work around: its sandbox is built for the
configured port, so a launch fails on port drift.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import (
    egress_policy,
    egress_relay,
    local_service_network,
    mount_storage,
    session_ca,
    sidecar_image,
    squid_config,
)
from .container_cli import (
    APPLE_CONTAINER,
    DOCKER,
    PODMAN,
    MountSpec,
    Runtime,
    _mount_arg,
)
from .host_locks import path_lock

CONTAINER = "project-sandbox-egress"
# The internal network of the unreleased squid-sidecar line; ``down`` removes a
# leftover one on a best-effort basis (design.md Decision 9).
LEGACY_NETWORK = "sbx-egress"
ROLE_LABEL = "project-sandbox.egress.role"
FINGERPRINT_LABEL = "project-sandbox.egress.fingerprint"
PORT_LABEL = "project-sandbox.egress.port"
ADDRESS_LABEL = "project-sandbox.egress.address"
LOCK_KIND = "egress-sidecar"
# Squid listens here inside the sidecar; the host side is [egress.sidecar] port.
CONTAINER_PORT = squid_config.LISTEN_PORT
PUBLISH_ADDRESS = "127.0.0.1"

# Endpoint strategies (egress-sidecar-linux design.md Decision 1).
LOOPBACK = "loopback"
DOCKER_GATEWAY = "docker-gateway"
PODMAN_PASTA = "podman-pasta"
# Parent-proxy relays: the Apple CONNECT relay and the Docker Engine forwarder.
RELAY_CONNECT = "connect"
RELAY_FORWARD = "forward"

INPUT_MOUNT = squid_config.INPUT_DIR
SIDECAR_SUBDIR = "sidecar"
SESSIONS_SUBDIR = "sessions"
STATE_NAME = "state.json"
CONF_NAME = "squid.conf"
UPSTREAM_CA_NAME = "upstream-ca.pem"
# The sandbox mounts only this subdirectory of its session directory at the
# same path; the firewall's sidecar block reads it (design.md Decision 8).
SANDBOX_SUBDIR = "sandbox"
SANDBOX_MOUNT = "/project-sandbox-egress"
# 1 was the squid-sidecar line's internal-network state; it means "needs up".
STATE_VERSION = 2

# Plain-HTTP request the sidecar must deny with our labelled page when ready.
READINESS_HOST = "sidecar-readiness.invalid"
READINESS_MARKER = "project-sandbox-egress: host-not-allowed"
READINESS_TIMEOUT = 30.0
# How long a cold start waits for the published port to be released by the
# container it just removed before reporting it as taken.
PORT_RELEASE_TIMEOUT = 5.0
# Runtime log caps (design.md Decision 10). Apple container has no log options.
LOG_OPTIONS: dict[str, tuple[str, ...]] = {
    DOCKER.name: ("max-size=10m", "max-file=3"),
    PODMAN.name: ("max-size=10m",),
    APPLE_CONTAINER.name: (),
}
# Human names for the restart-only inputs, used in drift warnings.
RESTART_FIELDS: dict[str, str] = {
    "image": "sidecar image",
    "copilot": "[egress.copilot] enabled",
    "upstream_url": "[egress.upstream] url",
    "upstream_ca_sha256": "[egress.upstream] ca_cert",
    "port": "[egress.sidecar] port",
    "address": "published address (Docker's default bridge gateway)",
    "memory": "[egress.sidecar] memory",
    "cpus": "[egress.sidecar] cpus",
    "log_options": "runtime log options",
}
RESTART_HINT = (
    "Run `project-sandbox-egress restart` to apply; it interrupts every running "
    "sidecar-mode sandbox."
)

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


class SidecarError(RuntimeError):
    """The sidecar could not be started or changed; the message is user-facing."""


def default_runner(argv: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(argv), capture_output=True, text=True, errors="replace", check=False
    )


def endpoint(port: int, address: str = PUBLISH_ADDRESS) -> str:
    """The published endpoint on the host."""
    return f"{address}:{port}"


def describe_address(address: str) -> str:
    return (
        "host loopback"
        if address == PUBLISH_ADDRESS
        else "Docker's default bridge gateway"
    )


# --------------------------------------------------------------------------
# Host layout and state.json
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Layout:
    root: Path  # egress/<runtime>

    @property
    def sidecar(self) -> Path:
        return self.root / SIDECAR_SUBDIR

    @property
    def sessions(self) -> Path:
        return self.root / SESSIONS_SUBDIR

    @property
    def state_file(self) -> Path:
        return self.root / STATE_NAME


def layout(runtime: Runtime, root: Path | None = None) -> Layout:
    """``$XDG_STATE_HOME/project-sandbox/egress/<runtime>``."""
    return Layout((root or session_ca.egress_root()) / runtime.name)


_STATE_KEYS = {
    "version",
    "image",
    "restart_fingerprint",
    "restart_inputs",
    "reload_fingerprint",
    "copilot",
    "upstream_url",
    "upstream",
    "relay_port",
    "port",
    "started_at",
}


def read_state(lay: Layout) -> dict | None:
    """``state.json``, or ``None`` when missing, garbled or incomplete.

    ``None`` means "needs up": the caller replaces the sidecar instead of
    guessing what it runs.
    """
    try:
        data = json.loads(lay.state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        not isinstance(data, dict)
        or data.get("version") != STATE_VERSION
        or not _STATE_KEYS <= set(data)
    ):
        return None
    port = data["port"]
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return None
    return data


def write_state(lay: Layout, data: dict) -> None:
    mount_storage.ensure_private_dirs(lay.root)
    _write_file(lay.state_file, json.dumps(data, indent=2, sort_keys=True) + "\n")


def _write_file(path: Path, text: str | bytes, mode: int = 0o644) -> None:
    """Replace ``path`` atomically (the directory, not the file, is mounted)."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode() if isinstance(text, str) else text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _complete(lay: Layout) -> bool:
    names = (CONF_NAME, session_ca.CERT_NAME, session_ca.KEY_NAME)
    return all((lay.sidecar / name).is_file() for name in names)


# --------------------------------------------------------------------------
# Per-runtime endpoint strategy (egress-sidecar-linux design.md Decision 1)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Strategy:
    """Where the sidecar publishes and how its own network is set up."""

    kind: str = LOOPBACK
    address: str = PUBLISH_ADDRESS  # the one host address the port is published on

    def describe(self) -> str:
        return {
            LOOPBACK: "host loopback",
            DOCKER_GATEWAY: f"Docker's default bridge gateway {self.address}",
            PODMAN_PASTA: "host loopback, sidecar on a pasta network",
        }[self.kind]


def _docker_is_rootless(info: dict) -> bool:
    options = info.get("SecurityOptions") or info.get("securityOptions") or []
    if not isinstance(options, list):
        return False
    return any("name=rootless" in str(option) for option in options)


def rootless_docker_message() -> str:
    return (
        "--egress sidecar does not support rootless Docker: its network namespace "
        "cannot reach a sidecar on host loopback, and the setup is untested. "
        "Sidecar mode works on Apple container, Docker Desktop, Docker Engine "
        "(rootful), Podman machine and rootless Podman; use --egress firewall here."
    )


def endpoint_strategy(
    runtime: Runtime,
    *,
    info: Callable[[Runtime], dict] | None = None,
    gateway: Callable[[Runtime], str] | None = None,
) -> Strategy:
    """Select the endpoint strategy for ``runtime`` from read-only probes.

    Raises :class:`SidecarError` for rootless Docker or a failing probe.
    """
    if runtime.name == APPLE_CONTAINER.name:
        return Strategy()
    probe = info or local_service_network._runtime_info
    try:
        data = probe(runtime)
        if runtime.name == DOCKER.name:
            if local_service_network._docker_is_desktop(data):
                return Strategy()
            if _docker_is_rootless(data):
                raise SidecarError(rootless_docker_message())
            address = (gateway or local_service_network._bridge_gateway)(runtime)
            local_service_network._validate_endpoint(address)
            return Strategy(DOCKER_GATEWAY, address)
        if (
            runtime.name == PODMAN.name
            and local_service_network._podman_uses_local_pasta(data)
        ):
            return Strategy(PODMAN_PASTA)
    except SystemExit as exc:
        raise SidecarError(str(exc)) from None
    # Podman machine, rootless Podman with slirp4netns: firewall mode's
    # host-alias forwarding reaches a loopback publish.
    return Strategy()


def recorded_strategy(state: dict | None) -> Strategy:
    """The strategy a running sidecar was started with (``state.json``)."""
    state = state or {}
    kind = state.get("strategy")
    return Strategy(
        kind if kind in (LOOPBACK, DOCKER_GATEWAY, PODMAN_PASTA) else LOOPBACK,
        str(state.get("address") or PUBLISH_ADDRESS),
    )


def sandbox_plan(
    strategy: Strategy, port: int
) -> local_service_network.ForwardingPlan | None:
    """The sandbox's plan for the sidecar endpoint when it needs no bridge forwarder.

    On Docker Engine the sidecar is published on the gateway, so every sandbox
    reaches it at ``host.docker.internal`` mapped to the gateway, and no
    per-session listener exists for that port (design.md Decision 1).
    """
    if strategy.kind != DOCKER_GATEWAY:
        return None
    return local_service_network.ForwardingPlan(
        "docker-gateway-published",
        endpoint=strategy.address,
        add_host=f"{local_service_network.HOSTNAME}:{strategy.address}",
        port=port,
        label="Egress sidecar",
    )


# --------------------------------------------------------------------------
# Desired state and fingerprints
# --------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fingerprint(inputs: dict) -> str:
    return _sha256(json.dumps(inputs, sort_keys=True).encode())[:16]


def restart_inputs(
    runtime: Runtime,
    policy: egress_policy.PolicyFile,
    *,
    image: str,
    address: str = PUBLISH_ADDRESS,
) -> dict:
    """What only a restart can change in a running sidecar.

    ``address`` is recorded only when it is not host loopback (Docker Engine's
    gateway), so loopback sidecars keep their fingerprint.
    """
    ca = policy.upstream_ca_bytes() if policy.upstream_url else b""
    extra = {} if address == PUBLISH_ADDRESS else {"address": address}
    return {
        "image": image,
        "copilot": policy.copilot,
        "upstream_url": policy.upstream_url,
        "upstream_ca_sha256": _sha256(ca) if ca else None,
        "port": policy.port,
        "memory": policy.memory,
        "cpus": policy.cpus,
        "log_options": list(LOG_OPTIONS.get(runtime.name, ())),
        **extra,
    }


def relay_mode(
    runtime: Runtime, upstream_url: str | None, strategy: Strategy | None = None
) -> str | None:
    """Which sidecar-owned host relay the parent proxy needs, if any.

    - Apple ``container`` reaches a remote parent only through the CONNECT relay.
    - Docker Engine reaches a parent on host loopback only through the parent
      forwarder on the bridge gateway (egress-sidecar-linux Decision 2).
    """
    if upstream_url is None:
        return None
    local = egress_policy._upstream_endpoint(upstream_url, path=None).local
    if runtime.name == APPLE_CONTAINER.name and not local:
        return RELAY_CONNECT
    if strategy is not None and strategy.kind == DOCKER_GATEWAY and local:
        return RELAY_FORWARD
    return None


def needs_relay(
    runtime: Runtime, upstream_url: str | None, strategy: Strategy | None = None
) -> bool:
    return relay_mode(runtime, upstream_url, strategy) is not None


def loopback_parent_port(upstream_url: str | None) -> int | None:
    """The parent's port when it listens on host loopback, else ``None``."""
    if upstream_url is None:
        return None
    parsed = egress_policy._upstream_endpoint(upstream_url, path=None)
    return parsed.port if parsed.local else None


def upstream_for(
    runtime: Runtime,
    upstream_url: str | None,
    *,
    ca_file: bool,
    relay_port: int | None,
    strategy: Strategy | None = None,
) -> squid_config.Upstream | None:
    if upstream_url is None:
        return None
    parsed = egress_policy._upstream_endpoint(upstream_url, path=None)
    return squid_config.upstream_for(
        parsed,
        relay_port=(
            relay_port if needs_relay(runtime, upstream_url, strategy) else None
        ),
        ca_file=ca_file,
    )


def render_squid_conf(
    policy: egress_policy.PolicyFile,
    *,
    copilot: bool,
    upstream: squid_config.Upstream | None,
) -> str:
    """``squid.conf`` from ``policy``'s allowlist, with ``copilot`` and ``upstream``.

    For the reload fingerprint both come from the *running* sidecar, so a change
    to them (restart-only) never shows up as a reloadable difference.
    """
    effective = egress_policy.effective(dataclasses.replace(policy, copilot=copilot))
    return squid_config.render(effective, upstream=upstream)


def reload_fingerprint(squid_conf: str) -> str:
    return _sha256(squid_conf.encode())[:16]


def _running_upstream(state: dict) -> squid_config.Upstream | None:
    value = state.get("upstream")
    return squid_config.Upstream(**value) if isinstance(value, dict) else None


def restart_drift(state: dict | None, desired: dict, label: str | None) -> list[str]:
    """Human names of the restart-only fields that differ from the running sidecar."""
    if label == fingerprint(desired):
        return []
    running = (state or {}).get("restart_inputs") or {}
    fields = [RESTART_FIELDS[k] for k in desired if running.get(k) != desired[k]]
    return fields or ["configuration"]


# --------------------------------------------------------------------------
# Argv (pure)
# --------------------------------------------------------------------------


def sidecar_run_argv(
    runtime: Runtime,
    *,
    image: str,
    sidecar_dir: Path,
    port: int,
    memory: str,
    cpus: int,
    restart_fingerprint: str,
    strategy: Strategy | None = None,
    parent_port: int | None = None,
) -> list[str]:
    """Run the sidecar, publishing on exactly one host address.

    ``parent_port`` is a parent proxy's port on host loopback: on pasta it is
    forwarded to host loopback (``-T``); on Docker Engine the sidecar maps
    ``host.docker.internal`` to the gateway, where the parent forwarder listens.
    """
    strategy = strategy or Strategy()
    mount = MountSpec(sidecar_dir, INPUT_MOUNT, readonly=True)
    network: list[str] = []
    if strategy.kind == PODMAN_PASTA:
        plans = (
            [
                local_service_network.ForwardingPlan(
                    local_service_network.PASTA_STRATEGY, port=parent_port
                )
            ]
            if parent_port is not None
            else []
        )
        value = local_service_network.network_argument(plans, pasta=True)
        assert value is not None
        network = [
            "--network",
            value,
            "--add-host",
            f"{local_service_network.HOSTNAME}:127.0.0.1",
        ]
    elif strategy.kind == DOCKER_GATEWAY and parent_port is not None:
        network = [
            "--add-host",
            f"{local_service_network.HOSTNAME}:{strategy.address}",
        ]
    argv = [
        runtime.binary,
        "run",
        "-d",
        "--name",
        CONTAINER,
        "--label",
        f"{ROLE_LABEL}=sidecar",
        "--label",
        f"{FINGERPRINT_LABEL}={restart_fingerprint}",
        "--label",
        f"{PORT_LABEL}={port}",
        "--label",
        f"{ADDRESS_LABEL}={strategy.address}",
        *network,
        "--publish",
        f"{strategy.address}:{port}:{CONTAINER_PORT}",
        "--memory",
        memory,
        "--cpus",
        str(cpus),
        "--tmpfs",
        "/run",
        "--mount",
        _mount_arg(mount),
    ]
    for option in LOG_OPTIONS.get(runtime.name, ()):
        argv += ["--log-opt", option]
    return [*argv, image]


def container_inspect_argv(runtime: Runtime) -> list[str]:
    return [runtime.binary, "inspect", CONTAINER]


def exec_argv(runtime: Runtime, *command: str) -> list[str]:
    return [runtime.binary, "exec", CONTAINER, *command]


def readiness_argv(runtime: Runtime) -> list[str]:
    request = (
        f"GET http://{READINESS_HOST}/ HTTP/1.0\\r\\nHost: {READINESS_HOST}\\r\\n\\r\\n"
    )
    # BusyBox nc closes the connection when stdin ends, before the reply is
    # read; keep stdin open briefly so the denial page comes back.
    script = f"{{ printf '{request}'; sleep 1; }} | nc -w 3 127.0.0.1 {CONTAINER_PORT}"
    return exec_argv(runtime, "sh", "-c", script)


def reload_argv(runtime: Runtime) -> list[str]:
    return exec_argv(runtime, "sbx-reload")


def clock_argv(runtime: Runtime) -> list[str]:
    return exec_argv(runtime, "date", "+%s")


def logs_argv(
    runtime: Runtime, *, since: float | None = None, follow: bool = False
) -> list[str]:
    argv = [runtime.binary, "logs"]
    if follow:
        argv.append("--follow")
    if since is not None and runtime.name != APPLE_CONTAINER.name:
        # Apple container logs has no --since (spike 0.4); filtered by %ts.
        argv += ["--since", str(int(since))]
    return [*argv, CONTAINER]


def remove_container_argv(runtime: Runtime) -> list[str]:
    return [runtime.binary, "rm", "-f", CONTAINER]


def remove_legacy_network_argv(runtime: Runtime) -> list[str]:
    return [runtime.binary, "network", "rm", LEGACY_NETWORK]


# --------------------------------------------------------------------------
# Inspect output parsing (pure)
# --------------------------------------------------------------------------


def _first(data: object) -> dict:
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    if isinstance(data, dict):
        return data
    raise SidecarError("unexpected inspect output")


@dataclass(frozen=True)
class ContainerInfo:
    running: bool
    labels: dict[str, str]
    raw: str
    ip: str | None = None  # the container's address on Docker's default bridge

    @property
    def port(self) -> int | None:
        """The host port the container was published on (its label)."""
        value = self.labels.get(PORT_LABEL, "")
        return int(value) if value.isdigit() else None

    @property
    def address(self) -> str:
        """The host address the container was published on (its label)."""
        return self.labels.get(ADDRESS_LABEL) or PUBLISH_ADDRESS


def parse_container(runtime: Runtime, text: str) -> ContainerInfo:
    try:
        record = _first(json.loads(text))
    except ValueError as exc:
        raise SidecarError("could not parse the sidecar's inspect output") from exc
    if runtime.name == APPLE_CONTAINER.name:
        status = record.get("status") or {}
        running = isinstance(status, dict) and status.get("state") == "running"
        labels = _labels((record.get("configuration") or {}).get("labels"))
    else:
        state = record.get("State") or {}
        running = isinstance(state, dict) and state.get("Running") is True
        labels = _labels((record.get("Config") or {}).get("Labels"))
        ip = _bridge_ip(record.get("NetworkSettings"))
        return ContainerInfo(running=running, labels=labels, raw=text, ip=ip)
    return ContainerInfo(running=running, labels=labels, raw=text)


def _bridge_ip(settings: object) -> str | None:
    if not isinstance(settings, dict):
        return None
    networks = settings.get("Networks")
    if isinstance(networks, dict) and isinstance(networks.get("bridge"), dict):
        value = networks["bridge"].get("IPAddress")
        if value:
            return str(value)
    value = settings.get("IPAddress")
    return str(value) if value else None


def inspect(runtime: Runtime, runner: Runner) -> ContainerInfo | None:
    """The sidecar container, or ``None`` when it does not exist."""
    result = runner(container_inspect_argv(runtime))
    text = (result.stdout or "").strip()
    # Apple container answers an empty list for an unknown id.
    if result.returncode != 0 or text in ("", "[]", "{}"):
        return None
    try:
        return parse_container(runtime, result.stdout)
    except SidecarError:
        return None


def _labels(value: object) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    if isinstance(value, str):  # Docker's "k=v,k2=v2"
        pairs = (item.split("=", 1) for item in value.split(",") if "=" in item)
        return {k: v for k, v in pairs}
    return {}


# --------------------------------------------------------------------------
# Runtime and host helpers
# --------------------------------------------------------------------------


def _checked(runner: Runner, argv: Sequence[str], what: str) -> str:
    result = runner(argv)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()[-5:]
        raise SidecarError(f"{what} failed: " + " | ".join(detail))
    return result.stdout or ""


def probe_ready(runtime: Runtime, runner: Runner) -> bool:
    return READINESS_MARKER in (runner(readiness_argv(runtime)).stdout or "")


def port_free(port: int, address: str = PUBLISH_ADDRESS) -> bool:
    """Whether nothing listens on ``address:port`` (or the wildcard) on the host.

    ``down`` with sandboxes still connected leaves the sidecar's side of those
    connections in TIME_WAIT for up to a minute. A plain bind fails on them,
    though the runtime can publish the port again, so bind with
    ``SO_REUSEADDR``: that still fails on a listener bound to loopback. On
    macOS it succeeds next to a wildcard listener, which a loopback connect
    then finds.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((address, port))
        except OSError:
            return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        return probe.connect_ex((address, port)) != 0


def probe_published(
    port: int, address: str = PUBLISH_ADDRESS, *, timeout: float = 3.0
) -> bool:
    """Whether the sidecar answers its readiness request on the host endpoint."""
    request = (
        f"GET http://{READINESS_HOST}/ HTTP/1.0\r\nHost: {READINESS_HOST}\r\n\r\n"
    ).encode()
    try:
        with socket.create_connection((address, port), timeout=timeout) as sock:
            sock.sendall(request)
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
    except OSError:
        return False
    return READINESS_MARKER in b"".join(chunks).decode("utf-8", "replace")


def _wait(
    check: Callable[[], bool],
    *,
    timeout: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    interval: float = 0.5,
) -> bool:
    deadline = clock() + timeout
    while True:
        if check():
            return True
        if clock() >= deadline:
            return False
        sleep(interval)


def wait_ready(
    runtime: Runtime,
    runner: Runner,
    *,
    timeout: float = READINESS_TIMEOUT,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    if not _wait(
        lambda: probe_ready(runtime, runner), timeout=timeout, clock=clock, sleep=sleep
    ):
        raise SidecarError(
            f"the egress sidecar was not ready within {timeout:.0f}s.\n"
            + log_tail(runtime, runner)
        )


def log_tail(runtime: Runtime, runner: Runner, lines: int = 20) -> str:
    result = runner(logs_argv(runtime))
    text = (result.stdout or "") + (result.stderr or "")
    tail = text.strip().splitlines()[-lines:]
    return "Sidecar log (last lines):\n" + "\n".join(f"  {line}" for line in tail)


def sidecar_clock(runtime: Runtime, runner: Runner) -> float | None:
    """Epoch seconds on the sidecar's clock (VM clocks drift after sleep)."""
    result = runner(clock_argv(runtime))
    try:
        return float((result.stdout or "").strip()) if result.returncode == 0 else None
    except ValueError:
        return None


def fetch_logs(runtime: Runtime, runner: Runner, *, since: float | None = None) -> str:
    # Docker/Podman stamp lines on the daemon's clock; widen the window and let
    # the caller filter precisely by %ts.
    margin = None if since is None else since - 60
    result = runner(logs_argv(runtime, since=margin))
    return (result.stdout or "") + (result.stderr or "")


def ensure_image(
    runtime: Runtime,
    *,
    build: Callable[..., int],
    exists: Callable[[Runtime, str], bool],
    root: Path | None = None,
) -> str:
    """Render the sidecar build context and build it unless the tag exists."""
    context, tag = sidecar_image.render(root)
    if (
        not exists(runtime, tag)
        and build(runtime=runtime, context_dir=context, image_tag=tag) != 0
    ):
        raise SidecarError(f"building the egress sidecar image {tag} failed")
    return tag


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


@dataclass
class Sidecar:
    """The running sidecar as a launch sees it after :func:`ensure`."""

    runtime: Runtime
    layout: Layout
    state: dict
    ca: session_ca.SessionCA | None
    started: bool
    messages: list[str] = field(default_factory=list)

    @property
    def port(self) -> int:
        return int(self.state["port"])

    @property
    def address(self) -> str:
        return str(self.state.get("address") or PUBLISH_ADDRESS)

    @property
    def strategy(self) -> Strategy:
        return recorded_strategy(self.state)

    @property
    def endpoint(self) -> str:
        return endpoint(self.port, self.address)

    @property
    def copilot(self) -> bool:
        return bool(self.state["copilot"])

    @property
    def interception(self) -> bool:
        return self.ca is not None and self.ca.interception

    @property
    def upstream_url(self) -> str | None:
        return self.state.get("upstream_url")

    @property
    def upstream_ca(self) -> Path | None:
        path = self.layout.sidecar / UPSTREAM_CA_NAME
        return path if path.is_file() else None


@dataclass
class Collaborators:
    """Everything :func:`ensure` and friends call out to (injectable in tests)."""

    runner: Runner = default_runner
    relay: egress_relay.RelayLike = field(default_factory=egress_relay.RelayControl)
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC)
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    readiness_timeout: float = READINESS_TIMEOUT
    port_free: Callable[[int, str], bool] = port_free
    port_release_timeout: float = PORT_RELEASE_TIMEOUT
    host_probe: Callable[[int, str], bool] = probe_published
    strategy: Callable[[Runtime], Strategy] = endpoint_strategy


@contextlib.contextmanager
def _locked(lay: Layout):
    mount_storage.ensure_private_dirs(lay.root)
    os.chmod(lay.root, 0o700)
    with path_lock(LOCK_KIND, lay.root):
        yield


def address_drift_message(running: str, current: str, port: int) -> str:
    return (
        f"The running egress sidecar publishes {endpoint(port, running)}, but "
        f"Docker's default bridge gateway is now {current}. This sandbox reaches "
        "the sidecar through the current gateway and could not reach it. Run "
        "`project-sandbox-egress restart` to republish it (it interrupts every "
        "running sidecar-mode sandbox)."
    )


def port_drift_message(
    running: int, configured: int, address: str = PUBLISH_ADDRESS
) -> str:
    return (
        f"The running egress sidecar publishes {endpoint(running, address)}, but egress.toml "
        f"sets [egress.sidecar] port = {configured}. This sandbox is built for the "
        "configured port and could not reach the sidecar. Run "
        "`project-sandbox-egress restart` to apply the new port (it interrupts "
        "every running sidecar-mode sandbox)."
    )


def ensure(
    runtime: Runtime,
    policy: egress_policy.PolicyFile,
    *,
    image: str,
    root: Path | None = None,
    deps: Collaborators | None = None,
    expected_port: int | None = None,
    strategy: Strategy | None = None,
) -> Sidecar:
    """Reuse the running, ready sidecar or start it; apply live changes.

    Serialized with every other launch and ``project-sandbox-egress`` command,
    so concurrent first launches create exactly one sidecar. ``expected_port``
    is the port a launch rendered into its sandbox; a running sidecar that
    publishes another one, or on another address than ``strategy`` (probed when
    not given) requires, is an error naming ``project-sandbox-egress restart``.
    Reread policy under the global policy lock, then acquire the runtime lock;
    hold both through application so a delayed launch cannot undo a revocation.
    """
    deps = deps or Collaborators()
    strategy = strategy or deps.strategy(runtime)
    lay = layout(runtime, root)
    with egress_policy.current_policy(policy) as current, _locked(lay):
        sidecar = _ensure_locked(
            runtime, current, image=image, lay=lay, deps=deps, strategy=strategy
        )
    if expected_port is not None and sidecar.port != expected_port:
        raise SidecarError(
            port_drift_message(sidecar.port, expected_port, sidecar.address)
        )
    if expected_port is not None and sidecar.address != strategy.address:
        raise SidecarError(
            address_drift_message(sidecar.address, strategy.address, sidecar.port)
        )
    return sidecar


def _healthy(
    runtime: Runtime,
    lay: Layout,
    state: dict | None,
    info: ContainerInfo | None,
    runner: Runner,
) -> bool:
    """Running, ready, and matching our state (a leftover container does not)."""
    return (
        info is not None
        and info.running
        and state is not None
        and info.labels.get(ROLE_LABEL) == "sidecar"
        and info.port == state["port"]
        and info.address == (state.get("address") or PUBLISH_ADDRESS)
        and _complete(lay)
        and probe_ready(runtime, runner)
    )


def _ensure_locked(
    runtime: Runtime,
    policy: egress_policy.PolicyFile,
    *,
    image: str,
    lay: Layout,
    deps: Collaborators,
    strategy: Strategy,
    new_ca: bool = False,
) -> Sidecar:
    messages: list[str] = []
    state = read_state(lay)
    info = inspect(runtime, deps.runner)
    if _healthy(runtime, lay, state, info, deps.runner):
        assert state is not None and info is not None
        state = _reconcile(
            runtime, policy, image=image, lay=lay, state=state, info=info,
            deps=deps, messages=messages, strategy=strategy,
        )  # fmt: skip
        started = False
    else:
        state = _cold_start(
            runtime, policy, image=image, lay=lay, deps=deps,
            previous=state, new_ca=new_ca, strategy=strategy,
        )  # fmt: skip
        started = True
    return Sidecar(
        runtime=runtime,
        layout=lay,
        state=state,
        ca=session_ca.load(lay.sidecar),
        started=started,
        messages=messages,
    )


def port_taken_message(
    policy: egress_policy.PolicyFile, address: str = PUBLISH_ADDRESS
) -> str:
    where = policy.path or egress_policy.default_policy_path()
    return (
        f"{endpoint(policy.port, address)} is already in use on the host, so the egress "
        "sidecar cannot publish its proxy there. Stop the process using it, or "
        f"choose another port with [egress.sidecar] port in {where}."
    )


def _cold_start(
    runtime: Runtime,
    policy: egress_policy.PolicyFile,
    *,
    image: str,
    lay: Layout,
    deps: Collaborators,
    previous: dict | None,
    new_ca: bool,
    strategy: Strategy,
) -> dict:
    runner = deps.runner
    address = strategy.address
    try:
        desired = restart_inputs(runtime, policy, image=image, address=address)
        upstream_ca = policy.upstream_ca_bytes() if policy.upstream_url else b""
        mode = relay_mode(runtime, policy.upstream_url, strategy)
        parent_port = loopback_parent_port(policy.upstream_url)
    except egress_policy.EgressPolicyError as exc:
        raise SidecarError(str(exc)) from None

    # Also clears a stopped one (after a reboot) or a leftover one whose state
    # does not match; its published port is released before the check below.
    runner(remove_container_argv(runtime))
    if mode == RELAY_FORWARD:
        # The sidecar's address may change, so the forwarder always restarts.
        _stop_relay(lay, deps.relay)
    if not _wait(
        lambda: deps.port_free(policy.port, address),
        timeout=deps.port_release_timeout,
        clock=deps.clock,
        sleep=deps.sleep,
    ):
        raise SidecarError(port_taken_message(policy, address))

    mount_storage.ensure_private_dirs(lay.sidecar)
    os.chmod(lay.sidecar, 0o700)
    bump_set = egress_policy.derive_bump_set(copilot=policy.copilot)
    now = deps.now()
    ca = None if new_ca else session_ca.load(lay.sidecar)
    if not session_ca.reusable(ca, bump_set, now=now):
        session_ca.remove(lay.sidecar)
        session_ca.generate(
            lay.sidecar,
            bump_set,
            name=session_ca.common_name(runtime.name),
            lifetime=dt.timedelta(days=policy.ca_lifetime_days),
            now=now,
        )
    upstream_ca_path = lay.sidecar / UPSTREAM_CA_NAME
    if upstream_ca:
        _write_file(upstream_ca_path, upstream_ca)
    else:
        upstream_ca_path.unlink(missing_ok=True)

    relay_port = None
    if mode == RELAY_CONNECT:
        assert policy.upstream_url is not None
        recorded = (previous or {}).get("relay_port")
        relay_port = _ensure_relay(
            lay,
            deps.relay,
            upstream_url=policy.upstream_url,
            port=recorded
            or egress_relay.preferred_port(
                egress_policy._upstream_endpoint(policy.upstream_url, path=None)
            ),
            ca_file=upstream_ca_path if upstream_ca else None,
        )
    elif mode == RELAY_FORWARD:
        # Bound only after the sidecar runs (it needs the sidecar's address);
        # Squid connects to its parent lazily, so a placeholder port is safe
        # until the reload below.
        assert parent_port is not None
        relay_port = (previous or {}).get("relay_port") or parent_port
    else:
        _stop_relay(lay, deps.relay)

    def render(port: int | None) -> tuple[str, squid_config.Upstream | None]:
        upstream = upstream_for(
            runtime,
            policy.upstream_url,
            ca_file=bool(upstream_ca),
            relay_port=port,
            strategy=strategy,
        )
        return render_squid_conf(
            policy, copilot=policy.copilot, upstream=upstream
        ), upstream

    conf, upstream = render(relay_port)
    _write_file(lay.sidecar / CONF_NAME, conf)

    restart_fp = fingerprint(desired)
    _checked(
        runner,
        sidecar_run_argv(
            runtime,
            image=image,
            sidecar_dir=lay.sidecar,
            port=policy.port,
            memory=policy.memory,
            cpus=policy.cpus,
            restart_fingerprint=restart_fp,
            strategy=strategy,
            parent_port=parent_port,
        ),
        "starting the egress sidecar",
    )
    wait_ready(
        runtime,
        runner,
        timeout=deps.readiness_timeout,
        clock=deps.clock,
        sleep=deps.sleep,
    )
    sidecar_address = None
    if mode == RELAY_FORWARD:
        assert parent_port is not None
        sidecar_address = _sidecar_ip(runtime, runner)
        bound = _spawn_forwarder(
            lay,
            deps.relay,
            bind=address,
            peer=sidecar_address,
            target_port=parent_port,
            port=0,
        )
        if bound != relay_port:
            relay_port = bound
            conf, upstream = render(relay_port)
            _write_file(lay.sidecar / CONF_NAME, conf)
            _checked(runner, reload_argv(runtime), "reloading the egress sidecar")
    if not _wait(
        lambda: deps.host_probe(policy.port, address),
        timeout=deps.readiness_timeout,
        clock=deps.clock,
        sleep=deps.sleep,
    ):
        raise SidecarError(
            "the egress sidecar is ready inside its container but does not answer "
            f"on {endpoint(policy.port, address)} on the host.\n"
            + log_tail(runtime, runner)
        )
    state = {
        "version": STATE_VERSION,
        "image": image,
        "restart_fingerprint": restart_fp,
        "restart_inputs": desired,
        "reload_fingerprint": reload_fingerprint(conf),
        "copilot": policy.copilot,
        "upstream_url": policy.upstream_url,
        "upstream": dataclasses.asdict(upstream) if upstream else None,
        "relay_port": relay_port,
        "port": policy.port,
        "started_at": now.timestamp(),
        "strategy": strategy.kind,
        "address": address,
        "sidecar_address": sidecar_address,
    }
    write_state(lay, state)
    return state


def _sidecar_ip(runtime: Runtime, runner: Runner) -> str:
    info = inspect(runtime, runner)
    if info is None or not info.ip:
        raise SidecarError(
            "could not read the egress sidecar's address on Docker's default "
            "bridge, which the parent forwarder admits as its only peer"
        )
    return info.ip


def _stop_relay(lay: Layout, relay: egress_relay.RelayLike) -> None:
    try:
        relay.stop(lay.root)
    except egress_relay.RelayError as exc:
        raise SidecarError(str(exc)) from None


def _spawn_forwarder(
    lay: Layout,
    relay: egress_relay.RelayLike,
    *,
    bind: str,
    peer: str,
    target_port: int,
    port: int,
) -> int:
    try:
        return relay.spawn_forward(
            lay.root, bind=bind, peer=peer, target_port=target_port, port=port
        )
    except egress_relay.RelayError as exc:
        raise SidecarError(str(exc)) from None


def _ensure_relay(
    lay: Layout,
    relay: egress_relay.RelayLike,
    *,
    upstream_url: str,
    port: int,
    ca_file: Path | None,
) -> int:
    live = relay.port(lay.root)
    if live is not None:
        return live
    try:
        return relay.spawn(lay.root, upstream=upstream_url, port=port, ca_file=ca_file)
    except egress_relay.RelayError as exc:
        raise SidecarError(str(exc)) from None


def _reconcile(
    runtime: Runtime,
    policy: egress_policy.PolicyFile,
    *,
    image: str,
    lay: Layout,
    state: dict,
    info: ContainerInfo,
    deps: Collaborators,
    messages: list[str],
    strategy: Strategy | None = None,
) -> dict:
    """Apply live changes to a running sidecar; warn about restart-only ones.

    ``strategy`` is the runtime's current one (address drift is restart-only);
    the relay follows the strategy the sidecar was started with.
    """
    runner = deps.runner
    state = dict(state)
    upstream = _running_upstream(state)
    running = recorded_strategy(state)
    mode = relay_mode(runtime, state["upstream_url"], running)

    # Relay: respawn on the recorded port; follow a move with a reload.
    if mode is not None and upstream is not None:
        ca_path = lay.sidecar / UPSTREAM_CA_NAME
        port = state["relay_port"] or upstream.port
        bound = None
        if mode == RELAY_CONNECT and deps.relay.port(lay.root) is None:
            bound = _ensure_relay(
                lay,
                deps.relay,
                upstream_url=state["upstream_url"],
                port=port,
                ca_file=ca_path if ca_path.is_file() else None,
            )
            messages.append(f"Restarted the egress relay on 127.0.0.1:{bound}.")
        elif mode == RELAY_FORWARD:
            peer = info.ip or state.get("sidecar_address")
            live = deps.relay.port(lay.root)
            if live is None or peer != state.get("sidecar_address"):
                parent = loopback_parent_port(state["upstream_url"])
                assert parent is not None
                if not peer:
                    raise SidecarError(
                        "the egress sidecar has no address on Docker's default "
                        "bridge; run `project-sandbox-egress restart`"
                    )
                _stop_relay(lay, deps.relay)
                bound = _spawn_forwarder(
                    lay,
                    deps.relay,
                    bind=running.address,
                    peer=peer,
                    target_port=parent,
                    port=port,
                )
                state["sidecar_address"] = peer
                messages.append(
                    f"Restarted the egress parent forwarder on "
                    f"{running.address}:{bound}."
                )
        if bound is not None and bound != port:
            messages.append(
                f"The relay moved from port {port} to {bound}; the sidecar "
                "follows it without a restart."
            )
            upstream = dataclasses.replace(upstream, port=bound)
            state["relay_port"] = bound
            state["upstream"] = dataclasses.asdict(upstream)

    # Allowlist (reload fingerprint, rendered against the running upstream and
    # Copilot setting so restart-only changes never show up here).
    conf = render_squid_conf(policy, copilot=state["copilot"], upstream=upstream)
    fp = reload_fingerprint(conf)
    if fp != state["reload_fingerprint"]:
        _write_file(lay.sidecar / CONF_NAME, conf)
        _checked(runner, reload_argv(runtime), "reloading the egress sidecar")
        # `squid -k reconfigure` returns before Squid has reopened its
        # listening port; a launch probing right away saw "does not answer".
        wait_ready(
            runtime,
            runner,
            timeout=deps.readiness_timeout,
            clock=deps.clock,
            sleep=deps.sleep,
        )
        state["reload_fingerprint"] = fp
        messages.append(
            f"Applied the egress allowlist from {policy.path or 'built-in defaults'} "
            "to the running sidecar (reloaded, no restart)."
        )

    # Restart-only drift: warn, keep using the running sidecar.
    try:
        desired = restart_inputs(
            runtime, policy, image=image, address=(strategy or running).address
        )
    except egress_policy.EgressPolicyError as exc:
        raise SidecarError(str(exc)) from None
    fields = restart_drift(state, desired, info.labels.get(FINGERPRINT_LABEL))
    if fields:
        messages.append(
            "[W] The running egress sidecar differs from egress.toml or the "
            f"installed version in: {', '.join(fields)}. {RESTART_HINT}"
        )
    write_state(lay, state)
    return state


def down(
    runtime: Runtime,
    *,
    root: Path | None = None,
    deps: Collaborators | None = None,
) -> bool:
    """Remove the sidecar, the relay and the CA; return whether a sidecar existed.

    No use tracking: running sandboxes lose egress. A leftover ``sbx-egress``
    network of the squid-sidecar line is removed on a best-effort basis.
    """
    deps = deps or Collaborators()
    lay = layout(runtime, root)
    with _locked(lay):
        existed = inspect(runtime, deps.runner) is not None
        _down_locked(runtime, lay, deps, keep_ca=False)
        deps.runner(remove_legacy_network_argv(runtime))  # failure is fine
        return existed


def _down_locked(
    runtime: Runtime, lay: Layout, deps: Collaborators, *, keep_ca: bool
) -> None:
    _stop_relay(lay, deps.relay)
    deps.runner(remove_container_argv(runtime))
    if keep_ca:
        for name in (CONF_NAME, UPSTREAM_CA_NAME):
            (lay.sidecar / name).unlink(missing_ok=True)
    else:
        shutil.rmtree(lay.sidecar, ignore_errors=True)
    lay.state_file.unlink(missing_ok=True)


def restart(
    runtime: Runtime,
    policy: egress_policy.PolicyFile,
    *,
    image: str,
    new_ca: bool = False,
    root: Path | None = None,
    deps: Collaborators | None = None,
) -> Sidecar:
    """``down`` without deleting a reusable CA, then ``up``."""
    deps = deps or Collaborators()
    strategy = deps.strategy(runtime)
    lay = layout(runtime, root)
    with egress_policy.current_policy(policy) as current, _locked(lay):
        _down_locked(runtime, lay, deps, keep_ca=not new_ca)
        return _ensure_locked(
            runtime,
            current,
            image=image,
            lay=lay,
            deps=deps,
            strategy=strategy,
            new_ca=new_ca,
        )


def reload(
    runtime: Runtime,
    policy: egress_policy.PolicyFile,
    *,
    image: str,
    root: Path | None = None,
    deps: Collaborators | None = None,
) -> list[str]:
    """Apply ``egress.toml``'s allowlist to the running sidecar."""
    deps = deps or Collaborators()
    lay = layout(runtime, root)
    with egress_policy.current_policy(policy) as current, _locked(lay):
        state = read_state(lay)
        info = inspect(runtime, deps.runner)
        if info is None or not info.running or state is None or not _complete(lay):
            raise SidecarError(
                "the egress sidecar is not running; start it with "
                "`project-sandbox-egress up` (changes in egress.toml apply then)"
            )
        messages: list[str] = []
        _reconcile(
            runtime, current, image=image, lay=lay, state=state, info=info,
            deps=deps, messages=messages,
        )  # fmt: skip
        return messages


# --------------------------------------------------------------------------
# Read-only views: status and dry-run (no lock, no writes)
# --------------------------------------------------------------------------


def status(
    runtime: Runtime,
    policy: egress_policy.PolicyFile,
    *,
    image: str,
    root: Path | None = None,
    deps: Collaborators | None = None,
    probe: bool = True,
    strategy: Strategy | None = None,
) -> dict:
    """Describe the sidecar without creating, starting, changing or removing it.

    ``strategy`` is the runtime's current endpoint strategy; when not given it
    is probed (read-only) if ``probe``, and taken from ``state.json`` otherwise.
    """
    deps = deps or Collaborators()
    lay = layout(runtime, root)
    state = read_state(lay)
    strategy_error = None
    if strategy is None and probe:
        try:
            strategy = deps.strategy(runtime)
        except SidecarError as exc:
            strategy_error = str(exc)
    current = strategy or recorded_strategy(state)
    info = inspect(runtime, deps.runner)
    running = info is not None and info.running
    ready = running and probe and probe_ready(runtime, deps.runner)
    healthy = (
        running
        and bool(ready)
        and state is not None
        and info is not None
        and info.port == state["port"]
        and info.address == current.address
    )
    report: dict = {
        "runtime": runtime.name,
        "container": CONTAINER,
        "policy_path": str(policy.path or egress_policy.default_policy_path()),
        "policy_exists": policy.path is not None,
        "exists": info is not None,
        "running": running,
        "ready": bool(ready),
        "state_dir": str(lay.root),
        "configured_endpoint": endpoint(policy.port, current.address),
        "configured_address": current.address,
        "strategy": current.kind,
        "would": "reuse" if healthy else "start",
    }
    if strategy_error:
        report["strategy_error"] = strategy_error
    ca = session_ca.load(lay.sidecar)
    now = deps.now()
    if ca is not None:
        report["ca"] = {
            "name_constraints": list(ca.name_constraints),
            "interception": ca.interception,
            "not_after": ca.not_after.isoformat(),
            "expired": ca.not_after <= now,
            "expires_soon": session_ca.remaining(ca, now=now)
            < session_ca.MIN_REMAINING,
        }
    try:
        mode = relay_mode(
            runtime,
            (state or {}).get("upstream_url") or policy.upstream_url,
            recorded_strategy(state) if state is not None else current,
        )
    except egress_policy.EgressPolicyError:
        mode = None
    report["relay"] = {
        "needed": mode is not None,
        "mode": mode,
        "address": (
            recorded_strategy(state).address
            if mode == RELAY_FORWARD and state is not None
            else (current.address if mode == RELAY_FORWARD else PUBLISH_ADDRESS)
        ),
        "peer": (state or {}).get("sidecar_address"),
        "live": deps.relay.is_live(lay.root),
        "port": deps.relay.port(lay.root),
    }
    drift: dict = {"reload": False, "restart": []}
    if state is not None and running:
        assert info is not None
        report["endpoint"] = endpoint(info.port or state["port"], info.address)
        report["address"] = info.address
        report["image"] = state["image"]
        report["image_current"] = state["image"] == image
        report["copilot"] = state["copilot"]
        report["upstream_url"] = state["upstream_url"]
        report["started_at"] = state["started_at"]
        try:
            conf = render_squid_conf(
                policy, copilot=state["copilot"], upstream=_running_upstream(state)
            )
            drift["reload"] = reload_fingerprint(conf) != state["reload_fingerprint"]
            drift["restart"] = restart_drift(
                state,
                restart_inputs(runtime, policy, image=image, address=current.address),
                info.labels.get(FINGERPRINT_LABEL),
            )
        except egress_policy.EgressPolicyError as exc:
            drift["error"] = str(exc)
    report["drift"] = drift
    return report


def status_lines(report: dict) -> list[str]:
    lines = [f"Egress sidecar ({report['runtime']}): {CONTAINER}"]
    if not report["exists"]:
        lines.append("  State: not running")
    else:
        state = "running" if report["running"] else "stopped"
        if report["running"]:
            state += ", ready" if report["ready"] else ", NOT ready"
        lines.append(f"  State: {state}")
    if "endpoint" in report:
        lines.append(
            f"  Endpoint: {report['endpoint']} "
            f"({describe_address(report.get('address', PUBLISH_ADDRESS))})"
        )
        current = "current" if report["image_current"] else "STALE (restart to upgrade)"
        lines.append(f"  Image: {report['image']} ({current})")
        lines.append(f"  Copilot profile: {'on' if report['copilot'] else 'off'}")
        lines.append(f"  Upstream: {report['upstream_url'] or 'none (direct)'}")
    else:
        lines.append(
            f"  Endpoint: {report['configured_endpoint']} "
            f"({describe_address(report.get('configured_address', PUBLISH_ADDRESS))}, "
            "once started)"
        )
    if report.get("strategy_error"):
        lines.append(f"  Runtime: {report['strategy_error']}")
    ca = report.get("ca")
    if ca:
        expiry = "EXPIRED" if ca["expired"] else ca["not_after"]
        use = (
            "trusted by sandboxes"
            if ca["interception"]
            else "not trusted (nothing intercepted)"
        )
        lines.append(
            f"  CA: constrained to {', '.join(ca['name_constraints'])}; expires "
            f"{expiry}; {use}"
        )
    relay = report["relay"]
    if relay["needed"] or relay["live"]:
        address = relay.get("address") or PUBLISH_ADDRESS
        where = f"{address}:{relay['port']}" if relay["port"] else "not running"
        if relay.get("mode") == RELAY_FORWARD:
            peer = relay.get("peer")
            only = f", accepts only the sidecar at {peer}" if peer else ""
            lines.append(f"  Parent forwarder: {where}{only}")
        else:
            lines.append(f"  Relay: {where}")
    drift = report["drift"]
    if drift.get("reload"):
        lines.append(
            "  Drift: the allowlist in egress.toml differs; apply it with "
            "`project-sandbox-egress reload`"
        )
    if drift.get("restart"):
        lines.append(
            "  Drift (restart needed): " + ", ".join(drift["restart"]) + ". "
            "Apply with `project-sandbox-egress restart`."
        )
    if drift.get("error"):
        lines.append(f"  Policy error: {drift['error']}")
    exists = "" if report["policy_exists"] else " (missing: built-in defaults)"
    lines.append(f"  Policy: {report['policy_path']}{exists}")
    return lines


# --------------------------------------------------------------------------
# Per-session sandbox inputs (design.md Decision 8)
# --------------------------------------------------------------------------


def sandbox_mount(directory: Path) -> str:
    """``--mount`` value for the sandbox: the public inputs only, read-only."""
    return _mount_arg(MountSpec(directory, SANDBOX_MOUNT, readonly=True))


def write_sandbox_inputs(
    directory: Path,
    *,
    ca: session_ca.SessionCA | None,
    upstream_ca: Path | None = None,
) -> Path:
    """Write ``<session>/sandbox/{ca.pem,upstream-ca.pem}``.

    ``ca.pem`` is copied only when the sidecar intercepts TLS, ``upstream-ca.pem``
    only when an upstream CA is configured; the key is never placed here.
    """
    sandbox = directory / SANDBOX_SUBDIR
    sandbox.mkdir(mode=0o755)
    if ca is not None and ca.interception:
        (sandbox / session_ca.CERT_NAME).write_bytes(ca.cert_path.read_bytes())
    if upstream_ca is not None and upstream_ca.is_file():
        (sandbox / UPSTREAM_CA_NAME).write_bytes(upstream_ca.read_bytes())
    for path in sandbox.iterdir():
        path.chmod(0o644)
    return sandbox


@dataclass(frozen=True)
class CACheck:
    error: str | None = None
    warning: str | None = None


def check_ca(sidecar: Sidecar, *, now: dt.datetime) -> CACheck:
    """Warn when an intercepting CA expires within 2 days; refuse when expired.

    With nothing intercepted the CA is never trusted, so expiry is irrelevant.
    """
    ca = sidecar.ca
    if ca is None or not ca.interception:
        return CACheck()
    left = session_ca.remaining(ca, now=now)
    if left <= dt.timedelta(0):
        return CACheck(
            error=(
                f"the egress sidecar's CA expired at {ca.not_after.isoformat()}; "
                "sandboxes would reject the intercepted Copilot token exchange. "
                "Run `project-sandbox-egress restart` to rotate it."
            )
        )
    if left < session_ca.MIN_REMAINING:
        hours = int(left.total_seconds() // 3600)
        return CACheck(
            warning=(
                f"[W] The egress sidecar's CA expires in {hours} hours "
                f"({ca.not_after.isoformat()}). Run `project-sandbox-egress "
                "restart` to rotate it."
            )
        )
    return CACheck()
