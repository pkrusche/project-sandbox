#!/usr/bin/env python3
"""Stop sandbox containers whose project-sandbox CLI is no longer running.

A CLI killed with SIGKILL cannot tear down its headless container. Such a
container keeps running and can break later e2e suites. Only containers that
are both running and recorded as ``orphaned`` (their CLI PID is gone) are
stopped, so a sandbox that is legitimately in use is never touched.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from typing import Any

from project_sandbox import container_cli, observability


def running_containers(runtime: container_cli.Runtime) -> set[str]:
    if runtime == container_cli.APPLE_CONTAINER:
        argv = [runtime.binary, "list", "--quiet"]
    else:
        argv = [runtime.binary, "ps", "--format", "{{.Names}}"]
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        print(
            f"[W] Could not list {runtime.name} containers: {proc.stderr.strip()}",
            file=sys.stderr,
        )
        return set()
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def orphaned_containers(
    runtime: container_cli.Runtime,
    records: list[dict[str, Any]],
    running: set[str],
) -> list[dict[str, Any]]:
    return [
        record
        for record in records
        if record.get("status") == "orphaned"
        and record.get("runtime") == runtime.name
        and record.get("container_name") in running
    ]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--runtime", required=True, choices=("apple-container", "docker", "podman")
    )
    args = p.parse_args(argv)
    runtime = container_cli.select_runtime(args.runtime)
    running = running_containers(runtime)
    if not running:
        return 0
    for record in orphaned_containers(runtime, observability.list_records(), running):
        name = record["container_name"]
        print(f"Stopping orphaned sandbox container {name} (CLI pid {record['pid']})")
        subprocess.run(
            container_cli.build_stop_argv(runtime, name),
            capture_output=True,
            check=False,
        )
        observability.finish_record(
            observability.state_dir() / f"{record['session_id']}.json",
            exit_code=-1,
            status="orphaned",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
