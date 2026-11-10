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
import stat
from pathlib import Path

from . import paths
from .git_identity import GitIdentity
from .paths import (
    HISTORY_HISTFILE,
    HISTORY_SHELL_TARGET,
    WORKSPACE_DEVCONTAINER_TARGET,
    WORKSPACE_SANDBOX_TARGET,
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

DIRECTORY = ".devcontainer"
DOCKERFILE_NAME = "Dockerfile"
CONFIG_NAME = "devcontainer.json"
GITIGNORE = ".gitignore"
# The directory ignores itself, so nothing is added to the root .gitignore.
GITIGNORE_TEXT = "*\n"

# Links an older project-sandbox wrote into .devcontainer/ for the removed
# firewall variant and agent profiles. Only symlinks are removed.
_STALE_LINKS = (
    "init-firewall.sh",
    "claude",
    "claude-devcontainer",
    "codex",
    "codex-devcontainer",
)


def open_directory(project: Path) -> "paths.PrivateDir":
    """Open (creating) ``<project>/.devcontainer``; a symlink is refused.

    Masking the link's path inside a sandbox would leave the link's target
    writable, and rendering through it would write wherever it points.
    """
    if (project / DIRECTORY).is_symlink():
        raise paths.UnsafePathError(
            f"Refusing {project / DIRECTORY}: it is a symlink, whose target would "
            "stay writable from a sandbox. Replace it with a directory."
        )
    return paths.open_in_project(project, DIRECTORY)


def ensure_ignored(directory: "paths.PrivateDir") -> None:
    """Write ``.gitignore`` (``*``) unless the directory already has one."""
    if directory.lstat(GITIGNORE) is None:
        directory.write(GITIGNORE, GITIGNORE_TEXT)


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
    history_shell: Path,
    mask_dir: Path,
    network: str = NETWORK_NONE,
    build_context: Path | None = None,
    proxy_environment: dict[str, str] | None = None,
    directory: "paths.PrivateDir | None" = None,
) -> Path:
    """Write ``.devcontainer/devcontainer.json`` and return ``.devcontainer``.

    ``history_shell`` and ``mask_dir`` are private host directories in the
    project's state (persisted bash history; an empty directory masking
    ``.devcontainer`` and a legacy ``.project-sandbox``). The Dockerfile and
    its inputs are rendered into ``.devcontainer/`` by the caller.

    ``proxy_environment`` (the ``--internet-proxy`` variables) applies only to
    ``network=runtime``: an offline devcontainer has nothing to proxy.
    """
    if network not in NETWORK_MODES:
        raise ValueError(f"Unknown devcontainer network mode: {network}")
    dc_dir = project / DIRECTORY
    owned = directory is None
    dc = open_directory(project) if directory is None else directory
    try:
        _write_config(
            dc,
            project=project,
            dc_dir=dc_dir,
            identity=identity,
            memory=memory,
            cpus=cpus,
            extra_mounts=extra_mounts,
            history_shell=history_shell,
            mask_dir=mask_dir,
            network=network,
            build_context=build_context or dc_dir,
            proxy_environment=proxy_environment,
        )
    finally:
        if owned:
            dc.close()
    return dc_dir


def _write_config(
    dc: "paths.PrivateDir",
    *,
    project: Path,
    dc_dir: Path,
    identity: GitIdentity,
    memory: str | None,
    cpus: int | None,
    extra_mounts: list[str],
    history_shell: Path,
    mask_dir: Path,
    network: str,
    build_context: Path,
    proxy_environment: dict[str, str] | None,
) -> None:
    ensure_ignored(dc)
    for name in _STALE_LINKS:
        st = dc.lstat(name)
        if st is not None and stat.S_ISLNK(st.st_mode):
            dc.unlink(name)
    # The masks need an existing bind target inside the workspace: a missing
    # one would be created (as root on some runtimes) in the project.
    mask_project_sandbox = (project / ".project-sandbox").is_dir() and not (
        project / ".project-sandbox"
    ).is_symlink()

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
        *(
            [f"source={mask_dir},target={WORKSPACE_SANDBOX_TARGET},type=bind,readonly"]
            if mask_project_sandbox
            else []
        ),
        f"source={mask_dir},target={WORKSPACE_DEVCONTAINER_TARGET},type=bind,readonly",
    ]

    config: dict = {
        "name": f"{project.name} (project-sandbox)",
        "build": {
            "dockerfile": DOCKERFILE_NAME,
            "context": _devcontainer_ref(dc_dir, build_context),
            # No build proxy args: this file sits inside whole-project build
            # contexts, so values here would be copied into image layers.
        },
        "runArgs": run_args,
        "workspaceMount": "source=${localWorkspaceFolder},target=/workspace,type=bind,consistency=delegated",
        "workspaceFolder": "/workspace",
        # The history and mask directories live in private host state, which
        # a user may clear before "Reopen in Container"; a missing bind source
        # makes the runtime fail to start. initializeCommand runs on the host
        # before the container is created and recreates them, private. The
        # argv (array) form passes each path as one literal argument, with no
        # shell parsing.
        "initializeCommand": [
            "mkdir",
            "-p",
            "-m",
            "700",
            str(history_shell),
            str(mask_dir),
        ],
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

    dc.write(CONFIG_NAME, json.dumps(config, indent=2) + "\n")


def _devcontainer_ref(dc_dir: Path, path: Path) -> str:
    return Path(
        os.path.relpath(path.resolve(strict=False), dc_dir.resolve(strict=False))
    ).as_posix()
