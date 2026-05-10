from pathlib import Path

from jinja2 import Environment, PackageLoader


def render(
    context_dir: Path,
    *,
    base_image: str,
    install_claude: bool,
    install_codex: bool,
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
            install_claude=install_claude,
            install_codex=install_codex,
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
