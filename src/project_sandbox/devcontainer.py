import os
from pathlib import Path

from jinja2 import Environment, PackageLoader

from . import config_agents
from .git_identity import GitIdentity


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

    _symlink(dc_dir / "Dockerfile", Path("../.project-sandbox/Dockerfile"))
    _symlink(dc_dir / "init-firewall.sh", Path("../.project-sandbox/init-firewall.sh"))
    _symlink(dc_dir / "claude", Path("../.project-sandbox/claude"))
    _symlink(dc_dir / "codex", Path("../.project-sandbox/codex"))

    out = dc_dir / "devcontainer.json"
    env = Environment(loader=PackageLoader("project_sandbox", "templates"))
    tmpl = env.get_template("devcontainer.json.j2")
    generated_dockerfile = project / ".project-sandbox" / "Dockerfile"
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
    claude_credentials_dir = credential_dirs.get(
        "claude", config_agents.credentials_dir(project / ".project-sandbox")
    )
    out.write_text(
        tmpl.render(
            project_name=project.name,
            dockerfile_ref=_devcontainer_ref(dc_dir, generated_dockerfile),
            build_context_ref=_devcontainer_ref(dc_dir, build_context),
            firewall_enabled=firewall_enabled,
            memory=memory,
            cpus=cpus,
            mount_codex_secrets=mount_codex_secrets,
            mount_opencode_secrets=mount_opencode_secrets,
            claude_config_mount="${localWorkspaceFolder}/.project-sandbox/claude",
            claude_credentials_mount=claude_credentials_dir.resolve(strict=False).as_posix(),
            codex_config_mount="${localWorkspaceFolder}/.project-sandbox/codex",
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
