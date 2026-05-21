import subprocess
from collections.abc import Sequence
from pathlib import Path

from .git_identity import GitIdentity


def build_run_argv(
    *,
    image: str,
    project_abs: Path,
    claude_cfg: Path,
    codex_cfg: Path,
    claude_home_host: Path | None,
    codex_home_host: Path | None,
    identity: GitIdentity,
    memory: str,
    cpus: int,
    extra_mounts: list[str],
    agent: str,
    firewall_enabled: bool,
    interactive: bool,
    extra_env: Sequence[str] = (),
    opencode_home_host: Path | None = None,
    copilot_home_host: Path | None = None,
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
        f"type=bind,source={codex_cfg.parent},target=/project-sandbox-config/codex,readonly",
    ]
    if claude_home_host and claude_home_host.exists():
        argv += [
            "--mount",
            f"type=bind,source={claude_home_host},target=/home/agent/.claude.host,readonly",
        ]
    if codex_home_host and codex_home_host.exists():
        argv += [
            "--mount",
            f"type=bind,source={codex_home_host},target=/home/agent/.codex.host,readonly",
        ]
    if opencode_home_host and opencode_home_host.exists():
        argv += [
            "--mount",
            f"type=bind,source={opencode_home_host},target=/home/agent/.config/opencode.host,readonly",
        ]
    if copilot_home_host and copilot_home_host.exists():
        argv += [
            "--mount",
            f"type=bind,source={copilot_home_host},target=/home/agent/.copilot.host,readonly",
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
        "CLAUDE_CONFIG_DIR=/home/agent/.claude",
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
) -> int:
    build_context = build_context or context_dir
    dockerfile_path = dockerfile_path or context_dir / "Dockerfile"
    cmd = ["container", "build", "-t", image_tag]
    default_dockerfile = build_context / "Dockerfile"
    if dockerfile_path.resolve(strict=False) != default_dockerfile.resolve(strict=False):
        cmd += ["-f", str(dockerfile_path)]
    cmd.append(str(build_context))
    if dry_run:
        print(" ".join(cmd))
        return 0
    return subprocess.run(cmd, check=False).returncode


def ensure_system_started(*, dry_run: bool = False) -> int:
    cmd = ["container", "system", "start"]
    if dry_run:
        print(" ".join(cmd))
        return 0
    return subprocess.run(cmd, check=False).returncode


def run(argv: list[str], *, dry_run: bool = False) -> int:
    if dry_run:
        print(" ".join(argv))
        return 0
    return subprocess.run(argv, check=False).returncode
