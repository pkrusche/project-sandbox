import re
from collections.abc import Callable
from pathlib import Path

from . import templating

_USER_SETUP_COMMAND_RE = re.compile(
    r"(?<![\w.-])(addgroup|adduser|groupadd|groupmod|useradd|usermod)(?![\w.-])"
)

_NON_APT_IMAGE_FRAGMENTS = (
    "alpine", "scratch", "distroless", "centos", "rhel", "fedora",
    "rocky", "almalinux", "opensuse", "suse", "busybox", "wolfi", "chainguard",
)

_LOCAL_INSTALL_RE = re.compile(
    r"uv\s+sync"
    r"|pip3?\s+install\s+(-e\s+)?\."
    r"|poetry\s+install"
    r"|pipenv\s+install"
    r"|npm\s+install(?!\s+(-g|--global)\b)"
    r"|yarn\s+install"
    r"|pnpm\s+install",
    re.IGNORECASE,
)


def render(
    context_dir: Path,
    *,
    base_image: str | None = None,
    base_dockerfile: Path | None = None,
    build_context: Path | None = None,
    install_agents: tuple[str, ...] = ("claude", "codex", "opencode"),
    warn: Callable[[str], None] | None = None,
) -> Path:
    if (base_image is None) == (base_dockerfile is None):
        raise ValueError("Provide exactly one of base_image or base_dockerfile")

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

    tmpl = templating.get_template("Dockerfile.j2")
    shared = dict(
        base_image=base_image,
        source_dockerfile_text=source_dockerfile_text,
        sandbox_copy_prefix=copy_prefix,
        install_claude="claude" in install_agents,
        install_codex="codex" in install_agents,
        install_opencode="opencode" in install_agents,
    )
    container = _write_dockerfile(tmpl, context_dir / "Dockerfile", **shared, firewall_src_filename="init-firewall.sh")
    _write_dockerfile(tmpl, context_dir / "Dockerfile.devcontainer", **shared, firewall_src_filename="init-firewall-devcontainer.sh")
    return container


def _write_dockerfile(tmpl, out: Path, *, firewall_src_filename: str, **kwargs) -> Path:
    out.write_text(tmpl.render(firewall_src_filename=firewall_src_filename, **kwargs) + "\n", encoding="utf-8")
    return out


def source_warnings(base_dockerfile: Path) -> tuple[str, ...]:
    _, warnings = _read_source_dockerfile(base_dockerfile)
    return warnings


def _extract_last_from(blocks: list[list[str]]) -> str | None:
    result = None
    for block in blocks:
        if block:
            m = re.match(r"\s*FROM\s+(\S+)", block[0], re.IGNORECASE)
            if m:
                result = m.group(1)
    return result


def _is_non_apt_image(image: str) -> bool:
    lower = image.lower()
    return any(frag in lower for frag in _NON_APT_IMAGE_FRAGMENTS)


def _extract_final_workdir(blocks: list[list[str]]) -> str | None:
    result = None
    for block in blocks:
        if block:
            m = re.match(r"\s*WORKDIR\s+(\S+)", block[0], re.IGNORECASE)
            if m:
                result = m.group(1)
    return result


def _has_local_install_commands(blocks: list[list[str]]) -> bool:
    for block in blocks:
        if block and re.match(r"\s*RUN\b", block[0], re.IGNORECASE):
            command = " ".join(line.strip().rstrip("\\") for line in block)
            if _LOCAL_INSTALL_RE.search(command):
                return True
    return False


def _read_source_dockerfile(base_dockerfile: Path) -> tuple[str, tuple[str, ...]]:
    text = base_dockerfile.read_text(encoding="utf-8")
    sanitized, removed = _remove_restricted_user_setup(text)
    warnings: list[str] = []
    if removed:
        suffix = "s" if removed != 1 else ""
        warnings.append(
            "WARNING: Removed "
            f"{removed} restricted user setup instruction{suffix} from {base_dockerfile}; "
            "project-sandbox will create its own agent user with UID 1000."
        )
    blocks = _dockerfile_blocks(text)
    base = _extract_last_from(blocks)
    if base is not None and _is_non_apt_image(base):
        warnings.append(
            f"WARNING: Base image '{base}' may not support apt-get; "
            "project-sandbox requires a Debian/Ubuntu-based image."
        )
    workdir = _extract_final_workdir(blocks)
    if workdir and workdir != "/workspace" and _has_local_install_commands(blocks):
        warnings.append(
            f"WARNING: WORKDIR is set to '{workdir}' but the agent runs in /workspace; "
            "packages installed during the image build will not be accessible. "
            "Remove install steps from the Dockerfile and run them inside the container instead."
        )
    return sanitized.rstrip() + "\n", tuple(warnings)


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


def render_python_uv_dockerfile(
    context_dir: Path,
    python_version: str,
    has_pyproject: bool,
    has_uvlock: bool,
) -> Path:
    """Generate a uv+Python base Dockerfile and write it to context_dir.

    The cache-warming COPY/RUN block is included only when both pyproject.toml
    and uv.lock are present in the project (has_pyproject and has_uvlock both
    True). Callers are responsible for emitting a warning when either is absent.
    """
    lines = [
        "FROM ghcr.io/astral-sh/uv:latest AS uv-bin",
        f"FROM python:{python_version}-slim",
        "",
        "COPY --from=uv-bin /uv /uvx /usr/local/bin/",
        "ENV UV_CACHE_DIR=/opt/uv-cache",
        "WORKDIR /workspace",
    ]
    if has_pyproject and has_uvlock:
        lines += [
            "",
            "# pre-warm the uv cache",
            "COPY pyproject.toml uv.lock ./",
            "RUN uv sync --frozen --no-install-project && chown -R 1000:1000 /opt/uv-cache",
        ]
    out = context_dir / "Dockerfile.python-uv"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def render_entrypoint(context_dir: Path) -> Path:
    out = context_dir / "entrypoint.sh"
    tmpl = templating.get_template("entrypoint.sh.j2")
    out.write_text(tmpl.render() + "\n", encoding="utf-8")
    out.chmod(0o755)
    return out


def render_devcontainer_entrypoint(context_dir: Path) -> Path:
    out = context_dir / "project-sandbox-devcontainer-init"
    tmpl = templating.get_template("devcontainer-entrypoint.sh.j2")
    out.write_text(tmpl.render() + "\n", encoding="utf-8")
    out.chmod(0o755)
    return out
