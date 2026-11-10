#!/usr/bin/env python3
"""Exercise VCS access mounts with real Bash-agent container sessions."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

from project_sandbox.container_cli import select_runtime

ROOT = Path(__file__).resolve().parents[1]
SUBJECT = "vcs-access: commit from full sandbox"


def git(project: Path, *args: str) -> str:
    return subprocess.check_output(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "user.name=VCS E2E",
            "-c",
            "user.email=vcs-e2e@example.invalid",
            "-C",
            str(project),
            *args,
        ],
        text=True,
    )


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def metadata(project: Path) -> dict[str, str]:
    """Capture every original metadata file, not just a few known Git paths."""
    return {
        path.relative_to(project).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for name in (".git", ".jj")
        for path in sorted((project / name).rglob("*"))
        if path.is_file()
    }


def verify_full(project: Path, initial_head: str) -> str:
    require((project / "full.ok").read_text() == "ok\n", "full audit did not finish")
    head = git(project, "rev-parse", "HEAD")
    require(head != initial_head, "full access did not create a commit")
    require(
        (project / "full-commit.txt").read_text() == head,
        "container commit did not remain on host HEAD",
    )
    require(
        git(project, "log", "-1", "--format=%s") == SUBJECT + "\n",
        "unexpected full-access commit subject",
    )
    require(
        git(project, "show", "HEAD:full-access.txt") == "committed in container\n",
        "full-access file was not committed",
    )
    return head


def verify_restricted(
    project: Path,
    mode: str,
    head: str,
    before: dict[str, str],
    titles: str,
) -> None:
    require(
        (project / f"{mode}.ok").read_text() == "ok\n", f"{mode} audit did not finish"
    )
    require(git(project, "rev-parse", "HEAD") == head, f"{mode} changed HEAD")
    require(metadata(project) == before, f"{mode} changed host metadata")
    if mode == "read-only":
        require(
            (project / "commit-titles.txt").read_text() == titles,
            "read-only commit titles did not persist",
        )
        require(
            (project / "read-only-edit.txt").read_text() == "source stays writable\n",
            "read-only source edit did not persist",
        )
    else:
        require(
            (project / "hidden-access.txt").read_text() == "source stays writable\n",
            "hidden source edit did not persist",
        )


def run_session(
    project: Path, workdir: Path, args: argparse.Namespace, image: str, mode: str
) -> None:
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
        "--vcs-access",
        mode,
        "--prompt-text",
        f"bash /workspace/vcs-access-audit.sh {mode}",
        "--no-forward-credentials",
        "--no-token-refresh",
        "--no-firewall",
        "--timeout",
        str(args.timeout),
    ]
    if mode != "full":
        argv.append("--no-build")
    argv.extend((str(project), args.base_image))
    log = workdir / f"{mode}.log"
    print(f"Running {mode}: {log}", flush=True)
    try:
        with log.open("w") as stream:
            subprocess.run(
                argv, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT, check=True
            )
    except subprocess.CalledProcessError as error:
        tail = "\n".join(log.read_text(errors="replace").splitlines()[-20:])
        raise SystemExit(
            f"{mode} session exited {error.returncode}; log: {log}\n{tail}"
        ) from None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime",
        choices=("auto", "docker", "podman", "apple-container"),
        default="auto",
    )
    parser.add_argument("--base-image", default="python:3.12-slim")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    args.runtime = select_runtime(args.runtime).name
    # Apple's build VM cannot read the macOS per-user temporary directory.
    base = ROOT / ".project-sandbox" / "e2e"
    base.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="vcs-access.", dir=base))
    project = workdir / "project"
    project.mkdir()
    image = f"project-sandbox-vcs-access:{uuid4().hex[:12]}"
    success = False
    print(f"Workdir: {workdir}", flush=True)
    try:
        shutil.copyfile(
            ROOT / "scripts/fixtures/vcs-access.sh", project / "vcs-access-audit.sh"
        )
        (project / "README.md").write_text("VCS access fixture\n")
        (project / ".gitignore").write_text(".project-sandbox/\n.devcontainer/\n.jj/\n")
        git(project, "init")
        git(project, "add", "README.md", ".gitignore", "vcs-access-audit.sh")
        git(project, "commit", "-m", "vcs-access: initial commit")
        initial_head = git(project, "rev-parse", "HEAD")
        run_session(project, workdir, args, image, "full")
        head = verify_full(project, initial_head)
        before = metadata(project)
        require(
            {
                ".git/HEAD",
                ".git/config",
                ".git/vcs-access-canary",
                ".jj/vcs-access-canary",
            }
            <= before.keys(),
            "fixture must contain both Git and jj metadata",
        )
        (project / "metadata-files.json").write_text(json.dumps(sorted(before)))
        titles = git(project, "log", "--format=%s")
        for mode in ("read-only", "hidden"):
            run_session(project, workdir, args, image, mode)
            verify_restricted(project, mode, head, before, titles)
        success = True
        print(
            "PASS: full commit persisted; read-only history persisted and writes failed; metadata hidden"
        )
    finally:
        if success and not args.keep:
            shutil.rmtree(workdir)
        else:
            print(f"Workdir retained: {workdir}")


if __name__ == "__main__":
    main()
