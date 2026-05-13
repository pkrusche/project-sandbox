from pathlib import Path

from jinja2 import Environment, PackageLoader

from .git_identity import GitIdentity


def render(
    project: Path,
    *,
    identity: GitIdentity,
    firewall_enabled: bool,
    memory: str | None,
    cpus: int | None,
    extra_mounts: list[str],
    refresh: bool = False,
) -> Path:
    dc_dir = project / ".devcontainer"
    dc_dir.mkdir(exist_ok=True)

    _symlink(dc_dir / "Dockerfile", Path("../.project-sandbox/Dockerfile"))
    _symlink(dc_dir / "init-firewall.sh", Path("../.project-sandbox/init-firewall.sh"))
    _symlink(dc_dir / "claude", Path("../.project-sandbox/claude"))
    _symlink(dc_dir / "codex", Path("../.project-sandbox/codex"))

    out = dc_dir / "devcontainer.json"
    if out.exists() and not refresh:
        return dc_dir

    env = Environment(loader=PackageLoader("project_sandbox", "templates"))
    tmpl = env.get_template("devcontainer.json.j2")
    out.write_text(
        tmpl.render(
            project_name=project.name,
            firewall_enabled=firewall_enabled,
            memory=memory,
            cpus=cpus,
            mount_claude_host=Path.home().joinpath(".claude").exists(),
            mount_codex_host=Path.home().joinpath(".codex").exists(),
            mount_opencode_host=Path.home().joinpath(".config/opencode").exists(),
            mount_copilot_host=Path.home().joinpath(".copilot").exists(),
            claude_settings_mount="${localWorkspaceFolder}/.project-sandbox/claude/settings.json",
            codex_config_mount="${localWorkspaceFolder}/.project-sandbox/codex/config.toml",
            extra_mounts=extra_mounts,
            user_name=identity.name or "",
            user_email=identity.email or "",
        )
        + "\n",
        encoding="utf-8",
    )
    return dc_dir


def _symlink(link: Path, target: Path) -> None:
    if link.exists() or link.is_symlink():
        if link.resolve() == (link.parent / target).resolve():
            return
        if link.is_dir() and not link.is_symlink():
            raise RuntimeError(f"Cannot replace directory with symlink: {link}")
        link.unlink()
    link.symlink_to(target)
