#!/usr/bin/env python3
"""Remove containers, forwarders, and images left behind by the e2e suites.

Run after ``scripts/run-e2e-tests.sh`` (or an interrupted e2e script) to sweep:

* sandbox containers (``project-sandbox-<session>``) running an e2e image;
* Squid fixture containers that ``e2e-build-proxy.py`` started through IPL,
  recognised by their config mount inside a ``project-sandbox-build-proxy.*``
  temp directory, so a real IPL proxy is never touched;
* socat forwarders whose project-sandbox CLI is gone;
* images tagged by the e2e suites, either explicitly or derived from their
  temporary project directories.

Only names the e2e scripts generate are matched: images of real projects,
other containers, and live sandbox sessions are left alone. Refuses to run
while an e2e script is active unless ``--force`` is given. ``--dry-run`` lists
what would be removed without changing anything.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from project_sandbox import container_cli

RUNTIMES = (container_cli.APPLE_CONTAINER, container_cli.DOCKER, container_cli.PODMAN)

SESSION_CONTAINER = re.compile(r"project-sandbox-\d{8}t\d{6}-[0-9a-f]{12}")
SQUID_FIXTURE_PREFIX = "internet-proxy-squid-"
SQUID_FIXTURE_ROOT = "/project-sandbox-build-proxy."
IPL_MANAGED_LABEL = "io.internet-proxy-locally.managed"

# Temp project directories whose basename becomes the default image tag
# ``project-sandbox-<dir>-<path hash>``. ``mktemp`` (shell suites) appends six
# alphanumerics; ``mkdtemp`` (Python suites) appends eight of ``[a-z0-9_]``,
# where a separator run may have been collapsed by the tag sanitiser.
_MKTEMP_DIRS = (
    "env-e2e",
    "git-e2e",
    "jj-e2e",
    "pi-ollama-e2e",
    "dockerfile-tamper-e2e",
    "node-npm",
    "project-sandbox-e2e",
    "project",  # verify-timeout-teardown.sh
)
_MKDTEMP_DIRS = (
    "project-sandbox-agent-proxy",
    "agent-proxy-isolation",
    "internet-proxy-e2e",
)
E2E_IMAGE = re.compile(
    "project-sandbox-(?:"
    rf"(?:{'|'.join(map(re.escape, _MKTEMP_DIRS))})[._-][a-z0-9]{{6}}-[0-9a-f]{{8}}"
    rf"|(?:{'|'.join(map(re.escape, _MKDTEMP_DIRS))})[._-][a-z0-9._-]{{6,9}}-[0-9a-f]{{8}}"
    # e2e-python-uv.sh projects use fixed directory names.
    r"|(?:managed|base)-python-[0-9a-f]{8}"
    # Explicit --image-tag values.
    r"|internet-proxy-smoke"
    r"|proxy-endpoints-[a-z0-9_]{8}"
    r"|build-proxy"
    ")"
)

ACTIVE_E2E = re.compile(
    r"(?:^|/)(?:run-e2e-tests\.sh|verify-timeout-teardown\.sh|check-agent-proxy\.py"
    r"|e2e-(?!cleanup\.py|reap-orphans\.py)[\w-]+\.(?:sh|py))(?:\s|$)"
)
SOCAT_LISTEN = re.compile(r"TCP-LISTEN:\d+,bind=[^,]+,reuseaddr,fork")


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _lines(argv: list[str], what: str) -> list[str] | None:
    proc = _run(argv)
    if proc.returncode != 0:
        print(f"[W] Could not list {what}: {proc.stderr.strip()}", file=sys.stderr)
        return None
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def image_repository(reference: str) -> str:
    """``localhost/name:tag`` or ``docker.io/library/name:tag`` -> ``name``."""
    name = reference.rsplit("/", 1)[-1]
    return name.split("@", 1)[0].split(":", 1)[0]


def is_e2e_image(reference: str) -> bool:
    return E2E_IMAGE.fullmatch(image_repository(reference)) is not None


def container_facts(
    entry: dict[str, Any],
) -> tuple[str, str, dict[str, str], list[str]]:
    """Name, image, labels, and mount sources from a Docker/Podman or Apple inspect entry."""
    config = entry.get("configuration")
    if isinstance(config, dict):  # Apple container
        image = config.get("image") or {}
        mounts = config.get("mounts") or []
        return (
            str(config.get("id", "")),
            str(image.get("reference", "")) if isinstance(image, dict) else "",
            dict(config.get("labels") or {}),
            [str(m.get("source", "")) for m in mounts if isinstance(m, dict)],
        )
    docker_config = entry.get("Config") or {}
    mounts = entry.get("Mounts") or []
    return (
        str(entry.get("Name", "")).lstrip("/"),
        str(docker_config.get("Image", "")),
        dict(docker_config.get("Labels") or {}),
        [str(m.get("Source", "")) for m in mounts if isinstance(m, dict)],
    )


def is_e2e_container(entry: dict[str, Any]) -> bool:
    name, image, labels, sources = container_facts(entry)
    if SESSION_CONTAINER.fullmatch(name):
        return is_e2e_image(image)
    if name.startswith(SQUID_FIXTURE_PREFIX):
        return labels.get(IPL_MANAGED_LABEL) == "true" and any(
            SQUID_FIXTURE_ROOT in source for source in sources
        )
    return False


def e2e_containers(runtime: container_cli.Runtime) -> list[str]:
    if runtime == container_cli.APPLE_CONTAINER:
        listing = [runtime.binary, "list", "--all", "--quiet"]
    else:
        listing = [runtime.binary, "ps", "--all", "--format", "{{.Names}}"]
    names = _lines(listing, f"{runtime.name} containers")
    candidates = [
        name
        for name in names or []
        if SESSION_CONTAINER.fullmatch(name) or name.startswith(SQUID_FIXTURE_PREFIX)
    ]
    if not candidates:
        return []
    proc = _run([runtime.binary, "inspect", *candidates])
    try:
        entries = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        entries = []
    if not isinstance(entries, list) or not entries:
        print(
            f"[W] Could not inspect {runtime.name} containers: {proc.stderr.strip()}",
            file=sys.stderr,
        )
        return []
    return [
        container_facts(entry)[0]
        for entry in entries
        if isinstance(entry, dict) and is_e2e_container(entry)
    ]


def remove_container_argvs(
    runtime: container_cli.Runtime, name: str
) -> list[list[str]]:
    if runtime == container_cli.APPLE_CONTAINER:
        return [[runtime.binary, "stop", name], [runtime.binary, "rm", name]]
    return [[runtime.binary, "rm", "--force", name]]


def e2e_images(runtime: container_cli.Runtime) -> list[str]:
    if runtime == container_cli.APPLE_CONTAINER:
        listing = [runtime.binary, "image", "list", "--quiet"]
    else:
        listing = [runtime.binary, "images", "--format", "{{.Repository}}:{{.Tag}}"]
    references = _lines(listing, f"{runtime.name} images") or []
    return sorted({ref for ref in references if is_e2e_image(ref)})


def processes() -> dict[int, tuple[int, str]]:
    """pid -> (ppid, args) for every visible process."""
    table: dict[int, tuple[int, str]] = {}
    for line in _lines(["ps", "-eo", "pid=,ppid=,args="], "processes") or []:
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), parts[2])
    return table


def active_e2e(table: dict[int, tuple[int, str]]) -> list[str]:
    return [
        args
        for pid, (_, args) in table.items()
        if pid != os.getpid() and ACTIVE_E2E.search(args)
    ]


def orphaned_forwarders(table: dict[int, tuple[int, str]]) -> list[int]:
    """socat bridges started by local_service_network whose CLI has exited."""
    orphans = []
    for pid, (ppid, args) in table.items():
        argv = args.split()
        if (
            len(argv) == 3
            and Path(argv[0]).name == "socat"
            and SOCAT_LISTEN.fullmatch(argv[1])
            and re.match(r"TCP6?:", argv[2])
            and "project-sandbox" not in table.get(ppid, (0, ""))[1]
        ):
            orphans.append(pid)
    return sorted(orphans)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--runtime",
        choices=[runtime.name for runtime in RUNTIMES],
        action="append",
        help="Runtime to clean (repeatable; default: every one found on PATH).",
    )
    p.add_argument("--dry-run", action="store_true", help="List only; remove nothing.")
    p.add_argument(
        "--force", action="store_true", help="Clean up even while an e2e script runs."
    )
    args = p.parse_args(argv)

    table = processes()
    active = active_e2e(table)
    if active and not args.force:
        print("Refusing to clean up while e2e scripts are running:", file=sys.stderr)
        for command in active:
            print(f"  {command}", file=sys.stderr)
        print("Pass --force to clean up anyway.", file=sys.stderr)
        return 1

    prefix = "Would remove" if args.dry_run else "Removing"
    failed = False
    for pid in orphaned_forwarders(table):
        print(f"{prefix} orphaned forwarder pid {pid}: {table[pid][1]}")
        if not args.dry_run:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                print(f"[W] Could not stop pid {pid}: {exc}", file=sys.stderr)
                failed = True

    selected = args.runtime or [
        runtime.name for runtime in RUNTIMES if shutil.which(runtime.binary)
    ]
    for runtime in RUNTIMES:
        if runtime.name not in selected:
            continue
        if shutil.which(runtime.binary) is None:
            print(f"[W] {runtime.binary} not found on PATH", file=sys.stderr)
            failed = True
            continue
        # Containers first: an image still in use by one cannot be removed.
        work = [
            (f"{runtime.name} container {name}", remove_container_argvs(runtime, name))
            for name in e2e_containers(runtime)
        ] + [
            (f"{runtime.name} image {ref}", [[runtime.binary, "image", "rm", ref]])
            for ref in e2e_images(runtime)
        ]
        for label, commands in work:
            print(f"{prefix} {label}")
            if args.dry_run:
                continue
            result = None
            for command in commands:
                result = _run(command)
            if result is not None and result.returncode != 0:
                print(
                    f"[W] Could not remove {label}: {result.stderr.strip()}",
                    file=sys.stderr,
                )
                failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
