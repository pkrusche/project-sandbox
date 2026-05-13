from pathlib import Path

from jinja2 import Environment, PackageLoader


def render(
    context_dir: Path,
    *,
    base_image: str,
    install_agents: tuple[str, ...] = ("claude", "codex", "opencode", "copilot"),
    refresh: bool = False,
) -> Path:
    out = context_dir / "Dockerfile"
    if out.exists() and not refresh:
        return out
    env = Environment(loader=PackageLoader("project_sandbox", "templates"))
    tmpl = env.get_template("Dockerfile.j2")
    out.write_text(
        tmpl.render(
            base_image=base_image,
            install_claude="claude" in install_agents,
            install_codex="codex" in install_agents,
            install_opencode="opencode" in install_agents,
            install_copilot="copilot" in install_agents,
        )
        + "\n",
        encoding="utf-8",
    )
    return out


def render_entrypoint(context_dir: Path, *, refresh: bool = False) -> Path:
    out = context_dir / "entrypoint.sh"
    if out.exists() and not refresh:
        return out
    env = Environment(loader=PackageLoader("project_sandbox", "templates"))
    tmpl = env.get_template("entrypoint.sh.j2")
    out.write_text(tmpl.render() + "\n", encoding="utf-8")
    out.chmod(0o755)
    return out


def render_devcontainer_entrypoint(context_dir: Path, *, refresh: bool = False) -> Path:
    out = context_dir / "project-sandbox-devcontainer-init"
    if out.exists() and not refresh:
        return out
    env = Environment(loader=PackageLoader("project_sandbox", "templates"))
    tmpl = env.get_template("devcontainer-entrypoint.sh.j2")
    out.write_text(tmpl.render() + "\n", encoding="utf-8")
    out.chmod(0o755)
    return out
