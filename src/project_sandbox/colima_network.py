"""The two private Docker networks of the Colima egress sidecar.

``psbx-egress-uplink`` is an ordinary bridge that only the sidecar uses to reach
the Internet or a parent proxy. ``psbx-sandbox`` is an ``--internal`` bridge
(no route out) that carries the sidecar under the alias ``egress`` and every
sidecar-mode sandbox. Both are created on demand, labelled, reused, and never
removed by a session (design.md D3).

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


class NetworkError(RuntimeError):
    """A network could not be created, validated or read; the message is user-facing."""


@dataclass(frozen=True)
class NetworkSpec:
    name: str
    role: str
    internal: bool


SPECS = (
    NetworkSpec(UPLINK_NETWORK, UPLINK_ROLE, internal=False),
    NetworkSpec(SANDBOX_NETWORK, SANDBOX_ROLE, internal=True),
)


def create_argv(runtime: Runtime, spec: NetworkSpec) -> list[str]:
    assert runtime.profile is not None
    argv = runtime.argv("network", "create", "--driver", "bridge")
    if spec.internal:
        argv.append("--internal")
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
    if problems:
        raise NetworkError(
            f"Docker network {spec.name!r} already exists and cannot be used: "
            + "; ".join(problems)
            + f". Remove or rename it (docker --context {runtime.context} network "
            f"rm {spec.name}) if it is not needed."
        )


def ensure(runtime: Runtime, runner: Runner) -> None:
    """Create each network when absent and validate it either way.

    Safe to repeat and to race with another launch: a create that loses the race
    ("already exists") is followed by a fresh inspect and validation.
    """
    for spec in SPECS:
        record = inspect(runtime, runner, spec.name)
        if record is None:
            created = runner(create_argv(runtime, spec))
            if created.returncode != 0:
                record = inspect(runtime, runner, spec.name)
                if record is None:
                    detail = (created.stderr or created.stdout or "").strip()
                    raise NetworkError(
                        f"creating Docker network {spec.name!r} failed: "
                        f"{detail or 'unknown error'}"
                    )
            else:
                record = inspect(runtime, runner, spec.name)
                if record is None:
                    raise NetworkError(
                        f"Docker network {spec.name!r} disappeared after creation"
                    )
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
