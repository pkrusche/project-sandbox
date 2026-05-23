import os
from pathlib import Path

from jinja2 import Environment, PackageLoader

from . import config_claude
from .git_identity import GitIdentity


def render(
    project: Path,
    *,
    identity: GitIdentity,
    firewall_enabled: bool,
    memory: str | None,
    cpus: int | None,
    extra_mounts: list[str],
    claude_credentials_dir: Path | None = None,
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
    claude_credentials_dir = claude_credentials_dir or config_claude.credentials_dir(
        project / ".project-sandbox"
    )
    out.write_text(
        tmpl.render(
            project_name=project.name,
            dockerfile_ref=_devcontainer_ref(dc_dir, generated_dockerfile),
            build_context_ref=_devcontainer_ref(dc_dir, build_context),
            firewall_enabled=firewall_enabled,
            memory=memory,
            cpus=cpus,
            mount_codex_host=Path.home().joinpath(".codex").exists(),
            mount_opencode_host=Path.home().joinpath(".config/opencode").exists(),
            mount_copilot_host=Path.home().joinpath(".copilot").exists(),
            claude_config_mount="${localWorkspaceFolder}/.project-sandbox/claude",
            claude_credentials_mount=claude_credentials_dir.resolve(strict=False).as_posix(),
            codex_config_mount="${localWorkspaceFolder}/.project-sandbox/codex",
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


def _devcontainer_ref(dc_dir: Path, path: Path) -> str:
    return Path(
        os.path.relpath(path.resolve(strict=False), dc_dir.resolve(strict=False))
    ).as_posix()
