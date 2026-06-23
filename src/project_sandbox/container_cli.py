import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .git_identity import GitIdentity

RUNTIME_CHOICES = ("auto", "apple-container", "docker", "podman")


@dataclass(frozen=True)
class Runtime:
    name: str
    binary: str


APPLE_CONTAINER = Runtime("apple-container", "container")
DOCKER = Runtime("docker", "docker")
PODMAN = Runtime("podman", "podman")

_RUNTIMES = {
    APPLE_CONTAINER.name: APPLE_CONTAINER,
    DOCKER.name: DOCKER,
    PODMAN.name: PODMAN,
}


def select_runtime(requested: str, *, dry_run: bool = False) -> Runtime:
    if requested not in RUNTIME_CHOICES:
        raise SystemExit(f"Unsupported runtime: {requested}")

    if requested != "auto":
        runtime = _RUNTIMES[requested]
        if not dry_run and shutil.which(runtime.binary) is None:
            raise SystemExit(f"{runtime.binary} CLI not found on PATH")
        return runtime

    if dry_run:
        return APPLE_CONTAINER if sys.platform == "darwin" else DOCKER

    candidates = (
        (APPLE_CONTAINER, DOCKER, PODMAN)
        if sys.platform == "darwin"
        else (DOCKER, PODMAN)
    )
    for runtime in candidates:
        if shutil.which(runtime.binary) is not None:
            return runtime

    names = ", ".join(runtime.name for runtime in candidates)
    raise SystemExit(
        f"No supported container runtime found on PATH for {sys.platform}; "
        f"install one of: {names}, or pass --runtime explicitly."
    )


def build_run_argv(
    *,
    runtime: Runtime = APPLE_CONTAINER,
    image: str,
    project_abs: Path,
    claude_cfg: Path,
    claude_credentials_dir: Path | None = None,
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
    container_name: str | None = None,
    forward_credentials: bool = True,
) -> list[str]:
    argv = [
        runtime.binary,
        "run",
        "--rm",
        "--memory",
        memory,
        "--cpus",
        str(cpus),
        "--workdir",
        "/workspace",
    ]
    if container_name:
        argv += ["--name", container_name]
    if interactive:
        argv.append("-it")
    if firewall_enabled:
        argv += ["--cap-add", "NET_ADMIN", "--cap-add", "NET_RAW"]
    # Generated, non-secret config (settings.json / config.toml) is always
    # mounted; the staged host tokens under /project-sandbox-secrets are only
    # forwarded when forward_credentials is set. With it off, the container
    # starts unauthenticated and the user logs in inside the sandbox.
    argv += [
        "--mount",
        f"type=bind,source={project_abs},target=/workspace",
        "--mount",
        f"type=bind,source={claude_cfg.parent},target=/project-sandbox-config/claude,readonly",
        "--mount",
        f"type=bind,source={codex_cfg.parent},target=/project-sandbox-config/codex,readonly",
    ]
    if forward_credentials:
        argv += [
            "--mount",
            f"type=bind,source={claude_credentials_dir.resolve(strict=False)},target=/project-sandbox-secrets/claude,readonly",
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


def build_stop_argv(
    runtime: Runtime, container_name: str, *, grace: int = 5
) -> list[str]:
    """Return an argv that stops a named container with a bounded grace period.

    All three runtimes support `stop --time <seconds>`, which sends SIGTERM to
    the container's PID 1 and, after at most ``grace`` seconds, force-kills it.
    This bounded graceful stop (rather than an immediate `kill`) lets the
    container shut down cleanly while still guaranteeing a prompt exit.
    """
    return [runtime.binary, "stop", "--time", str(grace), container_name]


def build_image(
    *,
    runtime: Runtime = APPLE_CONTAINER,
    context_dir: Path,
    image_tag: str,
    build_context: Path | None = None,
    dockerfile_path: Path | None = None,
    dry_run: bool = False,
    verbose: bool = True,
) -> int:
    build_context = (build_context or context_dir).resolve(strict=False)
    dockerfile_path = (dockerfile_path or context_dir / "Dockerfile").resolve(strict=False)
    # Build with the context as "." and run from inside it. apple/container only
    # reliably honors a current-directory context; an absolute context path is
    # not mounted into BuildKit correctly, so COPY resolves against the wrong
    # tree and fails with "<file>: not found". Expressing the Dockerfile relative
    # to the context keeps -f valid once cwd is the context. docker/podman are
    # unaffected — "." + cwd is equivalent to an absolute context for them.
    try:
        file_arg = str(dockerfile_path.relative_to(build_context))
    except ValueError:
        file_arg = str(dockerfile_path)
    cmd = [runtime.binary, "build", "-t", image_tag, "-f", file_arg, "."]
    if dry_run:
        print(f"cd {shlex.quote(str(build_context))} && {shlex.join(cmd)}")
        return 0
    return _run_quietable(cmd, verbose=verbose, cwd=str(build_context))


def ensure_system_started(
    *, runtime: Runtime = APPLE_CONTAINER, dry_run: bool = False, verbose: bool = True
) -> int:
    if runtime.name != APPLE_CONTAINER.name:
        return 0
    cmd = [runtime.binary, "system", "start"]
    if dry_run:
        print(shlex.join(cmd))
        return 0
    return _run_quietable(cmd, verbose=verbose)


def _run_quietable(cmd: list[str], *, verbose: bool, cwd: str | None = None) -> int:
    """Run cmd, streaming its output when verbose. When quiet, capture output and
    surface it only if the command fails, so success stays silent but failures
    remain debuggable."""
    try:
        if verbose:
            return subprocess.run(cmd, check=False, cwd=cwd).returncode
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True, cwd=cwd)
        if proc.returncode != 0:
            sys.stdout.write(proc.stdout)
            sys.stderr.write(proc.stderr)
        return proc.returncode
    except FileNotFoundError:
        print(f"{cmd[0]} CLI not found on PATH")
        return 127


def run(argv: list[str], *, dry_run: bool = False) -> int:
    if dry_run:
        print(shlex.join(argv))
        return 0
    try:
        return subprocess.run(argv, check=False).returncode
    except FileNotFoundError:
        print(f"{argv[0]} CLI not found on PATH")
        return 127
