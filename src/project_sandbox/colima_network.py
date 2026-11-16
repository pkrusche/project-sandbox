"""The two private Docker networks of the Colima egress sidecar.

``psbx-egress-uplink`` is an ordinary bridge that only the sidecar uses to reach
the Internet or a parent proxy. ``psbx-sandbox`` is an ``--internal`` bridge
(no route out) that carries the sidecar under the alias ``egress`` and every
sidecar-mode sandbox. Both are created on demand, labelled, reused, and never
removed by a session (design.md D3).

``psbx-sandbox`` uses the isolated IPv4 gateway mode, so the Colima VM has no
address on it and a sandbox cannot reach the VM's own listeners
(isolated-sidecar-network spike S2, design.md D2). A network created by an
older version without that option is replaced once nothing else is attached.

This module only builds argv and validates what Docker reports; the sidecar
module decides when to call it.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .container_cli import Runtime

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]

UPLINK_NETWORK = "psbx-egress-uplink"
SANDBOX_NETWORK = "psbx-sandbox"
ALIAS = "egress"
PROXY_PORT = 3128  # squid_config.LISTEN_PORT; fixed and never published

OWNER_LABEL = "project-sandbox.owner"
OWNER = "project-sandbox"
PROFILE_LABEL = "project-sandbox.profile"
ROLE_LABEL = "project-sandbox.network.role"
UPLINK_ROLE = "egress-uplink"
SANDBOX_ROLE = "sandbox"


# The bridge gets no address in the VM (Docker Engine 28+, spike S2).
GATEWAY_MODE_OPTION = "com.docker.network.bridge.gateway_mode_ipv4"
GATEWAY_MODE = "isolated"
# Older engines may ignore an unknown bridge option yet still report it under
# Options, so the version is checked before the network is created.
MIN_ENGINE = (28, 0)


class NetworkError(RuntimeError):
    """A network could not be created, validated or read; the message is user-facing."""


class LegacyNetwork(NetworkError):
    """Ours, but created without a required driver option; it must be replaced."""


@dataclass(frozen=True)
class NetworkSpec:
    name: str
    role: str
    internal: bool
    options: tuple[tuple[str, str], ...] = ()


SPECS = (
    NetworkSpec(UPLINK_NETWORK, UPLINK_ROLE, internal=False),
    NetworkSpec(
        SANDBOX_NETWORK,
        SANDBOX_ROLE,
        internal=True,
        options=((GATEWAY_MODE_OPTION, GATEWAY_MODE),),
    ),
)


def create_argv(runtime: Runtime, spec: NetworkSpec) -> list[str]:
    assert runtime.profile is not None
    argv = runtime.argv("network", "create", "--driver", "bridge")
    if spec.internal:
        argv.append("--internal")
    for key, value in spec.options:
        argv += ["-o", f"{key}={value}"]
    for key, value in (
        (OWNER_LABEL, OWNER),
        (PROFILE_LABEL, runtime.profile),
        (ROLE_LABEL, spec.role),
    ):
        argv += ["--label", f"{key}={value}"]
    return [*argv, spec.name]


def inspect_argv(runtime: Runtime, name: str) -> list[str]:
    return runtime.argv("network", "inspect", name, "--format", "{{json .}}")


def connect_argv(
    runtime: Runtime, network: str, container: str, alias: str
) -> list[str]:
    return runtime.argv("network", "connect", "--alias", alias, network, container)


def disconnect_argv(runtime: Runtime, network: str, container: str) -> list[str]:
    return runtime.argv("network", "disconnect", "--force", network, container)


def remove_argv(runtime: Runtime, name: str) -> list[str]:
    return runtime.argv("network", "rm", name)


def engine_version_argv(runtime: Runtime) -> list[str]:
    return runtime.argv("version", "--format", "{{.Server.Version}}")


def _load(text: str) -> dict:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise NetworkError("could not parse Docker's network inspect output") from exc
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        raise NetworkError("unexpected Docker network inspect output")
    return data


def inspect(runtime: Runtime, runner: Runner, name: str) -> dict | None:
    """The network's inspect record, or ``None`` when it does not exist."""
    result = runner(inspect_argv(runtime, name))
    if result.returncode != 0:
        return None
    return _load(result.stdout or "")


def validate(runtime: Runtime, record: dict, spec: NetworkSpec) -> None:
    """Refuse a same-named network that is not ours or has the wrong shape."""
    labels = record.get("Labels") or {}
    problems = []
    if labels.get(OWNER_LABEL) != OWNER:
        problems.append("it is not owned by project-sandbox (missing owner label)")
    elif labels.get(PROFILE_LABEL) != runtime.profile:
        problems.append(
            f"it belongs to Colima profile {labels.get(PROFILE_LABEL)!r}, "
            f"not {runtime.profile!r}"
        )
    elif labels.get(ROLE_LABEL) != spec.role:
        problems.append(f"its role is {labels.get(ROLE_LABEL)!r}, not {spec.role!r}")
    if record.get("Driver") != "bridge":
        problems.append(f"its driver is {record.get('Driver')!r}, not 'bridge'")
    if bool(record.get("Internal")) != spec.internal:
        problems.append(
            "it is internal and must not be"
            if record.get("Internal")
            else "it is not internal and must be"
        )
    options = record.get("Options") or {}
    missing = []
    for key, value in spec.options:
        if key not in options:
            missing.append(f"{key}={value}")
        elif options[key] != value:
            problems.append(f"its {key} is {options[key]!r}, not {value!r}")
    if problems:
        raise NetworkError(
            f"Docker network {spec.name!r} already exists and cannot be used: "
            + "; ".join(problems)
            + f". Remove or rename it (docker --context {runtime.context} network "
            f"rm {spec.name}) if it is not needed."
        )
    if missing:
        raise LegacyNetwork(
            f"Docker network {spec.name!r} was created by an older project-sandbox "
            f"without {', '.join(missing)}, so the Colima VM has an address on it "
            "and its listeners are reachable from sandboxes"
        )


def attached_containers(record: dict) -> list[str]:
    """Names of the containers attached to the network ``record`` describes."""
    containers = record.get("Containers") or {}
    names = []
    for key, entry in containers.items():
        name = entry.get("Name") if isinstance(entry, dict) else None
        names.append(str(name or key))
    return sorted(names)


def _parse_version(text: str) -> tuple[int, int] | None:
    parts = text.strip().lstrip("v").split(".")
    try:
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return None


def check_engine(runtime: Runtime, runner: Runner) -> None:
    """Refuse an engine too old for the isolated gateway mode (design.md D2)."""
    result = runner(engine_version_argv(runtime))
    found = _parse_version(result.stdout or "") if result.returncode == 0 else None
    if found is not None and found >= MIN_ENGINE:
        return
    shown = (result.stdout or "").strip() or "unknown"
    required = ".".join(map(str, MIN_ENGINE))
    raise NetworkError(
        f"the {SANDBOX_NETWORK} network needs {GATEWAY_MODE_OPTION}={GATEWAY_MODE}, "
        f"which requires Docker Engine {required} or later in the Colima VM "
        f"(found {shown}). Upgrade Colima, then update the profile's engine with "
        f"`colima update --profile {runtime.profile}`."
    )


def _create(runtime: Runtime, runner: Runner, spec: NetworkSpec) -> dict:
    if spec.options:
        check_engine(runtime, runner)
    created = runner(create_argv(runtime, spec))
    record = inspect(runtime, runner, spec.name)
    if created.returncode != 0 and record is None:
        detail = (created.stderr or created.stdout or "").strip()
        hint = ""
        if spec.options:
            hint = (
                f" It is created with {', '.join(f'{k}={v}' for k, v in spec.options)}; "
                "if the engine rejected that option, upgrade Colima and run "
                f"`colima update --profile {runtime.profile}`."
            )
        raise NetworkError(
            f"creating Docker network {spec.name!r} failed: "
            f"{detail or 'unknown error'}.{hint}"
        )
    if record is None:
        raise NetworkError(f"Docker network {spec.name!r} disappeared after creation")
    return record


def _replace(
    runtime: Runtime,
    runner: Runner,
    spec: NetworkSpec,
    record: dict,
    owned: tuple[str, ...],
    reason: str,
) -> dict:
    """Remove and recreate a legacy network once only ``owned`` is attached."""
    attached = attached_containers(record)
    others = [name for name in attached if name not in owned]
    if others:
        verb = "is" if len(others) == 1 else "are"
        raise NetworkError(
            f"{reason}. It must be replaced, but {', '.join(others)} {verb} still "
            "attached. Stop those sandboxes, then run `project-sandbox-egress "
            f"restart --runtime colima --colima-profile {runtime.profile}`."
        )
    for name in attached:
        runner(disconnect_argv(runtime, spec.name, name))
    removed = runner(remove_argv(runtime, spec.name))
    if removed.returncode != 0 and inspect(runtime, runner, spec.name) is not None:
        detail = (removed.stderr or removed.stdout or "").strip()
        raise NetworkError(
            f"{reason}. Removing it to recreate it failed: {detail or 'unknown error'}"
        )
    return _create(runtime, runner, spec)


def is_legacy(runtime: Runtime, runner: Runner) -> bool:
    """The sandbox network exists and only lacks a required option."""
    spec = SPECS[1]
    record = inspect(runtime, runner, spec.name)
    if record is None:
        return False
    try:
        validate(runtime, record, spec)
    except LegacyNetwork:
        return True
    except NetworkError:
        return False
    return False


def ensure(runtime: Runtime, runner: Runner, *, owned: tuple[str, ...] = ()) -> None:
    """Create each network when absent and validate it either way.

    Safe to repeat and to race with another launch: a create that loses the race
    ("already exists") is followed by a fresh inspect and validation. A legacy
    network (ours, missing a required option) is replaced when only the
    containers in ``owned`` (the sidecar, which the caller recreates) are
    attached; otherwise this fails naming the attached sandboxes.
    """
    for spec in SPECS:
        record = inspect(runtime, runner, spec.name)
        if record is None:
            record = _create(runtime, runner, spec)
        try:
            validate(runtime, record, spec)
        except LegacyNetwork as exc:
            record = _replace(runtime, runner, spec, record, owned, str(exc))
            validate(runtime, record, spec)


@dataclass(frozen=True)
class Attachment:
    """The sidecar's place on the sandbox network."""

    address: str
    aliases: tuple[str, ...]


def attachment(record: dict, network: str = SANDBOX_NETWORK) -> Attachment | None:
    """``record`` is a container's inspect JSON; ``None`` when not attached."""
    networks = (record.get("NetworkSettings") or {}).get("Networks") or {}
    entry = networks.get(network)
    if not isinstance(entry, dict) or not entry.get("IPAddress"):
        return None
    aliases = tuple(str(a) for a in (entry.get("Aliases") or ()))
    return Attachment(str(entry["IPAddress"]), aliases)


def attached_to_both(record: dict) -> bool:
    """Running sidecar is on the uplink and on the sandbox network as ``egress``."""
    networks = (record.get("NetworkSettings") or {}).get("Networks") or {}
    sandbox = attachment(record)
    return (
        UPLINK_NETWORK in networks and sandbox is not None and ALIAS in sandbox.aliases
    )
