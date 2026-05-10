from pathlib import Path

from jinja2 import Environment, PackageLoader

from .git_identity import GitIdentity


def render(
    project: Path,
    *,
    identity: GitIdentity,
    install_claude: bool,
    install_codex: bool,
    memory: str | None,
    cpus: int | None,
    ro_creds: bool,
    extra_mounts: list[str],
    refresh: bool = False,
) -> Path:
    dc_dir = project / ".devcontainer"
    dc_dir.mkdir(exist_ok=True)

    _symlink(dc_dir / "Dockerfile", Path("../.project-sandbox/Dockerfile"))
    _symlink(dc_dir / "init-firewall.sh", Path("../.project-sandbox/init-firewall.sh"))

    out = dc_dir / "devcontainer.json"
    if out.exists() and not refresh:
        return dc_dir

    env = Environment(loader=PackageLoader("project_sandbox", "templates"))
    tmpl = env.get_template("devcontainer.json.j2")
    out.write_text(
        tmpl.render(
            project_name=project.name,
            install_claude=install_claude,
            install_codex=install_codex,
            memory=memory,
            cpus=cpus,
            ro_creds=ro_creds,
            mount_claude_host=Path.home().joinpath(".claude").exists(),
            mount_codex_host=Path.home().joinpath(".codex").exists(),
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
        link.unlink()
    link.symlink_to(target)
