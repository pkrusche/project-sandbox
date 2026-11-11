#!/usr/bin/env python3
"""Verify firewall initialization revokes sudo in real Bash-agent containers."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

from project_sandbox.container_cli import select_runtime

ROOT = Path(__file__).resolve().parents[1]


def run_session(
    project: Path, workdir: Path, args: argparse.Namespace, image: str, attempt: int
) -> None:
    # The second run reuses the image, but must get a fresh container with its
    # own successful initialization and revoked sudo grant. Each session writes
    # its own marker: on Podman machine, re-creating a file the host just
    # deleted can fail with ENOENT through the VM's shared-folder cache.
    marker = project / f"firewall-{attempt}.ok"
    argv = [
        "uv",
        "run",
        "project-sandbox",
        "--runtime",
        args.runtime,
        "--image-tag",
        image,
        "--agent",
        "bash",
        "--egress",
        "firewall",
        "--prompt-text",
        f"python3 -I /workspace/firewall-audit.py {marker.name}",
        "--no-forward-credentials",
        "--no-token-refresh",
        "--timeout",
        "180",
    ]
    if attempt > 1:
        argv.append("--no-build")
    argv.extend((str(project), args.base_image))
    marker.unlink(missing_ok=True)
    log = workdir / f"session-{attempt}.log"
    print(f"Running fresh container {attempt}: {log}", flush=True)
    try:
        with log.open("w") as stream:
            subprocess.run(
                argv, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT, check=True
            )
    except subprocess.CalledProcessError as error:
        tail = "\n".join(log.read_text(errors="replace").splitlines()[-20:])
        raise SystemExit(
            f"session {attempt} exited {error.returncode}; log: {log}\n{tail}"
        ) from None
    if marker.read_text() != "initialized; sudo revoked\n":
        raise RuntimeError("firewall audit did not complete")
    if (project / "shadow-imported").exists():
        raise RuntimeError("privileged initialization imported a workspace module")
    for name in ("sudo-policy.log", "sudo-attempt.log"):
        shutil.copyfile(project / name, workdir / f"session-{attempt}-{name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime",
        choices=("auto", "docker", "podman", "apple-container"),
        default="auto",
    )
    parser.add_argument("--base-image", default="python:3.12-slim")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    args.runtime = select_runtime(args.runtime).name
    base = ROOT / ".project-sandbox" / "e2e"
    base.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="firewall.", dir=base))
    project = workdir / "project"
    project.mkdir()
    image = f"project-sandbox-firewall:{uuid4().hex[:12]}"
    success = False
    print(f"Workdir: {workdir}", flush=True)
    try:
        shutil.copyfile(
            ROOT / "scripts/fixtures/firewall.py", project / "firewall-audit.py"
        )
        # These run if a privileged Python helper accidentally imports from cwd.
        # The audit itself uses -I, so only initialization can trigger the trap.
        for module in ("socket", "ipaddress", "json", "os", "sitecustomize"):
            (project / f"{module}.py").write_text(
                "from pathlib import Path\n"
                "Path('/workspace/shadow-imported').write_text('imported')\n"
                "raise RuntimeError('workspace import shadow executed')\n"
            )
        for attempt in (1, 2):
            run_session(project, workdir, args, image, attempt)
        success = True
        print(
            "PASS: firewall initialized and sudo replay denied in two fresh containers"
        )
    finally:
        if success and not args.keep:
            shutil.rmtree(workdir)
        else:
            print(f"Workdir retained: {workdir}")


if __name__ == "__main__":
    main()
