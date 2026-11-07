"""The global egress sidecar of each runtime (egress-sidecar-macos).

One container ``project-sandbox-egress`` on the runtime's default network serves
every ``--egress sidecar`` sandbox of a runtime. It publishes Squid's port on
host loopback only (``127.0.0.1:<[egress.sidecar] port>``), and sandboxes reach
it like any other local service, through the firewall-mode local-service
forwarding (design.md Decision 1). It is configured only by ``egress.toml`` and
runs until ``project-sandbox-egress down``.

Host state, per runtime (design.md Decision 9)::

    egress/<runtime>/
    ├─ sidecar/        mounted read-only into the sidecar at /project-sandbox-egress
    │  ├─ squid.conf  upstream-ca.pem
    │  └─ ca.pem  ca-key.pem (0600)
    ├─ state.json      a cache of what the sidecar runs; every decision re-inspects
    ├─ relay.lock relay.pid relay.port relay.log   (egress_relay, Apple only)
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
LOCK_KIND = "egress-sidecar"
# Squid listens here inside the sidecar; the host side is [egress.sidecar] port.
CONTAINER_PORT = squid_config.LISTEN_PORT
PUBLISH_ADDRESS = "127.0.0.1"

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


def endpoint(port: int) -> str:
    """The published endpoint on the host."""
    return f"{PUBLISH_ADDRESS}:{port}"


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
    lay.root.mkdir(parents=True, exist_ok=True, mode=0o700)
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
# Desired state and fingerprints
# --------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fingerprint(inputs: dict) -> str:
    return _sha256(json.dumps(inputs, sort_keys=True).encode())[:16]


def restart_inputs(
    runtime: Runtime, policy: egress_policy.PolicyFile, *, image: str
) -> dict:
    """What only a restart can change in a running sidecar."""
    ca = policy.upstream_ca_bytes() if policy.upstream_url else b""
    return {
        "image": image,
        "copilot": policy.copilot,
        "upstream_url": policy.upstream_url,
        "upstream_ca_sha256": _sha256(ca) if ca else None,
        "port": policy.port,
        "memory": policy.memory,
        "cpus": policy.cpus,
        "log_options": list(LOG_OPTIONS.get(runtime.name, ())),
    }


def needs_relay(runtime: Runtime, upstream_url: str | None) -> bool:
    """Apple ``container`` reaches a remote parent only through the host relay."""
    if upstream_url is None or runtime.name != APPLE_CONTAINER.name:
        return False
    return not egress_policy._upstream_endpoint(upstream_url, path=None).local


def upstream_for(
    runtime: Runtime,
    upstream_url: str | None,
    *,
    ca_file: bool,
    relay_port: int | None,
) -> squid_config.Upstream | None:
    if upstream_url is None:
        return None
    parsed = egress_policy._upstream_endpoint(upstream_url, path=None)
    return squid_config.upstream_for(
        parsed,
        relay_port=relay_port if needs_relay(runtime, upstream_url) else None,
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
) -> list[str]:
    """Run the sidecar on the default network, publishing on host loopback only."""
    mount = MountSpec(sidecar_dir, INPUT_MOUNT, readonly=True)
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
        "--publish",
        f"{PUBLISH_ADDRESS}:{port}:{CONTAINER_PORT}",
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

    @property
    def port(self) -> int | None:
        """The host port the container was published on (its label)."""
        value = self.labels.get(PORT_LABEL, "")
        return int(value) if value.isdigit() else None


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
    return ContainerInfo(running=running, labels=labels, raw=text)


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


def port_free(port: int) -> bool:
    """Whether nothing listens on ``127.0.0.1:port`` (or the wildcard) on the host.

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
            sock.bind((PUBLISH_ADDRESS, port))
        except OSError:
            return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        return probe.connect_ex((PUBLISH_ADDRESS, port)) != 0


def probe_published(port: int, *, timeout: float = 3.0) -> bool:
    """Whether the sidecar answers its readiness request on the host endpoint."""
    request = (
        f"GET http://{READINESS_HOST}/ HTTP/1.0\r\nHost: {READINESS_HOST}\r\n\r\n"
    ).encode()
    try:
        with socket.create_connection((PUBLISH_ADDRESS, port), timeout=timeout) as sock:
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


def supported(
    runtime: Runtime, *, info: Callable[[Runtime], dict] | None = None
) -> bool:
    """Whether sandboxes on ``runtime`` reach a port published on host loopback.

    True for Apple container (localhost alias), Docker Desktop and Podman
    machine; false for Docker Engine and rootless Podman with pasta on a Linux
    host (egress-sidecar-linux).
    """
    if runtime.name == APPLE_CONTAINER.name:
        return True
    probe = info or local_service_network._runtime_info
    if runtime.name == DOCKER.name:
        return local_service_network._docker_is_desktop(probe(runtime))
    if runtime.name == PODMAN.name:
        return not local_service_network._podman_uses_local_pasta(probe(runtime))
    return False


def unsupported_message(runtime: Runtime) -> str:
    return (
        f"--egress sidecar is not supported on this {runtime.name} runtime yet: "
        "Docker Engine and rootless Podman on a Linux host follow in "
        "egress-sidecar-linux. Sidecar mode works on Apple container, Docker "
        "Desktop and Podman machine; use --egress firewall here."
    )


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
    def endpoint(self) -> str:
        return endpoint(self.port)

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
    port_free: Callable[[int], bool] = port_free
    port_release_timeout: float = PORT_RELEASE_TIMEOUT
    host_probe: Callable[[int], bool] = probe_published


@contextlib.contextmanager
def _locked(lay: Layout):
    lay.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(lay.root, 0o700)
    with path_lock(LOCK_KIND, lay.root):
        yield


def port_drift_message(running: int, configured: int) -> str:
    return (
        f"The running egress sidecar publishes {endpoint(running)}, but egress.toml "
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
) -> Sidecar:
    """Reuse the running, ready sidecar or start it; apply live changes.

    Serialized with every other launch and ``project-sandbox-egress`` command,
    so concurrent first launches create exactly one sidecar. ``expected_port``
    is the port a launch rendered into its sandbox; a running sidecar that
    publishes another one is an error naming ``project-sandbox-egress restart``.
    """
    deps = deps or Collaborators()
    lay = layout(runtime, root)
    with _locked(lay):
        sidecar = _ensure_locked(runtime, policy, image=image, lay=lay, deps=deps)
    if expected_port is not None and sidecar.port != expected_port:
        raise SidecarError(port_drift_message(sidecar.port, expected_port))
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
    new_ca: bool = False,
) -> Sidecar:
    messages: list[str] = []
    state = read_state(lay)
    info = inspect(runtime, deps.runner)
    if _healthy(runtime, lay, state, info, deps.runner):
        assert state is not None and info is not None
        state = _reconcile(
            runtime, policy, image=image, lay=lay, state=state, info=info,
            deps=deps, messages=messages,
        )  # fmt: skip
        started = False
    else:
        state = _cold_start(
            runtime, policy, image=image, lay=lay, deps=deps,
            previous=state, new_ca=new_ca,
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


def port_taken_message(policy: egress_policy.PolicyFile) -> str:
    where = policy.path or egress_policy.default_policy_path()
    return (
        f"{endpoint(policy.port)} is already in use on the host, so the egress "
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
) -> dict:
    runner = deps.runner
    try:
        desired = restart_inputs(runtime, policy, image=image)
        upstream_ca = policy.upstream_ca_bytes() if policy.upstream_url else b""
    except egress_policy.EgressPolicyError as exc:
        raise SidecarError(str(exc)) from None

    # Also clears a stopped one (after a reboot) or a leftover one whose state
    # does not match; its published port is released before the check below.
    runner(remove_container_argv(runtime))
    if not _wait(
        lambda: deps.port_free(policy.port),
        timeout=deps.port_release_timeout,
        clock=deps.clock,
        sleep=deps.sleep,
    ):
        raise SidecarError(port_taken_message(policy))

    lay.sidecar.mkdir(parents=True, exist_ok=True, mode=0o700)
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
    if needs_relay(runtime, policy.upstream_url):
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
    else:
        deps.relay.stop(lay.root)
    upstream = upstream_for(
        runtime, policy.upstream_url, ca_file=bool(upstream_ca), relay_port=relay_port
    )
    conf = render_squid_conf(policy, copilot=policy.copilot, upstream=upstream)
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
    if not _wait(
        lambda: deps.host_probe(policy.port),
        timeout=deps.readiness_timeout,
        clock=deps.clock,
        sleep=deps.sleep,
    ):
        raise SidecarError(
            "the egress sidecar is ready inside its container but does not answer "
            f"on {endpoint(policy.port)} on the host.\n" + log_tail(runtime, runner)
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
    }
    write_state(lay, state)
    return state


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
) -> dict:
    """Apply live changes to a running sidecar; warn about restart-only ones."""
    runner = deps.runner
    state = dict(state)
    upstream = _running_upstream(state)

    # Relay: respawn on the recorded port; follow a move with a reload.
    if needs_relay(runtime, state["upstream_url"]) and upstream is not None:
        ca_path = lay.sidecar / UPSTREAM_CA_NAME
        port = state["relay_port"] or upstream.port
        if deps.relay.port(lay.root) is None:
            bound = _ensure_relay(
                lay,
                deps.relay,
                upstream_url=state["upstream_url"],
                port=port,
                ca_file=ca_path if ca_path.is_file() else None,
            )
            messages.append(f"Restarted the egress relay on 127.0.0.1:{bound}.")
            if bound != port:
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
        state["reload_fingerprint"] = fp
        messages.append(
            f"Applied the egress allowlist from {policy.path or 'built-in defaults'} "
            "to the running sidecar (reloaded, no restart)."
        )

    # Restart-only drift: warn, keep using the running sidecar.
    try:
        desired = restart_inputs(runtime, policy, image=image)
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
    try:
        deps.relay.stop(lay.root)
    except egress_relay.RelayError as exc:
        raise SidecarError(str(exc)) from None
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
    lay = layout(runtime, root)
    with _locked(lay):
        _down_locked(runtime, lay, deps, keep_ca=not new_ca)
        return _ensure_locked(
            runtime, policy, image=image, lay=lay, deps=deps, new_ca=new_ca
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
    with _locked(lay):
        state = read_state(lay)
        info = inspect(runtime, deps.runner)
        if info is None or not info.running or state is None or not _complete(lay):
            raise SidecarError(
                "the egress sidecar is not running; start it with "
                "`project-sandbox-egress up` (changes in egress.toml apply then)"
            )
        messages: list[str] = []
        _reconcile(
            runtime, policy, image=image, lay=lay, state=state, info=info,
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
) -> dict:
    """Describe the sidecar without creating, starting, changing or removing it."""
    deps = deps or Collaborators()
    lay = layout(runtime, root)
    state = read_state(lay)
    info = inspect(runtime, deps.runner)
    running = info is not None and info.running
    ready = running and probe and probe_ready(runtime, deps.runner)
    healthy = (
        running
        and bool(ready)
        and state is not None
        and info is not None
        and info.port == state["port"]
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
        "configured_endpoint": endpoint(policy.port),
        "would": "reuse" if healthy else "start",
    }
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
    relay_needed = needs_relay(
        runtime, (state or {}).get("upstream_url") or policy.upstream_url
    )
    report["relay"] = {
        "needed": relay_needed,
        "live": deps.relay.is_live(lay.root),
        "port": deps.relay.port(lay.root),
    }
    drift: dict = {"reload": False, "restart": []}
    if state is not None and running:
        assert info is not None
        report["endpoint"] = endpoint(info.port or state["port"])
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
                restart_inputs(runtime, policy, image=image),
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
        lines.append(f"  Endpoint: {report['endpoint']} (host loopback)")
        current = "current" if report["image_current"] else "STALE (restart to upgrade)"
        lines.append(f"  Image: {report['image']} ({current})")
        lines.append(f"  Copilot profile: {'on' if report['copilot'] else 'off'}")
        lines.append(f"  Upstream: {report['upstream_url'] or 'none (direct)'}")
    else:
        lines.append(
            f"  Endpoint: {report['configured_endpoint']} (host loopback, once started)"
        )
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
        where = f"127.0.0.1:{relay['port']}" if relay["port"] else "not running"
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
