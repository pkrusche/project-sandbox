import shlex
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from .git_identity import GitIdentity


def build_run_argv(
    *,
    image: str,
    project_abs: Path,
    claude_cfg: Path,
    claude_credentials_dir: Path,
    codex_cfg: Path,
    codex_credentials_dir: Path | None,
    identity: GitIdentity,
    memory: str,
    cpus: int,
    extra_mounts: list[str],
    agent: str,
    firewall_enabled: bool,
    interactive: bool,
    extra_env: Sequence[str] = (),
    opencode_credentials_dir: Path | None = None,
) -> list[str]:
    argv = [
        "container",
        "run",
        "--rm",
        "--memory",
        memory,
        "--cpus",
        str(cpus),
        "--workdir",
        "/workspace",
    ]
    if interactive:
        argv.append("-it")
    if firewall_enabled:
        argv += ["--cap-add", "NET_ADMIN", "--cap-add", "NET_RAW"]
    argv += [
        "--mount",
        f"type=bind,source={project_abs},target=/workspace",
        "--mount",
        f"type=bind,source={claude_cfg.parent},target=/project-sandbox-config/claude,readonly",
        "--mount",
        f"type=bind,source={claude_credentials_dir.resolve(strict=False)},target=/project-sandbox-secrets/claude,readonly",
        "--mount",
        f"type=bind,source={codex_cfg.parent},target=/project-sandbox-config/codex,readonly",
    ]
    if codex_credentials_dir is not None:
        argv += [
            "--mount",
            f"type=bind,source={codex_credentials_dir.resolve(strict=False)},target=/project-sandbox-secrets/codex,readonly",
        ]
    if opencode_credentials_dir is not None:
        argv += [
            "--mount",
            f"type=bind,source={opencode_credentials_dir.resolve(strict=False)},target=/project-sandbox-secrets/opencode,readonly",
        ]
    if identity.name:
        argv += [
            "--env",
            f"PROJECT_SANDBOX_USER_NAME={identity.name}",
            "--env",
            f"GIT_AUTHOR_NAME={identity.name}",
            "--env",
            f"GIT_COMMITTER_NAME={identity.name}",
        ]
    if identity.email:
        argv += [
            "--env",
            f"PROJECT_SANDBOX_USER_EMAIL={identity.email}",
            "--env",
            f"GIT_AUTHOR_EMAIL={identity.email}",
            "--env",
            f"GIT_COMMITTER_EMAIL={identity.email}",
        ]
    argv += [
        "--env",
        "CLAUDE_SECURESTORAGE_CONFIG_DIR=/home/agent/.claude",
        "--env",
        "CODEX_HOME=/home/agent/.codex",
    ]
    if not firewall_enabled:
        argv += ["--env", "PROJECT_SANDBOX_NO_FIREWALL=1"]
    for env in extra_env:
        argv += ["--env", env]
    for m in extra_mounts:
        argv += ["--mount", m]
    argv += [image, "project-sandbox-run", agent]
    return argv


def build_image(
    *,
    context_dir: Path,
    image_tag: str,
    build_context: Path | None = None,
    dockerfile_path: Path | None = None,
    dry_run: bool = False,
    verbose: bool = True,
) -> int:
    build_context = build_context or context_dir
    dockerfile_path = dockerfile_path or context_dir / "Dockerfile"
    cmd = ["container", "build", "-t", image_tag]
    default_dockerfile = build_context / "Dockerfile"
    if dockerfile_path.resolve(strict=False) != default_dockerfile.resolve(strict=False):
        cmd += ["-f", str(dockerfile_path)]
    cmd.append(str(build_context))
    if dry_run:
        print(shlex.join(cmd))
        return 0
    return _run_quietable(cmd, verbose=verbose)


def ensure_system_started(*, dry_run: bool = False, verbose: bool = True) -> int:
    cmd = ["container", "system", "start"]
    if dry_run:
        print(shlex.join(cmd))
        return 0
    return _run_quietable(cmd, verbose=verbose)


def _run_quietable(cmd: list[str], *, verbose: bool) -> int:
    """Run cmd, streaming its output when verbose. When quiet, capture output and
    surface it only if the command fails, so success stays silent but failures
    remain debuggable."""
    try:
        if verbose:
            return subprocess.run(cmd, check=False).returncode
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if proc.returncode != 0:
            sys.stdout.write(proc.stdout)
            sys.stderr.write(proc.stderr)
        return proc.returncode
    except FileNotFoundError:
        print("container CLI not found on PATH")
        return 127


def run(argv: list[str], *, dry_run: bool = False) -> int:
    if dry_run:
        print(shlex.join(argv))
        return 0
    try:
        return subprocess.run(argv, check=False).returncode
    except FileNotFoundError:
        print("container CLI not found on PATH")
        return 127
