import os
import re
from pathlib import Path

from . import config_agents, templating
from .git_identity import GitIdentity
from .paths import (
    HISTORY_CLAUDE_PROJECTS_TARGET,
    HISTORY_HISTFILE,
    HISTORY_SHELL_TARGET,
    ensure_history_paths,
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
    build_context: Path | None = None,
) -> Path:
    dc_dir = project / ".devcontainer"
    dc_dir.mkdir(exist_ok=True)

    _symlink(dc_dir / "Dockerfile", Path("../.project-sandbox/Dockerfile.devcontainer"))
    _symlink(dc_dir / "init-firewall.sh", Path("../.project-sandbox/init-firewall-devcontainer.sh"))
    _symlink(dc_dir / "claude", Path("../.project-sandbox/claude"))
    _symlink(dc_dir / "claude-devcontainer", Path("../.project-sandbox/claude-devcontainer"))
    _symlink(dc_dir / "codex", Path("../.project-sandbox/codex"))
    _symlink(dc_dir / "codex-devcontainer", Path("../.project-sandbox/codex-devcontainer"))

    out = dc_dir / "devcontainer.json"
    tmpl = templating.get_template("devcontainer.json.j2")
    generated_dockerfile = project / ".project-sandbox" / "Dockerfile.devcontainer"
    build_context = build_context or project / ".project-sandbox"
    use_provided_credential_dirs = credential_dirs is not None
    credential_dirs = credential_dirs or _credential_dirs(project / ".project-sandbox")
    host_home = Path.home()
    if use_provided_credential_dirs:
        mount_codex_secrets = "codex" in credential_dirs
        mount_opencode_secrets = "opencode" in credential_dirs
    else:
        mount_codex_secrets = host_home.joinpath(".codex").exists()
        mount_opencode_secrets = host_home.joinpath(".config/opencode").exists()
    claude_devcontainer_credentials_dir = credential_dirs.get(
        "claude-devcontainer",
        config_agents.credentials_dir(project / ".project-sandbox", "claude-devcontainer"),
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
    # The history dir is gitignored and ephemeral, so the bind sources can be
    # absent at container-create time (fresh clone, cleaned .project-sandbox, or
    # an upgrade that added history mounts). A missing source makes the runtime
    # fail to start. initializeCommand runs on the host before the container is
    # created, recreating the directories so the mounts always succeed.
    history_init_command = (
        f"mkdir -p '{history_root}/shell' '{history_root}/claude_projects'"
    )
    out.write_text(
        tmpl.render(
            project_name=project.name,
            dockerfile_ref=_devcontainer_ref(dc_dir, generated_dockerfile),
            build_context_ref=_devcontainer_ref(dc_dir, build_context),
            firewall_enabled=firewall_enabled,
            memory=memory,
            memory_hostreq=_host_memory(memory),
            cpus=cpus,
            mount_codex_secrets=mount_codex_secrets,
            mount_opencode_secrets=mount_opencode_secrets,
            claude_config_mount="${localWorkspaceFolder}/.project-sandbox/claude-devcontainer",
            claude_credentials_mount=claude_devcontainer_credentials_dir.resolve(strict=False).as_posix(),
            codex_config_mount="${localWorkspaceFolder}/.project-sandbox/codex-devcontainer",
            codex_credentials_mount=credential_dirs.get(
                "codex",
                config_agents.credentials_dir(project / ".project-sandbox", "codex"),
            )
            .resolve(strict=False)
            .as_posix(),
            opencode_credentials_mount=credential_dirs.get(
                "opencode",
                config_agents.credentials_dir(project / ".project-sandbox", "opencode"),
            )
            .resolve(strict=False)
            .as_posix(),
            history_mounts=history_mounts,
            history_init_command=history_init_command,
            history_histfile=HISTORY_HISTFILE,
            extra_mounts=extra_mounts,
            user_name=identity.name or "",
            user_email=identity.email or "",
        )
        + "\n",
        encoding="utf-8",
    )
    return dc_dir


def _credential_dirs(context_dir: Path) -> dict[str, Path]:
    return {
        agent: config_agents.credentials_dir(context_dir, agent)
        for agent in ("claude", "codex", "opencode")
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
