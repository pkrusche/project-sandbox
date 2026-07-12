import json
import os
import re
from pathlib import Path

from . import config_agents
from .git_identity import GitIdentity
from .paths import (
    HISTORY_CLAUDE_PROJECTS_TARGET,
    HISTORY_HISTFILE,
    HISTORY_SHELL_TARGET,
    WORKSPACE_DEVCONTAINER_TARGET,
    WORKSPACE_SANDBOX_TARGET,
    ensure_history_paths,
    ensure_workspace_sandbox_mask,
)

_MEMORY_RE = re.compile(r"(\d+)\s*([gm])b?", re.IGNORECASE)


def _host_memory(memory: str | None) -> str | None:
    """Normalize a container memory string (e.g. "8g", "512m", "8gb") to the
    devcontainer hostRequirements form ("8gb", "512mb"). Returns None when the
    value is empty or not in a recognized unit, so the field can be omitted."""
    if not memory:
        return None
    match = _MEMORY_RE.fullmatch(memory.strip())
    if match is None:
        return None
    value, unit = match.groups()
    return f"{value}{unit.lower()}b"


def render(
    project: Path,
    *,
    identity: GitIdentity,
    firewall_enabled: bool,
    memory: str | None,
    cpus: int | None,
    extra_mounts: list[str],
    credential_dirs: dict[str, Path] | None = None,
    forward_credentials: bool = True,
    build_context: Path | None = None,
) -> Path:
    dc_dir = project / ".devcontainer"
    dc_dir.mkdir(exist_ok=True)

    _symlink(dc_dir / "Dockerfile", Path("../.project-sandbox/Dockerfile.devcontainer"))
    _symlink(
        dc_dir / "init-firewall.sh",
        Path("../.project-sandbox/init-firewall-devcontainer.sh"),
    )
    _symlink(dc_dir / "claude", Path("../.project-sandbox/claude"))
    _symlink(
        dc_dir / "claude-devcontainer", Path("../.project-sandbox/claude-devcontainer")
    )
    _symlink(dc_dir / "codex", Path("../.project-sandbox/codex"))
    _symlink(
        dc_dir / "codex-devcontainer", Path("../.project-sandbox/codex-devcontainer")
    )

    out = dc_dir / "devcontainer.json"
    generated_dockerfile = project / ".project-sandbox" / "Dockerfile.devcontainer"
    build_context = build_context or project / ".project-sandbox"
    use_provided_credential_dirs = credential_dirs is not None
    credential_dirs = credential_dirs or _credential_dirs(project / ".project-sandbox")
    host_home = Path.home()
    if use_provided_credential_dirs:
        mount_codex_secrets = "codex" in credential_dirs
        mount_opencode_secrets = "opencode" in credential_dirs
        mount_pi_secrets = "pi" in credential_dirs
    else:
        mount_codex_secrets = host_home.joinpath(".codex").exists()
        mount_opencode_secrets = host_home.joinpath(".config/opencode").exists()
        mount_pi_secrets = host_home.joinpath(".pi/agent").exists()
    claude_devcontainer_credentials_dir = credential_dirs.get(
        "claude-devcontainer",
        config_agents.credentials_dir(
            project / ".project-sandbox", "claude-devcontainer"
        ),
    )
    # Persist bash and Claude session history across devcontainer rebuilds, the
    # same way the interactive CLI run path does. Create the host files so the
    # bind mounts have something to bind to, and reference them relative to the
    # workspace folder so the config stays portable.
    ensure_history_paths(project, create=True)
    history_root = "${localWorkspaceFolder}/.project-sandbox/history"
    # Both sources are directories: apple/container rejects single-file bind
    # mounts ("is not a directory"), so bash history is persisted via a directory
    # mount + HISTFILE rather than mounting ~/.bash_history.
    history_mounts = [
        f"source={history_root}/shell,target={HISTORY_SHELL_TARGET},type=bind",
        f"source={history_root}/claude_projects,target={HISTORY_CLAUDE_PROJECTS_TARGET},type=bind",
    ]
    ensure_workspace_sandbox_mask(project, create=True)
    workspace_mask_root = "${localWorkspaceFolder}/.project-sandbox/workspace-mask"
    workspace_mask_mount = f"source={workspace_mask_root},target={WORKSPACE_SANDBOX_TARGET},type=bind,readonly"
    # Mask .devcontainer the same way so its host-path mounts and config are not
    # visible from inside the running devcontainer.
    devcontainer_mask_mount = f"source={workspace_mask_root},target={WORKSPACE_DEVCONTAINER_TARGET},type=bind,readonly"
    # Build the config as a Python dict and emit it with json.dumps so every
    # interpolated value (project name, git identity, mount specs, including the
    # user-supplied --mount values in extra_mounts) is JSON-escaped. Rendering
    # these directly into a template produced no escaping, letting a crafted
    # value close a string and inject arbitrary devcontainer fields.
    claude_config_mount = "${localWorkspaceFolder}/.project-sandbox/claude-devcontainer"
    claude_credentials_mount = claude_devcontainer_credentials_dir.resolve(
        strict=False
    ).as_posix()
    codex_config_mount = "${localWorkspaceFolder}/.project-sandbox/codex-devcontainer"
    codex_credentials_mount = (
        credential_dirs.get(
            "codex",
            config_agents.credentials_dir(project / ".project-sandbox", "codex"),
        )
        .resolve(strict=False)
        .as_posix()
    )
    opencode_credentials_mount = (
        credential_dirs.get(
            "opencode",
            config_agents.credentials_dir(project / ".project-sandbox", "opencode"),
        )
        .resolve(strict=False)
        .as_posix()
    )
    pi_credentials_mount = (
        credential_dirs.get(
            "pi",
            config_agents.credentials_dir(project / ".project-sandbox", "pi"),
        )
        .resolve(strict=False)
        .as_posix()
    )

    run_args: list[str] = []
    if firewall_enabled:
        run_args.append("--cap-add=NET_ADMIN")
        run_args.append("--cap-add=NET_RAW")
    if memory:
        run_args.append(f"--memory={memory}")
    if cpus:
        run_args.append(f"--cpus={cpus}")

    container_env = {
        "CLAUDE_SECURESTORAGE_CONFIG_DIR": "/home/agent/.claude",
        "CODEX_HOME": "/home/agent/.codex",
        "NODE_OPTIONS": "--max-old-space-size=4096",
        "PI_SKIP_VERSION_CHECK": "1",
        "PI_OFFLINE": "1",
    }
    if firewall_enabled:
        container_env["UV_OFFLINE"] = "1"
    container_env["HISTFILE"] = HISTORY_HISTFILE

    # Generated, non-secret config is always mounted; the staged host tokens
    # under /project-sandbox-secrets are only wired when forwarding credentials.
    # With it off the devcontainer starts unauthenticated, matching a direct run
    # launched with --no-forward-credentials.
    mounts = [
        f"source={claude_config_mount},target=/project-sandbox-config/claude,type=bind,readonly",
        f"source={codex_config_mount},target=/project-sandbox-config/codex,type=bind,readonly",
    ]
    if forward_credentials:
        mounts.append(
            f"source={claude_credentials_mount},target=/project-sandbox-secrets/claude,type=bind,readonly"
        )
        if mount_codex_secrets:
            mounts.append(
                f"source={codex_credentials_mount},target=/project-sandbox-secrets/codex,type=bind,readonly"
            )
        if mount_opencode_secrets:
            mounts.append(
                f"source={opencode_credentials_mount},target=/project-sandbox-secrets/opencode,type=bind,readonly"
            )
        if mount_pi_secrets:
            mounts.append(
                f"source={pi_credentials_mount},target=/project-sandbox-secrets/pi,type=bind,readonly"
            )
    mounts.extend(history_mounts)
    mounts.extend(extra_mounts)
    # Keep this after user-supplied mounts so a writable custom mount cannot
    # expose generated sandbox files inside the workspace.
    mounts.append(workspace_mask_mount)
    mounts.append(devcontainer_mask_mount)

    # Several bind-mount sources live under ephemeral host directories: the
    # gitignored history dir under the workspace, and the staged credential
    # dirs under /tmp (see config_agents.CREDENTIALS_ROOT). Both can vanish
    # between "project-sandbox run" and "Reopen in Container" (fresh clone,
    # cleaned .project-sandbox, or a host reboot/tmp-reaper clearing /tmp), and
    # a missing bind source makes the runtime fail to start. initializeCommand
    # runs on the host before the container is created, recreating all of
    # these directories so the mounts always succeed; self-healed credential
    # directories come back empty (unauthenticated) until the CLI is re-run to
    # restage real content. Use the argv (array) form so each path is passed
    # as a single literal argument with no shell parsing: a workspace path
    # containing an apostrophe (or any shell metacharacter) is then safe and
    # cannot create the wrong directories.
    initialize_command_dirs = [
        f"{history_root}/shell",
        f"{history_root}/claude_projects",
        workspace_mask_root,
    ]
    if forward_credentials:
        initialize_command_dirs.append(str(claude_credentials_mount))
        if mount_codex_secrets:
            initialize_command_dirs.append(str(codex_credentials_mount))
        if mount_opencode_secrets:
            initialize_command_dirs.append(str(opencode_credentials_mount))
        if mount_pi_secrets:
            initialize_command_dirs.append(str(pi_credentials_mount))
    initialize_command = ["mkdir", "-p", *initialize_command_dirs]

    post_start_command = (
        "sudo -n /usr/local/bin/project-sandbox-init-firewall && "
        if firewall_enabled
        else ""
    ) + "/usr/local/bin/project-sandbox-devcontainer-init"

    config: dict = {
        "name": f"{project.name} (project-sandbox)",
        "build": {
            "dockerfile": _devcontainer_ref(dc_dir, generated_dockerfile),
            "context": _devcontainer_ref(dc_dir, build_context),
        },
        "runArgs": run_args,
        "workspaceMount": "source=${localWorkspaceFolder},target=/workspace,type=bind,consistency=delegated",
        "workspaceFolder": "/workspace",
    }
    config["initializeCommand"] = initialize_command
    config["remoteUser"] = "agent"
    config["containerEnv"] = container_env
    config["remoteEnv"] = {
        "PROJECT_SANDBOX_USER_NAME": identity.name or "",
        "PROJECT_SANDBOX_USER_EMAIL": identity.email or "",
    }
    config["mounts"] = mounts
    config["postStartCommand"] = post_start_command
    config["waitFor"] = "postStartCommand"
    config["customizations"] = {
        "vscode": {
            "extensions": [
                "anthropic.claude-code",
                "openai.codex-vscode",
            ],
            "settings": {
                "terminal.integrated.defaultProfile.linux": "bash",
            },
        }
    }
    config["features"] = {}
    memory_hostreq = _host_memory(memory)
    if cpus or memory_hostreq:
        host_requirements: dict = {}
        if cpus:
            host_requirements["cpus"] = cpus
        if memory_hostreq:
            host_requirements["memory"] = memory_hostreq
        config["hostRequirements"] = host_requirements

    out.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return dc_dir


def _credential_dirs(context_dir: Path) -> dict[str, Path]:
    return {
        agent: config_agents.credentials_dir(context_dir, agent)
        for agent in ("claude", "codex", "opencode", "pi")
    }


def _symlink(link: Path, target: Path) -> None:
    if link.exists() or link.is_symlink():
        if link.resolve() == (link.parent / target).resolve():
            return
        if link.is_dir() and not link.is_symlink():
            raise RuntimeError(f"Cannot replace directory with symlink: {link}")
        link.unlink()
    link.symlink_to(target)


def _devcontainer_ref(dc_dir: Path, path: Path) -> str:
    return Path(
        os.path.relpath(path.resolve(strict=False), dc_dir.resolve(strict=False))
    ).as_posix()
