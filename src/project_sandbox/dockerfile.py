import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import templating

_USER_SETUP_COMMAND_RE = re.compile(
    r"(?<![\w.-])(addgroup|adduser|groupadd|groupmod|useradd|usermod)(?![\w.-])"
)

_NON_APT_IMAGE_FRAGMENTS = (
    "alpine",
    "scratch",
    "distroless",
    "centos",
    "rhel",
    "fedora",
    "rocky",
    "almalinux",
    "opensuse",
    "suse",
    "busybox",
    "wolfi",
    "chainguard",
)

_LOCAL_INSTALL_RE = re.compile(
    r"uv\s+sync"
    r"|pip3?\s+install\s+(-e\s+)?\."
    r"|poetry\s+install"
    r"|pipenv\s+install"
    r"|npm\s+install(?!\s+(-g|--global)\b)"
    r"|npm\s+ci\b"
    r"|yarn\s+install"
    r"|pnpm\s+install",
    re.IGNORECASE,
)

_DEPENDENCY_STAGE_NAME = "project-sandbox-dependencies"


@dataclass(frozen=True)
class _DockerfileStage:
    from_block_index: int
    end_block_index: int
    base: str
    alias: str | None
    parent_index: int | None


@dataclass(frozen=True)
class _DockerfileFragments:
    before_dependencies: str
    dependency_from: str
    after_dependencies: str


def render(
    context_dir: Path,
    *,
    base_image: str | None = None,
    base_dockerfile: Path | None = None,
    build_context: Path | None = None,
    install_agents: tuple[str, ...] = ("claude", "codex", "opencode", "pi"),
    warn: Callable[[str], None] | None = None,
) -> Path:
    if (base_image is None) == (base_dockerfile is None):
        raise ValueError("Provide exactly one of base_image or base_dockerfile")

    source_before_dependencies = ""
    dependency_from = f"FROM {base_image}" if base_image is not None else ""
    source_after_dependencies = ""
    if base_dockerfile is not None:
        source_dockerfile_text, warnings = _read_source_dockerfile(base_dockerfile)
        fragments = _source_dockerfile_fragments(
            _dockerfile_blocks(source_dockerfile_text)
        )
        source_before_dependencies = fragments.before_dependencies
        dependency_from = fragments.dependency_from
        source_after_dependencies = fragments.after_dependencies
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
        source_before_dependencies=source_before_dependencies,
        dependency_from=dependency_from,
        source_after_dependencies=source_after_dependencies,
        sandbox_copy_prefix=copy_prefix,
        install_claude="claude" in install_agents,
        install_codex="codex" in install_agents,
        install_opencode="opencode" in install_agents,
        install_pi="pi" in install_agents,
    )
    container = _write_dockerfile(
        tmpl,
        context_dir / "Dockerfile",
        **shared,
        firewall_src_filename="init-firewall.sh",
    )
    _write_dockerfile(
        tmpl,
        context_dir / "Dockerfile.devcontainer",
        **shared,
        firewall_src_filename="init-firewall-devcontainer.sh",
    )
    return container


# Paths that are never needed inside the image and never affect a build (so they
# are safe to drop from a whole-project build context). Deliberately excludes
# .git — git-based version backends (setuptools_scm, hatch-vcs) read it during
# `uv sync` / project install — and source/artifact dirs like dist/build that a
# project might legitimately ship. Must NOT list .project-sandbox/, whose
# generated scripts are COPY'd into the image.
_DOCKERIGNORE_PATTERNS = (
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "*.pyc",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".DS_Store",
    "target",
)


def render_dockerignore(
    context_dir: Path, *, build_context: Path | None = None
) -> tuple[Path, ...]:
    """Write build-ignore files scoped to the generated python-uv Dockerfile.

    Call this *only* for the python-uv flow, where project-sandbox generates the
    Dockerfile (`COPY . . && uv sync`) and therefore knows the excluded paths are
    not build inputs. It is NOT used for user-supplied `--dockerfile` builds: that
    Dockerfile may legitimately `COPY` a venv or `node_modules`, so imposing an
    ignore file the user did not write could silently break their build.

    BuildKit (used by docker, podman and Apple container) honors a
    ``<dockerfile>.dockerignore`` next to the referenced Dockerfile, so this trims
    the context without creating a root .dockerignore that would affect the user's
    other builds. A user's existing root .dockerignore is left authoritative — the
    per-Dockerfile file would *override* (not merge with) it, re-including paths
    they deliberately excluded — so generation is skipped when one is present, and
    also when the build context is just the .project-sandbox dir (nothing to trim).
    """
    root = build_context if build_context is not None else context_dir
    if root.resolve(strict=False) == context_dir.resolve(strict=False):
        return ()
    if (root / ".dockerignore").exists():
        return ()
    body = "\n".join(_DOCKERIGNORE_PATTERNS) + "\n"
    written = []
    for dockerfile_name in ("Dockerfile", "Dockerfile.devcontainer"):
        out = context_dir / f"{dockerfile_name}.dockerignore"
        out.write_text(body, encoding="utf-8")
        written.append(out)
    return tuple(written)


def _write_dockerfile(tmpl, out: Path, *, firewall_src_filename: str, **kwargs) -> Path:
    out.write_text(
        tmpl.render(firewall_src_filename=firewall_src_filename, **kwargs) + "\n",
        encoding="utf-8",
    )
    return out


def source_warnings(base_dockerfile: Path) -> tuple[str, ...]:
    _, warnings = _read_source_dockerfile(base_dockerfile)
    return warnings


def _extract_last_from(blocks: list[list[str]]) -> str | None:
    stages = _dockerfile_stages(blocks)
    if not stages:
        return None
    stage = stages[-1]
    while stage.parent_index is not None:
        stage = stages[stage.parent_index]
    return stage.base


def _source_dockerfile_fragments(
    blocks: list[list[str]],
) -> _DockerfileFragments:
    """Insert an inherited dependency stage into sanitized source blocks."""
    stages = _dockerfile_stages(blocks)
    if not stages:
        raise ValueError("Dockerfile must contain at least one FROM instruction")

    internal_name = _unused_dependency_stage_name(stages)
    prefix_indexes = [
        index
        for index, stage in enumerate(stages)
        if stage.alias is not None and stage.alias.casefold() == "prefix"
    ]
    if len(prefix_indexes) > 1:
        raise ValueError(
            "Dockerfile declares more than one stage named 'prefix' "
            "(stage-name matching is case-insensitive)"
        )

    if not prefix_indexes:
        final = stages[-1]
        dependency_block = _rewrite_from_block(
            blocks[final.from_block_index], alias=internal_name
        )
        final_from = _generated_from(internal_name, final.alias)
        after = [[final_from], *blocks[final.from_block_index + 1 :]]
        return _DockerfileFragments(
            before_dependencies=_join_dockerfile_blocks(
                blocks[: final.from_block_index]
            ),
            dependency_from=_join_dockerfile_blocks([dependency_block]).rstrip(),
            after_dependencies=_join_dockerfile_blocks(after),
        )

    prefix_index = prefix_indexes[0]
    prefix = stages[prefix_index]
    final_index = len(stages) - 1
    if prefix_index == final_index:
        return _DockerfileFragments(
            before_dependencies=_join_dockerfile_blocks(
                blocks[: prefix.end_block_index]
            ),
            dependency_from=_generated_from(prefix.alias or "prefix", internal_name),
            after_dependencies=_generated_from(internal_name, None) + "\n",
        )

    path_child = final_index
    while stages[path_child].parent_index is not None:
        if stages[path_child].parent_index == prefix_index:
            break
        path_child = stages[path_child].parent_index  # type: ignore[assignment]
    else:
        raise ValueError(
            "Dockerfile stage named 'prefix' is not an ancestor of the final stage; "
            "make the final stage inherit from prefix"
        )

    child = stages[path_child]
    after_blocks = [list(block) for block in blocks[prefix.end_block_index :]]
    child_offset = child.from_block_index - prefix.end_block_index
    after_blocks[child_offset] = _rewrite_from_block(
        after_blocks[child_offset], base=internal_name, alias=child.alias
    )
    return _DockerfileFragments(
        before_dependencies=_join_dockerfile_blocks(blocks[: prefix.end_block_index]),
        dependency_from=_generated_from(prefix.alias or "prefix", internal_name),
        after_dependencies=_join_dockerfile_blocks(after_blocks),
    )


def _dockerfile_stages(blocks: list[list[str]]) -> list[_DockerfileStage]:
    parsed: list[tuple[int, str, str | None]] = []
    for block_index, block in enumerate(blocks):
        parsed_from = _parse_from_block(block)
        if parsed_from is not None:
            base, alias = parsed_from
            parsed.append((block_index, base, alias))

    stages: list[_DockerfileStage] = []
    prior_aliases: dict[str, int] = {}
    for index, (block_index, base, alias) in enumerate(parsed):
        end = parsed[index + 1][0] if index + 1 < len(parsed) else len(blocks)
        stages.append(
            _DockerfileStage(
                from_block_index=block_index,
                end_block_index=end,
                base=base,
                alias=alias,
                parent_index=prior_aliases.get(base.casefold()),
            )
        )
        if alias is not None:
            prior_aliases[alias.casefold()] = index
    return stages


def _parse_from_block(block: list[str]) -> tuple[str, str | None] | None:
    if not block:
        return None
    match = re.match(r"\s*FROM\s+(.*)", " ".join(block), re.IGNORECASE)
    if match is None:
        return None
    tokens = match.group(1).split()
    base_index = _first_non_option_token_index(tokens)
    if base_index is None:
        return None
    alias = None
    if len(tokens) >= base_index + 3 and tokens[base_index + 1].lower() == "as":
        alias = tokens[base_index + 2]
    return tokens[base_index], alias


def _rewrite_from_block(
    block: list[str], *, base: str | None = None, alias: str | None = None
) -> list[str]:
    match = re.match(r"(\s*FROM\s+)(.*)", " ".join(block), re.IGNORECASE)
    if match is None:
        raise ValueError("Expected a FROM instruction")
    tokens = match.group(2).split()
    base_index = _first_non_option_token_index(tokens)
    if base_index is None:
        raise ValueError("FROM instruction is missing a base image")
    if base is not None:
        tokens[base_index] = base
    tokens = tokens[: base_index + 1]
    if alias is not None:
        tokens.extend(("AS", alias))
    return [match.group(1) + " ".join(tokens)]


def _generated_from(base: str, alias: str | None) -> str:
    result = f"FROM {base}"
    if alias is not None:
        result += f" AS {alias}"
    return result


def _unused_dependency_stage_name(stages: list[_DockerfileStage]) -> str:
    used = {stage.alias.casefold() for stage in stages if stage.alias is not None}
    candidate = _DEPENDENCY_STAGE_NAME
    suffix = 2
    while candidate.casefold() in used:
        candidate = f"{_DEPENDENCY_STAGE_NAME}-{suffix}"
        suffix += 1
    return candidate


def _join_dockerfile_blocks(blocks: list[list[str]]) -> str:
    lines = [line for block in blocks for line in block]
    return "\n".join(lines).rstrip() + ("\n" if lines else "")


def _first_non_option_token(rest: str) -> str | None:
    """Return the image token from a FROM line, skipping options like
    ``--platform=...`` (or ``--flag value``) that may precede the image."""
    tokens = rest.split()
    index = _first_non_option_token_index(tokens)
    return tokens[index] if index is not None else None


def _first_non_option_token_index(tokens: list[str]) -> int | None:
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.startswith("--"):
            # "--flag=value" carries its value inline; "--flag value" does not,
            # so skip the following token as well.
            if "=" not in token:
                i += 1
            i += 1
            continue
        return i
    return None


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
            "project-sandbox will create its own unprivileged agent user."
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
            if _is_mixed_user_setup_run(block):
                command = " ".join(line.strip().rstrip("\\") for line in block).strip()
                raise ValueError(
                    "Dockerfile RUN instruction mixes restricted user-management "
                    "commands (useradd/groupadd/etc.) with other build steps and "
                    "cannot be sanitized automatically:\n"
                    f"    {command}\n"
                    "Move the user-management commands into a separate RUN "
                    "instruction so project-sandbox can remove them safely; "
                    "project-sandbox creates its own unprivileged agent user."
                )
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


def _is_mixed_user_setup_run(block: list[str]) -> bool:
    """Return True when a RUN block contains user-management commands alongside
    unrelated build steps, so removing the whole block would drop real work."""
    if not block or not re.match(r"\s*RUN\b", block[0], re.IGNORECASE):
        return False
    command = " ".join(line.strip().rstrip("\\") for line in block)
    command = re.sub(r"^\s*RUN\b", "", command, count=1, flags=re.IGNORECASE)
    has_user_setup = False
    has_other = False
    for sub in re.split(r"&&|\|\||;", command):
        sub = sub.strip()
        if not sub or _is_trivial_subcommand(sub):
            continue
        if _USER_SETUP_COMMAND_RE.search(sub):
            has_user_setup = True
        else:
            has_other = True
    return has_user_setup and has_other


def _is_trivial_subcommand(sub: str) -> bool:
    head = sub.split(maxsplit=1)[0]
    return head in {"set", "export", "true", ":"}


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

    The project itself is installed during the image build (with network access)
    so that 'uv run' inside the sandboxed container — which has no network —
    finds it already present and skips the build step entirely. Two layers are
    used so that the slower source-copy/project-install layer only rebuilds when
    source files change, while the faster dep-only layer is cache-stable against
    lockfile changes.
    """
    lines = [
        # Pin uv to an exact tag and digest instead of the mutable ":latest"
        # tag. Bump deliberately and refresh the digest via
        # `docker buildx imagetools inspect ghcr.io/astral-sh/uv:<tag>`.
        "FROM ghcr.io/astral-sh/uv:0.11.28"
        "@sha256:0f36cb9361a3346885ca3677e3767016687b5a170c1a6b88465ec14aefec90aa"
        " AS uv-bin",
        f"FROM python:{python_version}-slim",
        "",
        "ARG AGENT_UID=1000",
        "ARG AGENT_GID=1000",
        "",
        "COPY --from=uv-bin /uv /uvx /usr/local/bin/",
        "ENV UV_CACHE_DIR=/opt/uv-cache",
        "ENV UV_PROJECT_ENVIRONMENT=/opt/venv",
        "WORKDIR /workspace",
    ]
    if has_pyproject and has_uvlock:
        lines += [
            "",
            "# uv requires the Git executable to resolve git+https dependencies",
            "RUN apt-get update && apt-get install -y --no-install-recommends git && \\",
            "    rm -rf /var/lib/apt/lists/*",
            "",
            "# layer 1: install deps only (rebuilds only when pyproject.toml/uv.lock change)",
            "COPY pyproject.toml uv.lock ./",
            "RUN uv sync --frozen --no-install-project",
            "",
            "# layer 2: install the project so 'uv run' works offline inside the sandbox",
            "COPY . .",
            'RUN uv sync --frozen && chown -R "${AGENT_UID}:${AGENT_GID}" /opt/uv-cache /opt/venv',
        ]
    out = context_dir / "Dockerfile.python-uv"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def render_rust_cargo_dockerfile(
    context_dir: Path,
    rust_version: str | None,
    has_cargo_toml: bool,
    has_cargo_lock: bool,
    *,
    workspace_members: list[str] | None = None,
    workspace_root_is_package: bool = False,
) -> Path:
    """Generate a Rust/cargo base Dockerfile and write it to context_dir.

    The cache-warming COPY/RUN block is included only when both Cargo.toml and
    Cargo.lock are present in the project (has_cargo_toml and has_cargo_lock
    both True). Callers are responsible for emitting a warning when either is
    absent.

    The project is compiled during the image build (with network access) so
    that 'cargo build' inside the sandboxed container — which has no network —
    finds dependency sources already fetched into CARGO_HOME and finds compiled
    dependency artifacts already in CARGO_TARGET_DIR. Two layers are used so
    that the faster fetch-only layer is reused across source edits and rebuilds
    only when Cargo.toml/Cargo.lock change, while the slower source-copy/compile
    layer rebuilds whenever any source file changes.

    The project compile is best-effort ('cargo build || true'): a project that
    does not yet compile must not block image creation, since 'cargo fetch'
    has already made the dependency sources available offline and cargo builds
    dependencies before the project crate, so their artifacts are cached even
    when the project crate itself fails to build. This lets an agent enter the
    sandbox to fix a broken build offline — the whole point of the flow.

    workspace_members: relative paths to workspace member crates, or None if
        this is not a workspace. When set, each member's Cargo.toml is COPY'd
        into the fetch layer so cargo can resolve the full dependency graph.
    workspace_root_is_package: True when the workspace root Cargo.toml also
        declares a [package] (i.e. the root is itself a crate).
    """
    base_image = f"rust:{rust_version}-slim" if rust_version else "rust:slim"
    lines = [
        f"FROM {base_image}",
        "",
        "ARG AGENT_UID=1000",
        "ARG AGENT_GID=1000",
        "",
        "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
        "    build-essential cmake \\\n"
        "    pkg-config \\\n"
        "    libssl-dev \\\n"
        "    libudev-dev libasound2-dev \\\n"
        "    libx11-dev libxcb1-dev libxrandr-dev libxi-dev libxcursor-dev \\\n"
        "    libwayland-dev \\\n"
        "    libdbus-1-dev \\\n"
        "    libpq-dev libsqlite3-dev \\\n"
        "    libclang-dev \\\n"
        "    libfontconfig1-dev libfreetype6-dev \\\n"
        "    && rm -rf /var/lib/apt/lists/*",
        "",
        "ENV CARGO_HOME=/opt/cargo-cache",
        "ENV CARGO_TARGET_DIR=/opt/cargo-target",
        "WORKDIR /workspace",
    ]
    if has_cargo_toml and has_cargo_lock:
        is_workspace = workspace_members is not None
        members = workspace_members or []

        lines += [
            "",
            "# layer 1: fetch dependency sources (rebuilds only when Cargo.toml/Cargo.lock change)",
            "COPY Cargo.toml Cargo.lock ./",
        ]

        # For workspace projects, copy each member's manifest so cargo can
        # resolve the full dependency graph without the member source files.
        for member in members:
            lines.append(f"COPY {member}/Cargo.toml {member}/")

        # cargo refuses to parse a manifest that declares no targets, so we
        # create a minimal stub for each crate that needs one, run the fetch,
        # then remove the stubs before COPY . . overlays the real source.
        needs_root_stub = not is_workspace or workspace_root_is_package
        stub_dirs = (["src"] if needs_root_stub else []) + [f"{m}/src" for m in members]

        if stub_dirs:
            fetch_parts = (
                ["mkdir -p " + " ".join(stub_dirs)]
                + [f"touch {d}/lib.rs" for d in stub_dirs]
                + ["cargo fetch --locked"]
                + ["rm -rf " + " ".join(stub_dirs)]
            )
            first, *rest = fetch_parts
            lines.append("RUN " + first + "".join(f" \\\n    && {p}" for p in rest))
        else:
            lines.append("RUN cargo fetch --locked")

        lines += [
            "",
            "# layer 2: pre-compile so 'cargo build' is fast (and offline) inside the",
            "# sandbox. Best-effort: a project that does not yet compile must not block",
            "# the image build — 'cargo fetch' above already made deps available offline.",
            "COPY . .",
            "RUN cargo build || true",
            "RUN mkdir -p /opt/cargo-cache /opt/cargo-target"
            ' && chown -R "${AGENT_UID}:${AGENT_GID}" /opt/cargo-cache /opt/cargo-target',
        ]
    out = context_dir / "Dockerfile.rust-cargo"
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
