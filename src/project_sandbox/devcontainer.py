"""Render ``.devcontainer/`` for ``project-sandbox --devcontainer``.

The devcontainer is a test and debug environment with the project's
dependencies. No agent runs there: it has no agent CLIs, credentials, agent
configs or agent IDE extensions, no added capabilities, no firewall and no
root step after start. By default it has no network at all (``--network
none``); ``--devcontainer-network=runtime`` gives it the runtime's default
network, unfiltered.
"""

import json
import os
import re
from pathlib import Path

from .git_identity import GitIdentity
from .paths import (
    HISTORY_HISTFILE,
    HISTORY_SHELL_TARGET,
    WORKSPACE_DEVCONTAINER_TARGET,
    WORKSPACE_SANDBOX_TARGET,
    ensure_history_paths,
    ensure_workspace_sandbox_mask,
)

NETWORK_NONE = "none"
NETWORK_RUNTIME = "runtime"
NETWORK_MODES = (NETWORK_NONE, NETWORK_RUNTIME)

# Offline switches for the generated toolchains, so tools fail fast instead of
# timing out when the devcontainer has no network.
OFFLINE_ENVIRONMENT = {
    "UV_OFFLINE": "1",
    "CARGO_NET_OFFLINE": "true",
    "npm_config_offline": "true",
}

DOCKERFILE_NAME = "Dockerfile.devcontainer"
CONFIG_NAME = "devcontainer.json"

# Links an older project-sandbox wrote into .devcontainer/ for the removed
# firewall variant and agent profiles. Only symlinks are removed.
_STALE_LINKS = (
    "init-firewall.sh",
    "claude",
    "claude-devcontainer",
    "codex",
    "codex-devcontainer",
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
    memory: str | None,
    cpus: int | None,
    extra_mounts: list[str],
    network: str = NETWORK_NONE,
    build_context: Path | None = None,
    proxy_environment: dict[str, str] | None = None,
) -> Path:
    """Write ``.devcontainer/devcontainer.json`` and return ``.devcontainer``.

    ``proxy_environment`` (the ``--internet-proxy`` variables) applies only to
    ``network=runtime``: an offline devcontainer has nothing to proxy.
    """
    if network not in NETWORK_MODES:
        raise ValueError(f"Unknown devcontainer network mode: {network}")
    dc_dir = project / ".devcontainer"
    if dc_dir.is_symlink():
        # devcontainer.json's initializeCommand runs on the host; never write
        # it through a link that something else may have planted.
        raise RuntimeError(f"Refusing to render through a symlink: {dc_dir}")
    dc_dir.mkdir(exist_ok=True)

    _symlink(dc_dir / "Dockerfile", Path("../.project-sandbox") / DOCKERFILE_NAME)
    for name in _STALE_LINKS:
        if (dc_dir / name).is_symlink():
            (dc_dir / name).unlink()

    out = dc_dir / CONFIG_NAME
    generated_dockerfile = project / ".project-sandbox" / DOCKERFILE_NAME
    build_context = build_context or project / ".project-sandbox"

    # Persist bash history across devcontainer rebuilds. Create the host
    # directory so the bind mount has something to bind to, and reference it
    # relative to the workspace folder so the config stays portable. The
    # source is a directory: apple/container rejects single-file bind mounts,
    # so bash history is persisted via a directory mount + HISTFILE.
    ensure_history_paths(project, create=True)
    history_shell = "${localWorkspaceFolder}/.project-sandbox/history/shell"
    ensure_workspace_sandbox_mask(project, create=True)
    workspace_mask_root = "${localWorkspaceFolder}/.project-sandbox/workspace-mask"

    run_args: list[str] = []
    if network == NETWORK_NONE:
        # Only loopback, enforced by the kernel without capabilities or a
        # root step.
        run_args.extend(["--network", "none"])
    if memory:
        run_args.append(f"--memory={memory}")
    if cpus:
        run_args.append(f"--cpus={cpus}")

    container_env = {
        "NODE_OPTIONS": "--max-old-space-size=4096",
        "HISTFILE": HISTORY_HISTFILE,
    }
    if network == NETWORK_NONE:
        container_env.update(OFFLINE_ENVIRONMENT)
    else:
        container_env.update(proxy_environment or {})

    # Build the config as a Python dict and emit it with json.dumps so every
    # interpolated value (project name, git identity, mount specs, including the
    # user-supplied --mount values in extra_mounts) is JSON-escaped. Rendering
    # these directly into a template produced no escaping, letting a crafted
    # value close a string and inject arbitrary devcontainer fields.
    mounts = [
        f"source={history_shell},target={HISTORY_SHELL_TARGET},type=bind",
        *extra_mounts,
        # Keep the masks after user-supplied mounts so a writable custom mount
        # cannot expose generated files inside the workspace. Test code may
        # come from an agent session and must not rewrite devcontainer.json,
        # whose initializeCommand runs on the host.
        f"source={workspace_mask_root},target={WORKSPACE_SANDBOX_TARGET},type=bind,readonly",
        f"source={workspace_mask_root},target={WORKSPACE_DEVCONTAINER_TARGET},type=bind,readonly",
    ]

    config: dict = {
        "name": f"{project.name} (project-sandbox)",
        "build": {
            "dockerfile": _devcontainer_ref(dc_dir, generated_dockerfile),
            "context": _devcontainer_ref(dc_dir, build_context),
            # No build proxy args: this file sits inside whole-project build
            # contexts, so values here would be copied into image layers.
        },
        "runArgs": run_args,
        "workspaceMount": "source=${localWorkspaceFolder},target=/workspace,type=bind,consistency=delegated",
        "workspaceFolder": "/workspace",
        # The history and mask directories live in the gitignored
        # .project-sandbox and can vanish before "Reopen in Container" (fresh
        # clone, cleaned directory); a missing bind source makes the runtime
        # fail to start. initializeCommand runs on the host before the
        # container is created and recreates them. The argv (array) form
        # passes each path as one literal argument, with no shell parsing.
        "initializeCommand": ["mkdir", "-p", history_shell, workspace_mask_root],
        "remoteUser": "agent",
        "containerEnv": container_env,
        "remoteEnv": {
            "PROJECT_SANDBOX_USER_NAME": identity.name or "",
            "PROJECT_SANDBOX_USER_EMAIL": identity.email or "",
        },
        "mounts": mounts,
        # Runs as remoteUser: applies the git/jj identity, nothing privileged.
        "postStartCommand": "/usr/local/bin/project-sandbox-devcontainer-init",
        "waitFor": "postStartCommand",
        "customizations": {
            "vscode": {
                "settings": {
                    "terminal.integrated.defaultProfile.linux": "bash",
                },
            }
        },
        "features": {},
    }
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
