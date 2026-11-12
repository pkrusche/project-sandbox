import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import uuid
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import config_agents, copilot_auth, mount_options, session
from .git_identity import GitIdentity
from .paths import WORKSPACE_NODE_MODULES_TARGET

RUNTIME_CHOICES = ("auto", "apple-container", "docker", "podman", "chroot")


@dataclass(frozen=True)
class Runtime:
    name: str
    binary: str
    # False for runtimes (like chroot) that neither build nor run an image;
    # lets callers branch on capability instead of comparing runtime identity.
    is_container: bool = True


@dataclass(frozen=True)
class MountSpec:
    source: Path
    target: str
    readonly: bool = False
    # Set for a --mount value we don't model structurally (a non-bind type, or
    # one missing source/target even after alias lookup); passed through to
    # the runtime unchanged instead of being reconstructed from fields.
    raw: str | None = None


APPLE_CONTAINER = Runtime("apple-container", "container")
DOCKER = Runtime("docker", "docker")
PODMAN = Runtime("podman", "podman")
CHROOT = Runtime("chroot", "unshare", is_container=False)

_RUNTIMES = {
    APPLE_CONTAINER.name: APPLE_CONTAINER,
    DOCKER.name: DOCKER,
    PODMAN.name: PODMAN,
    CHROOT.name: CHROOT,
}


def host_build_identity(runtime: Runtime) -> tuple[int, int] | None:
    """Return the host identity that Docker/Podman images must build for."""
    if not sys.platform.startswith("linux") or runtime.name not in (
        DOCKER.name,
        PODMAN.name,
    ):
        return None
    uid, gid = os.getuid(), os.getgid()
    # UID 0 cannot be assigned to the non-root agent user. A root caller can
    # already access and clean up files owned by the image's default UID 1000.
    # GID 0 is valid for a non-root user and must still be matched: otherwise a
    # host with that primary group would retain the same bind-mount mismatch.
    return (uid, gid) if uid != 0 else None


def select_runtime(requested: str, *, dry_run: bool = False) -> Runtime:
    if requested not in RUNTIME_CHOICES:
        raise SystemExit(f"Unsupported runtime: {requested}")

    if requested != "auto":
        runtime = _RUNTIMES[requested]
        if runtime == CHROOT and not sys.platform.startswith("linux"):
            raise SystemExit("The chroot runtime is supported on Linux only")
        if not dry_run and shutil.which(runtime.binary) is None:
            raise SystemExit(f"{runtime.binary} CLI not found on PATH")
        if not dry_run and runtime == PODMAN:
            require_rootless_podman()
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
            if runtime == PODMAN:
                require_rootless_podman()
            return runtime

    names = ", ".join(runtime.name for runtime in candidates)
    raise SystemExit(
        f"No supported container runtime found on PATH for {sys.platform}; "
        f"install one of: {names}, or pass --runtime explicitly."
    )


def podman_rootless() -> bool | None:
    """Whether podman runs rootless, or None when ``podman info`` cannot say.

    Asks podman rather than inspecting the caller: a root caller, or a podman
    machine or connection set to rootful, all report ``false`` here.
    """
    try:
        proc = subprocess.run(
            [PODMAN.binary, "info", "--format", "{{.Host.Security.Rootless}}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    answer = proc.stdout.strip().lower() if proc.returncode == 0 else ""
    return {"true": True, "false": False}.get(answer)


def require_rootless_podman() -> None:
    """Refuse rootful podman, which project-sandbox does not support."""
    rootless = podman_rootless()
    if rootless is True:
        return
    if rootless is False:
        raise SystemExit(
            "podman is running rootful, which project-sandbox does not support. "
            "Run project-sandbox as a normal user with rootless podman"
            + (
                ", or switch the machine with: podman machine set --rootful=false"
                if sys.platform == "darwin"
                else "."
            )
        )
    raise SystemExit(
        "Could not determine whether podman runs rootless; check `podman info`. "
        "project-sandbox supports rootless podman only."
    )


def parse_mount(value: str) -> MountSpec:
    try:
        fields = mount_options.parse(value)
        source, target = fields.get("source"), fields.get("target")
        if target is not None:
            mount_options.validate_target(target)
        if not mount_options.simple_bind(fields):
            # Runtime-specific options are retained verbatim. Security checks
            # independently require a bounded bind source, even for raw mounts.
            return MountSpec(
                Path(source).resolve(strict=False) if source else Path("."),
                target or "",
                readonly=fields.get("readonly") == "true",
                raw=value,
            )
        assert source is not None and target is not None
        return MountSpec(
            Path(source).resolve(strict=False), target, fields.get("readonly") == "true"
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def build_mount_specs(
    *,
    project_abs: Path,
    claude_cfg: Path,
    claude_credentials_dir: Path | None,
    codex_cfg: Path,
    codex_credentials_dir: Path | None,
    opencode_credentials_dir: Path | None,
    pi_credentials_dir: Path | None = None,
    pi_cfg: Path | None = None,
    copilot_credentials_dir: Path | None = None,
    copilot_cfg: Path | None = None,
    extra_mounts: Sequence[str] = (),
    forward_credentials: bool = True,
    agent: str,
) -> list[MountSpec]:
    mounts = [
        MountSpec(project_abs.resolve(strict=False), "/workspace"),
        MountSpec(
            claude_cfg.parent.resolve(strict=False),
            "/project-sandbox-config/claude",
            True,
        ),
        MountSpec(
            codex_cfg.parent.resolve(strict=False),
            "/project-sandbox-config/codex",
            True,
        ),
    ]
    if pi_cfg is not None:
        mounts.append(
            MountSpec(
                pi_cfg.parent.resolve(strict=False),
                "/project-sandbox-config/pi",
                True,
            )
        )
    if copilot_cfg is not None:
        mounts.append(
            MountSpec(
                copilot_cfg.parent.resolve(strict=False),
                copilot_auth.CONFIG_TARGET,
                True,
            )
        )
    allowed_credentials = config_agents.allowed_credential_agents(agent)
    if forward_credentials:
        if "claude" in allowed_credentials and claude_credentials_dir is not None:
            mounts.append(
                MountSpec(
                    claude_credentials_dir.resolve(strict=False),
                    "/project-sandbox-secrets/claude",
                    True,
                )
            )
        if "codex" in allowed_credentials and codex_credentials_dir is not None:
            mounts.append(
                MountSpec(
                    codex_credentials_dir.resolve(strict=False),
                    "/project-sandbox-secrets/codex",
                    True,
                )
            )
        if "opencode" in allowed_credentials and opencode_credentials_dir is not None:
            mounts.append(
                MountSpec(
                    opencode_credentials_dir.resolve(strict=False),
                    "/project-sandbox-secrets/opencode",
                    True,
                )
            )
        if "pi" in allowed_credentials and pi_credentials_dir is not None:
            mounts.append(
                MountSpec(
                    pi_credentials_dir.resolve(strict=False),
                    "/project-sandbox-secrets/pi",
                    True,
                )
            )
        # The Copilot token is a GitHub token: config_agents never authorizes
        # it for bash, so only a copilot/copilot-headless run mounts it.
        if "copilot" in allowed_credentials and copilot_credentials_dir is not None:
            mounts.append(
                MountSpec(
                    copilot_credentials_dir.resolve(strict=False),
                    copilot_auth.SECRETS_TARGET,
                    True,
                )
            )
    mounts.extend(parse_mount(item) for item in extra_mounts)
    return mounts


def _mount_arg(mount: MountSpec) -> str:
    if mount.raw is not None:
        return mount.raw
    fields = ["type=bind", f"source={mount.source}", f"target={mount.target}"]
    if mount.readonly:
        fields.append("readonly")
    return mount_options.encode(fields)


def node_modules_tmpfs_arg(runtime: Runtime) -> str:
    """Return the --tmpfs value that backs /workspace/node_modules at runtime.

    Docker and Podman mount --tmpfs noexec and root-owned by default, which
    would break node_modules/.bin scripts and native addons and keep the
    entrypoint (running as the agent) from populating it. Apple container
    uses a bare path; Docker and Apple container end-to-end checks were
    confirmed passing by the user (see add-node-npm-flavor change notes).
    """
    if runtime.name == APPLE_CONTAINER.name:
        return WORKSPACE_NODE_MODULES_TARGET
    if runtime.name == PODMAN.name:
        # Podman copies the bind-mounted host directory's contents into a new
        # tmpfs by default, which would leak host dependencies into the sandbox.
        return f"{WORKSPACE_NODE_MODULES_TARGET}:rw,exec,mode=1777,notmpcopyup"
    return f"{WORKSPACE_NODE_MODULES_TARGET}:rw,exec,mode=1777"


def identity_env(identity: GitIdentity) -> list[str]:
    env: list[str] = []
    if identity.name:
        env += [
            f"PROJECT_SANDBOX_USER_NAME={identity.name}",
            f"GIT_AUTHOR_NAME={identity.name}",
            f"GIT_COMMITTER_NAME={identity.name}",
        ]
    if identity.email:
        env += [
            f"PROJECT_SANDBOX_USER_EMAIL={identity.email}",
            f"GIT_AUTHOR_EMAIL={identity.email}",
            f"GIT_COMMITTER_EMAIL={identity.email}",
        ]
    return env


def build_chroot_argv(
    *,
    script: Path,
    jail_root: Path,
    mounts: Sequence[MountSpec],
    identity: GitIdentity | None = None,
    agent: str = "bash",
    extra_env: Sequence[str] = (),
) -> list[str]:
    identity = identity or GitIdentity(None, None)
    argv = [
        CHROOT.binary,
        "--map-root-user",
        "--mount",
        "--",
        str(script),
        str(jail_root),
    ]
    for mount in mounts:
        if mount.raw is not None:
            raise ValueError(f"chroot runtime only supports bind mounts: {mount.raw}")
        target = Path(mount.target)
        if not target.is_absolute() or ".." in target.parts:
            raise ValueError(
                f"Chroot mount target must be an absolute jail path: {mount.target}"
            )
        argv.extend((str(mount.source), mount.target, "ro" if mount.readonly else "rw"))
    argv.extend(("--", agent, *identity_env(identity), *extra_env))
    return argv


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
    env_file: Path | None = None,
    opencode_credentials_dir: Path | None = None,
    pi_credentials_dir: Path | None = None,
    pi_cfg: Path | None = None,
    copilot_credentials_dir: Path | None = None,
    copilot_cfg: Path | None = None,
    copilot_host: str = copilot_auth.DEFAULT_HOST,
    container_name: str | None = None,
    forward_credentials: bool = True,
    add_hosts: Sequence[str] = (),
    network: str | None = None,
    shm_size: str | None = None,
    node_modules_tmpfs: bool = False,
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
    # Rootless podman maps the host user to container root, so the image's
    # agent user (built with the host UID) would land on an unrelated subuid and
    # be unable to write the /workspace bind mount. keep-id maps the host UID to
    # itself instead. Rootful podman is refused at runtime selection; a root
    # caller has no host identity to keep.
    if runtime.name == PODMAN.name and host_build_identity(runtime) is not None:
        argv.append("--userns=keep-id")
    if shm_size is not None:
        argv += ["--shm-size", shm_size]
    if container_name:
        argv += ["--name", container_name]
    if interactive:
        argv.append("-it")
    if firewall_enabled:
        argv += ["--cap-add", "NET_ADMIN", "--cap-add", "NET_RAW"]
    if network:
        argv += ["--network", network]
    for mapping in add_hosts:
        argv += ["--add-host", mapping]
    # Generated, non-secret config (settings.json / config.toml) is always
    # mounted; the staged host tokens under /project-sandbox-secrets are only
    # forwarded when forward_credentials is set. With it off, the container
    # starts unauthenticated and the user logs in inside the sandbox.
    mounts = build_mount_specs(
        project_abs=project_abs,
        claude_cfg=claude_cfg,
        claude_credentials_dir=claude_credentials_dir,
        codex_cfg=codex_cfg,
        codex_credentials_dir=codex_credentials_dir,
        opencode_credentials_dir=opencode_credentials_dir,
        pi_credentials_dir=pi_credentials_dir,
        pi_cfg=pi_cfg,
        copilot_credentials_dir=copilot_credentials_dir,
        copilot_cfg=copilot_cfg,
        extra_mounts=extra_mounts,
        forward_credentials=forward_credentials,
        agent=agent,
    )
    for mount in mounts:
        argv += ["--mount", _mount_arg(mount)]
    # After the /workspace bind mount so the tmpfs shadows the host's
    # node_modules; the entrypoint fills it from the image.
    if node_modules_tmpfs:
        argv += ["--tmpfs", node_modules_tmpfs_arg(runtime)]
    for env in identity_env(identity):
        argv += ["--env", env]
    argv += [
        "--env",
        "CLAUDE_SECURESTORAGE_CONFIG_DIR=/home/agent/.claude",
        "--env",
        "CODEX_HOME=/home/agent/.codex",
        "--env",
        "OPENSPEC_TELEMETRY=0",
    ]
    if agent.removesuffix("-headless") == "copilot":
        for env in copilot_auth.container_env(copilot_host):
            argv += ["--env", env]
    if not firewall_enabled:
        argv += ["--env", "PROJECT_SANDBOX_NO_FIREWALL=1"]
    for env in extra_env:
        argv += ["--env", env]
    if env_file is not None:
        argv += ["--env-file", str(env_file)]
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
    build_args: Sequence[str] = (),
    preview_build_args: Sequence[str] | None = None,
    network: str | None = None,
    dry_run: bool = False,
    verbose: bool = True,
) -> int:
    """Build ``image_tag``; ``build_args`` are extra ``--build-arg`` argv.

    ``preview_build_args`` replaces ``build_args`` in the dry-run print so
    sensitive values (such as proxy URLs) can be redacted.
    """
    if not runtime.is_container:
        return 0
    build_context = (build_context or context_dir).resolve(strict=False)
    dockerfile_path = (dockerfile_path or context_dir / "Dockerfile").resolve(
        strict=False
    )
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
    cmd = [runtime.binary, "build", "-t", image_tag, "-f", file_arg]
    # Podman defaults to OCI format, which drops the Dockerfile SHELL
    # instruction; RUN steps then use /bin/sh and fail on bashisms (pipefail).
    if runtime.name == PODMAN.name:
        cmd += ["--format", "docker"]
    if network:
        cmd += ["--network", network]
    identity = host_build_identity(runtime)
    if identity is not None:
        uid, gid = identity
        cmd += [
            "--build-arg",
            f"AGENT_UID={uid}",
            "--build-arg",
            f"AGENT_GID={gid}",
        ]
    if dry_run:
        shown = [
            *cmd,
            *(build_args if preview_build_args is None else preview_build_args),
            ".",
        ]
        print(f"cd {shlex.quote(str(build_context))} && {shlex.join(shown)}")
        return 0
    cmd += [*build_args, "."]
    return _run_quietable(cmd, verbose=verbose, cwd=str(build_context))


def image_exists(runtime: Runtime, image_tag: str, *, dry_run: bool = False) -> bool:
    """Return True when ``image_tag`` already exists for ``runtime``.

    Uses ``<binary> image inspect <tag>``, verified to exit 0 for a present
    image and non-zero for an absent one on docker, podman, and Apple
    ``container`` (0.12.3). Any non-zero exit (image absent, or the subcommand
    unsupported on some future runtime) maps to False, so an inconclusive check
    triggers a rebuild rather than reusing a possibly-stale image.
    """
    if dry_run:
        return False
    try:
        proc = subprocess.run(
            [runtime.binary, "image", "inspect", image_tag],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return False
    return proc.returncode == 0


def image_identity(runtime: Runtime, reference: str) -> str:
    """Resolve a local image to its full content identity, failing closed.

    The caller holds the runtime image lock while resolving mutable tags.
    Apple reports the OCI descriptor; Docker/Podman report the image config ID.
    """
    try:
        proc = subprocess.run(
            [runtime.binary, "image", "inspect", reference],
            check=True,
            capture_output=True,
            text=True,
        )
        images = json.loads(proc.stdout)
        if not isinstance(images, list) or len(images) != 1:
            raise ValueError("expected exactly one image")
        if runtime.name == APPLE_CONTAINER.name:
            identity = images[0]["configuration"]["descriptor"]["digest"]
        else:
            identity = images[0]["Id"]
            if isinstance(identity, str) and re.fullmatch(r"[0-9a-f]{64}", identity):
                identity = "sha256:" + identity
        if not isinstance(identity, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", identity
        ):
            raise ValueError("expected a full SHA256 image identity")
        return identity
    except (
        OSError,
        subprocess.CalledProcessError,
        ValueError,
        KeyError,
        TypeError,
    ) as exc:
        raise SystemExit(
            f"Cannot determine the local identity of image {reference!r} "
            f"for {runtime.name}; refusing to launch an unpinned image."
        ) from exc


@contextmanager
def pinned_image(
    runtime: Runtime, reference: str, identity: str
) -> Generator[str, None, None]:
    """Pin a launch while the caller holds the image lock during entry.

    Every runtime gets a unique, verified alias that lives until teardown, so
    the image survives another launch moving the shared tag once the image
    lock is released. Docker's containerd image store deletes an image as soon
    as its last tag moves away, which made a concurrent launch's `docker run`
    fail with "No such image". Docker/Podman still run the content ID; the
    alias only keeps it alive. Apple cannot reliably resolve locally built
    images by digest, so it runs the alias itself.
    """
    apple = runtime.name == APPLE_CONTAINER.name
    alias = f"localhost/project-sandbox-session:{uuid.uuid4().hex}"
    try:
        rc = run([runtime.binary, "image", "tag", reference, alias])
        if rc != 0:
            raise SystemExit(f"Could not pin the {runtime.name} image for this session")
        if image_identity(runtime, alias) != identity:
            raise SystemExit(f"Pinned {runtime.name} image identity changed; aborting")
        yield alias if apple else identity
    finally:
        # Remove only our alias, never force deletion or prune shared layers.
        remove = "delete" if apple else "rm"
        if run([runtime.binary, "image", remove, alias]) != 0:
            print(f"[W] Could not remove session image alias {alias}")


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


def run(
    argv: list[str], *, dry_run: bool = False, env: dict[str, str] | None = None
) -> int:
    if dry_run:
        print(shlex.join(argv))
        return 0
    try:
        return subprocess.run(argv, check=False, env=session.merged_env(env)).returncode
    except FileNotFoundError:
        print(f"{argv[0]} CLI not found on PATH")
        return 127
