from pathlib import Path

from jinja2 import Environment, PackageLoader


def render(
    context_dir: Path,
    *,
    base_image: str | None = None,
    base_dockerfile: Path | None = None,
    build_context: Path | None = None,
    install_agents: tuple[str, ...] = ("claude", "codex", "opencode", "copilot"),
    refresh: bool = False,
) -> Path:
    if (base_image is None) == (base_dockerfile is None):
        raise ValueError("Provide exactly one of base_image or base_dockerfile")

    out = context_dir / "Dockerfile"
    if out.exists() and not refresh:
        return out
    source_dockerfile_text = ""
    if base_dockerfile is not None:
        source_dockerfile_text = (
            base_dockerfile.read_text(encoding="utf-8").rstrip() + "\n"
        )

    copy_prefix = ""
    if build_context is not None:
        copy_prefix = _sandbox_copy_prefix(
            context_dir=context_dir,
            build_context=build_context,
        )

    env = Environment(loader=PackageLoader("project_sandbox", "templates"))
    tmpl = env.get_template("Dockerfile.j2")
    out.write_text(
        tmpl.render(
            base_image=base_image,
            source_dockerfile_text=source_dockerfile_text,
            sandbox_copy_prefix=copy_prefix,
            install_claude="claude" in install_agents,
            install_codex="codex" in install_agents,
            install_opencode="opencode" in install_agents,
            install_copilot="copilot" in install_agents,
        )
        + "\n",
        encoding="utf-8",
    )
    return out


def _sandbox_copy_prefix(*, context_dir: Path, build_context: Path) -> str:
    context_resolved = context_dir.resolve(strict=False)
    build_context_resolved = build_context.resolve(strict=True)
    relative = context_resolved.relative_to(build_context_resolved)
    if str(relative) == ".":
        return ""
    return relative.as_posix().rstrip("/") + "/"


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
