import re
from collections.abc import Callable
from pathlib import Path

from jinja2 import Environment, PackageLoader

_USER_SETUP_COMMAND_RE = re.compile(
    r"(?<![\w.-])(addgroup|adduser|groupadd|groupmod|useradd|usermod)(?![\w.-])"
)


def render(
    context_dir: Path,
    *,
    base_image: str | None = None,
    base_dockerfile: Path | None = None,
    build_context: Path | None = None,
    install_agents: tuple[str, ...] = ("claude", "codex", "opencode", "copilot"),
    warn: Callable[[str], None] | None = None,
) -> Path:
    if (base_image is None) == (base_dockerfile is None):
        raise ValueError("Provide exactly one of base_image or base_dockerfile")

    out = context_dir / "Dockerfile"
    source_dockerfile_text = ""
    if base_dockerfile is not None:
        source_dockerfile_text, warnings = _read_source_dockerfile(base_dockerfile)
        if warn is not None:
            for warning in warnings:
                warn(warning)

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


def source_warnings(base_dockerfile: Path) -> tuple[str, ...]:
    _, warnings = _read_source_dockerfile(base_dockerfile)
    return warnings


def _read_source_dockerfile(base_dockerfile: Path) -> tuple[str, tuple[str, ...]]:
    text = base_dockerfile.read_text(encoding="utf-8")
    sanitized, removed = _remove_restricted_user_setup(text)
    warnings = ()
    if removed:
        suffix = "s" if removed != 1 else ""
        warnings = (
            "WARNING: Removed "
            f"{removed} restricted user setup instruction{suffix} from {base_dockerfile}; "
            "project-sandbox will create its own agent user with UID 1000.",
        )
    return sanitized.rstrip() + "\n", warnings


def _remove_restricted_user_setup(text: str) -> tuple[str, int]:
    kept: list[str] = []
    removed = 0
    for block in _dockerfile_blocks(text):
        if _is_restricted_user_setup(block):
            removed += 1
            continue
        kept.extend(block)
    return "\n".join(kept), removed


def _dockerfile_blocks(text: str) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        if not current and (not stripped or stripped.startswith("#")):
            blocks.append([line])
            continue

        current.append(line)
        if not _continues(line):
            blocks.append(current)
            current = []

    if current:
        blocks.append(current)
    return blocks


def _continues(line: str) -> bool:
    stripped = line.rstrip()
    return stripped.endswith("\\") and not stripped.endswith("\\\\")


def _is_restricted_user_setup(block: list[str]) -> bool:
    if not block:
        return False
    match = re.match(r"\s*([A-Za-z]+)\b(.*)", block[0])
    if match is None:
        return False

    instruction = match.group(1).upper()
    value = match.group(2).strip()
    if instruction == "USER":
        return value.lower() not in {"0", "root"}
    if instruction == "RUN":
        command = " ".join(line.strip().rstrip("\\") for line in block)
        return _USER_SETUP_COMMAND_RE.search(command) is not None
    return False


def _sandbox_copy_prefix(*, context_dir: Path, build_context: Path) -> str:
    context_resolved = context_dir.resolve(strict=False)
    build_context_resolved = build_context.resolve(strict=True)
    relative = context_resolved.relative_to(build_context_resolved)
    if str(relative) == ".":
        return ""
    return relative.as_posix().rstrip("/") + "/"


def render_entrypoint(context_dir: Path) -> Path:
    out = context_dir / "entrypoint.sh"
    env = Environment(loader=PackageLoader("project_sandbox", "templates"))
    tmpl = env.get_template("entrypoint.sh.j2")
    out.write_text(tmpl.render() + "\n", encoding="utf-8")
    out.chmod(0o755)
    return out


def render_devcontainer_entrypoint(context_dir: Path) -> Path:
    out = context_dir / "project-sandbox-devcontainer-init"
    env = Environment(loader=PackageLoader("project_sandbox", "templates"))
    tmpl = env.get_template("devcontainer-entrypoint.sh.j2")
    out.write_text(tmpl.render() + "\n", encoding="utf-8")
    out.chmod(0o755)
    return out
