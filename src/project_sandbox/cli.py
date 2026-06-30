import hashlib
import os
import re
import shlex
import shutil
import sys
import time
import uuid
from argparse import ArgumentParser
from pathlib import Path

from . import (
    build_cache,
    config_agents,
    container_cli,
    devcontainer,
    dockerfile,
    dockerfile_checksum,
    firewall,
    oauth_refresh,
    session,
    token_expiry,
    transcript,
)
from . import (
    jj_workspace as jj_workspace_mod,
)
from . import (
    worktree as worktree_mod,
)
from .git_identity import read as read_identity
from .paths import (
    HISTORY_CLAUDE_PROJECTS_TARGET,
    HISTORY_HISTFILE,
    HISTORY_SHELL_TARGET,
    WORKSPACE_CARGO_TARGET,
    WORKSPACE_DEVCONTAINER_TARGET,
    WORKSPACE_SANDBOX_TARGET,
    ensure_dir,
    ensure_history_paths,
    ensure_workspace_sandbox_mask,
    resolve_strict,
)

SUPPORTED_AGENTS = ("claude", "codex", "opencode", "bash")
PROMPT_MOUNT_TARGET = "/project-sandbox-prompt"


def _default_image_tag(project: Path) -> str:
    resolved = project.resolve()
    name = re.sub(r"[^a-z0-9._-]", "-", resolved.name.lower())
    name = re.sub(r"-{2,}", "-", name).strip("-") or "project"
    path_hash = hashlib.sha256(str(resolved).encode()).hexdigest()[:8]
    return f"project-sandbox-{name}-{path_hash}:latest"


def build_parser() -> ArgumentParser:
    p = ArgumentParser(prog="project-sandbox")
    p.add_argument("project")
    p.add_argument("base_image", nargs="?")
    p.add_argument(
        "--dockerfile",
        help="Build the sandbox on top of an existing Dockerfile instead of a base image tag.",
    )
    p.add_argument(
        "--docker-context",
        help="Build context to use with --dockerfile (default: project root).",
    )
    p.add_argument(
        "--python-uv",
        action="store_true",
        help=(
            "Synthesise a Python/uv base Dockerfile instead of supplying one. "
            "Mutually exclusive with base_image and --dockerfile."
        ),
    )
    p.add_argument(
        "--python",
        default=None,
        dest="python_version",
        metavar="VERSION",
        help="Python image tag for --python-uv (default: 3.11). Only valid with --python-uv.",
    )
    p.add_argument(
        "--rust-cargo",
        action="store_true",
        help=(
            "Synthesise a Rust/cargo base Dockerfile instead of supplying one. "
            "Mutually exclusive with base_image and --dockerfile."
        ),
    )
    p.add_argument(
        "--rust",
        default=None,
        dest="rust_version",
        metavar="VERSION",
        help=(
            "Rust toolchain version for --rust-cargo, e.g. 1.87, producing "
            "rust:1.87-slim (default: latest stable rust:slim). Only valid "
            "with --rust-cargo."
        ),
    )
    p.add_argument("--image-tag", default=None)
    p.add_argument(
        "--runtime",
        choices=list(container_cli.RUNTIME_CHOICES),
        default="auto",
        help="Container runtime for direct CLI runs (default: auto).",
    )
    p.add_argument("--no-build", action="store_true")
    p.add_argument("--force-build", action="store_true")
    p.add_argument(
        "--no-verify-dockerfile",
        action="store_true",
        help=(
            "Skip Dockerfile tamper-detection check. Use when you have intentionally "
            "edited the Dockerfile and do not want to be prompted. The baseline is "
            "still updated after a real build so verification resumes on the next run."
        ),
    )
    p.add_argument("--memory", default="8g")
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--mount", dest="extra_mounts", action="append", default=[])
    p.add_argument("--extra-domain", action="append", default=[])
    p.add_argument(
        "--allow-github",
        action="store_true",
        help=(
            "Allow GitHub and GitHub Copilot hosts at runtime. This includes "
            "GitHub's published web/API/git IP ranges and DNS-pinned GitHub "
            "Copilot endpoints."
        ),
    )
    p.add_argument("--no-firewall", action="store_true")
    p.add_argument(
        "--branch",
        help="Run the agent in a git worktree on this branch (created if it doesn't exist).",
    )
    p.add_argument("--worktree-base")
    p.add_argument("--worktree-dir")
    p.add_argument(
        "--after-session",
        choices=["ask", "merge", "rebase", "pr", "nothing"],
        default="ask",
    )
    p.add_argument("--prompt")
    p.add_argument("--prompt-text")
    p.add_argument(
        "--agent",
        choices=list(SUPPORTED_AGENTS),
        help="Agent to run. When omitted, project-sandbox only initializes generated config files.",
    )
    p.add_argument("--log")
    p.add_argument("--timeout", type=int)
    p.add_argument(
        "--model",
        default=None,
        metavar="MODEL_ID",
        help=(
            "Model ID to use for the agent, in both interactive and unsupervised "
            "(batch) runs. "
            "Example for Claude: --agent claude --model sonnet --prompt-text '...'. "
            "Example for Codex: --agent codex --model gpt-5.4-mini "
            "--prompt-text '...'. "
            "Example for OpenCode: --agent opencode --model openai/gpt-5.4-mini "
            "--prompt-text '...'."
        ),
    )
    p.add_argument(
        "--effort",
        choices=["low", "medium", "high", "xhigh", "max"],
        default=None,
        metavar="LEVEL",
        help=(
            "Reasoning effort level for Claude, Codex, and OpenCode, in both "
            "interactive and unsupervised (batch) runs. "
            "One of: low, medium, high, xhigh, max (default: xhigh). "
            "Examples for Claude: --agent claude --model sonnet --effort low "
            "or --agent claude --model sonnet --effort high. "
            "Examples for Codex: --agent codex --model gpt-5.4-mini --effort low "
            "or --agent codex --model gpt-5.4-mini --effort high. "
            "Examples for OpenCode: --agent opencode --model openai/gpt-5.4-mini "
            "--effort low or --agent opencode --model openai/gpt-5.4-mini "
            "--effort high."
        ),
    )
    p.add_argument(
        "--no-forward-credentials",
        action="store_true",
        help=(
            "Do not stage, mount, or forward host agent credentials (and remove "
            "any previously staged), and generate a credential-free devcontainer; "
            "start unauthenticated and log in inside the sandbox. Disables the host "
            "token refresh and the credential-lifetime warning."
        ),
    )
    p.add_argument(
        "--api-key-env",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "With --no-forward-credentials, inject this host environment variable "
            "into a direct agent container. Repeat for multiple API keys."
        ),
    )
    p.add_argument(
        "--api-key-env-file",
        action="append",
        default=[],
        metavar="FILE",
        help=(
            "With --no-forward-credentials, inject API key environment variables "
            "from a dotenv-style KEY=VALUE file into a direct agent container. "
            "Repeat to load multiple files."
        ),
    )
    p.add_argument(
        "--no-token-refresh",
        action="store_true",
        help=(
            "Do not attempt to refresh the host Claude OAuth token before launching, even "
            "if it is near expiry. The staged token is used as-is."
        ),
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.prompt and args.prompt_text:
        raise SystemExit("Use only one of --prompt or --prompt-text")
    run_agent = _requested_agent(args)
    _validate_api_key_injection_args(args, run_agent)
    if args.branch and run_agent is None:
        raise SystemExit("--branch requires --agent, --prompt, or --prompt-text")
    if (
        args.branch
        and (args.prompt or args.prompt_text)
        and args.after_session == "ask"
    ):
        raise SystemExit(
            "--after-session=ask is not valid in unsupervised mode; use --after-session=nothing or another option."
        )

    project = resolve_strict(args.project)
    if args.image_tag is None:
        args.image_tag = _default_image_tag(project)
    identity = read_identity()
    available_agents = config_agents.available_agents()

    if run_agent is not None and args.branch:
        _validate_worktree_project(project)
    if run_agent is not None:
        _ensure_agent_available(run_agent, available_agents)
    runtime = (
        container_cli.select_runtime(args.runtime)
        if run_agent is not None and not args.dry_run
        else None
    )

    if args.dry_run:
        wt, workspace = _plan_worktree(args, project) if run_agent else (None, project)
        return _dry_run(
            args,
            project=project,
            workspace=workspace,
            worktree=wt,
            identity=identity,
            available_agents=available_agents,
        )

    # Validate fatal inputs BEFORE creating a worktree, so a bad build source,
    # missing prompt, or unwritable log path fails without first orphaning a
    # branch/worktree. _resolve_build_source also writes the synthesised
    # python-uv/rust-cargo Dockerfile, but only into .project-sandbox
    # (project-level, not the worktree).
    context_dir = ensure_dir(project / ".project-sandbox")
    base_image, base_dockerfile, build_context = _resolve_build_source(
        args,
        project=project,
        context_dir=context_dir,
    )
    if run_agent is not None:
        _validate_session_inputs(args)
    allow_github = _allow_github(args, run_agent)
    _warn_opencode_provider_allowlist(args, run_agent)

    wt, workspace = _setup_worktree(args, project) if run_agent else (None, project)

    # Once a worktree exists, every exit path must run teardown so a failed build
    # or early return does not orphan the worktree (and its branch). agent_ran
    # distinguishes a genuine session (honor --after-session) from a setup/build
    # failure before the agent ran (never integrate — just leave it in place).
    agent_ran = False
    exit_code = 1
    try:
        dockerfile.render(
            context_dir,
            base_image=base_image,
            base_dockerfile=base_dockerfile,
            build_context=build_context,
            install_agents=available_agents,
            warn=print,
        )
        # Trim the whole-project build context only for the python-uv/rust-cargo
        # flows, whose Dockerfile we generate and whose excluded paths we know
        # are not build inputs. User-supplied --dockerfile builds are left
        # untouched so an injected ignore file can't break a COPY they rely on.
        if getattr(args, "python_uv", False) or getattr(args, "rust_cargo", False):
            dockerfile.render_dockerignore(context_dir, build_context=build_context)
        dockerfile.render_entrypoint(context_dir)
        dockerfile.render_devcontainer_entrypoint(context_dir)
        firewall.render(
            context_dir,
            extra_domains=args.extra_domain,
            allow_github=allow_github,
        )

        cfg = config_agents.render(context_dir)
        forward_credentials = not args.no_forward_credentials
        # Before staging, ask the agent's own CLI to refresh its host token so the
        # container starts with a near-full window. bash may run claude, so it
        # refreshes the claude token; opencode has no delegated refresh (no-op).
        if run_agent is not None and forward_credentials and not args.no_token_refresh:
            oauth_refresh.refresh_host_token(
                "claude" if run_agent == "bash" else run_agent, home=Path.home()
            )
        if forward_credentials:
            credential_dirs = config_agents.sync_credentials(context_dir)
        else:
            # Read/copy no host credentials, and remove any staged by a previous
            # forwarding run so nothing lingers on disk or can be mounted.
            config_agents.purge_staged_credentials(context_dir)
            credential_dirs = {}

        _write_project_sandbox_gitignore(context_dir)
        _update_project_gitignore(project)

        devcontainer.render(
            project,
            identity=identity,
            firewall_enabled=not args.no_firewall,
            memory=args.memory,
            cpus=args.cpus,
            extra_mounts=args.extra_mounts,
            credential_dirs=credential_dirs,
            forward_credentials=forward_credentials,
            build_context=build_context,
        )

        if run_agent is None:
            _print_next_steps(
                context_dir=context_dir,
                project=project,
                available_agents=available_agents,
            )
            return 0

        assert runtime is not None
        rc = container_cli.ensure_system_started(runtime=runtime, verbose=args.verbose)
        if rc != 0:
            print(
                "[W] Apple container system not running - if you're on a Mac, you may need to install or start it. Otherwise, you can still work with the devcontainer setup."
            )

        tracked_dockerfiles = _tracked_project_dockerfiles(base_dockerfile, context_dir)
        if not args.no_verify_dockerfile:
            warnings = dockerfile_checksum.changed_warnings(context_dir, tracked_dockerfiles)
            if warnings:
                for warning in warnings:
                    print(warning)
                unsupervised_check = bool(args.prompt or args.prompt_text)
                if unsupervised_check or not sys.stdin.isatty():
                    print(
                        "[E] Dockerfile changed and run is non-interactive: aborting. "
                        "Use --no-verify-dockerfile to skip this check."
                    )
                    return 1
                answer = input("Rebuild from the changed Dockerfile anyway? [y/N] ")
                if answer.strip().lower() not in ("y", "yes"):
                    return 1

        if not args.no_build:
            # Reuse an existing image when its build inputs are unchanged. This
            # auto-skip is limited to the default flow where the build context is
            # the generated .project-sandbox dir, which fully determines the
            # image; whole-project contexts (--python-uv / --rust-cargo /
            # --dockerfile) keep building and rely on the runtime's layer
            # cache + the generated .dockerignore instead of fingerprinting an
            # arbitrary source tree.
            fingerprint = build_cache.compute_fingerprint(
                context_dir,
                extra={"image_tag": args.image_tag, "base_image": base_image or ""},
            )
            context_is_sandbox = build_context.resolve(
                strict=False
            ) == context_dir.resolve(strict=False)
            cache_hit = (
                not args.force_build
                and context_is_sandbox
                and build_cache.is_cache_valid(
                    context_dir,
                    image_tag=args.image_tag,
                    fingerprint=fingerprint,
                )
                and container_cli.image_exists(runtime, args.image_tag)
            )
            if cache_hit:
                print("Reusing cached image (inputs unchanged)")
            else:
                if not args.verbose:
                    print("Building container image…")
                start = time.monotonic()
                rc = container_cli.build_image(
                    runtime=runtime,
                    context_dir=context_dir,
                    image_tag=args.image_tag,
                    build_context=build_context,
                    dockerfile_path=context_dir / "Dockerfile",
                    verbose=args.verbose,
                )
                if rc != 0:
                    return rc
                build_cache.write_state(
                    context_dir,
                    image_tag=args.image_tag,
                    fingerprint=fingerprint,
                )
                # Record the trusted baseline after a real build into the masked
                # .project-sandbox dir the sandbox cannot reach.
                dockerfile_checksum.record(context_dir, tracked_dockerfiles)
                print(f"Built image in {time.monotonic() - start:.1f}s")

        cmd, log_path, unsupervised, container_stop_argv = _build_session_command(
            args,
            project=project,
            context_dir=context_dir,
            workspace=workspace,
            worktree=wt,
            identity=identity,
            run_agent=run_agent,
            claude_cfg=cfg["claude"],
            credential_dirs=credential_dirs,
            codex_cfg=cfg["codex"],
            runtime=runtime,
            create_prompt_files=True,
        )

        if not unsupervised:
            if args.verbose:
                _print_next_steps(
                    context_dir=context_dir,
                    project=project,
                    available_agents=available_agents,
                    launching=True,
                )
            else:
                print("Starting container…")

        agent_ran = True
        if unsupervised:
            assert log_path is not None
            if not args.verbose:
                print(f"Running {run_agent} (headless); streaming to {log_path}")
            exit_code = session.run(
                cmd,
                log_path=log_path,
                timeout=args.timeout,
                container_stop_argv=container_stop_argv,
                verbose=args.verbose,
            )
            if not args.verbose:
                print(f"Wrote {session.count_lines(log_path)} lines to {log_path}")
            if run_agent in ("claude", "codex"):
                _write_transcript_markdown(log_path)
        else:
            exit_code = container_cli.run(cmd)

        return exit_code
    finally:
        if wt is not None:
            if agent_ran:
                _teardown_worktree(args, project=project, wt=wt, exit_code=exit_code)
            elif isinstance(wt, jj_workspace_mod.JjWorkspace):
                jj_workspace_mod.remove(project, wt)
            else:
                _teardown_any(project, wt, after="nothing")


def _write_transcript_markdown(log_path: Path) -> None:
    """Best-effort: render a markdown transcript beside the JSON session log.

    Transcript generation must never fail the run, so any parsing/IO error is
    reported and swallowed.
    """
    try:
        md_path = transcript.log_to_markdown(log_path)
    except Exception as exc:  # noqa: BLE001 - best-effort sidecar, never fatal
        print(f"[W] Could not write markdown transcript: {exc}")
        return
    if md_path is not None:
        print(f"Transcript: {md_path}")


def _requested_agent(args) -> str | None:
    if args.agent:
        return args.agent
    if args.prompt or args.prompt_text:
        return "claude"
    return None


def _ensure_agent_available(run_agent: str, available_agents: tuple[str, ...]) -> None:
    if run_agent in available_agents:
        return
    available = ", ".join(available_agents)
    raise SystemExit(
        f"--agent={run_agent} is unavailable on this host; available agents: {available}"
    )


def _dry_run(
    args,
    *,
    project: Path,
    workspace: Path,
    worktree,
    identity,
    available_agents: tuple[str, ...],
) -> int:
    context_dir = project / ".project-sandbox"
    claude_cfg = context_dir / "claude" / "settings.json"
    codex_cfg = context_dir / "codex" / "config.toml"
    # Only the keys _build_session_command consumes ("claude", and optionally
    # "codex"/"opencode" when available); the devcontainer-specific dirs are not
    # used on the CLI run path.
    credential_dirs = {
        "claude": config_agents.credentials_dir(context_dir, "claude"),
        **{
            agent: config_agents.credentials_dir(context_dir, agent)
            for agent in ("codex", "opencode")
            if agent in available_agents
        },
    }
    run_agent = _requested_agent(args)
    _validate_api_key_injection_args(args, run_agent)
    _warn_opencode_provider_allowlist(args, run_agent)

    print("DRY RUN: no files, worktrees, images, or containers will be created.")
    if worktree is not None:
        print(f"Would use worktree at: {workspace}")
        if isinstance(worktree, jj_workspace_mod.JjWorkspace):
            source, target = jj_workspace_mod.repo_store_mount(project, workspace)
            print(f"Would mount .jj metadata: {source} -> {target}")
            git_mount = jj_workspace_mod.git_backend_mount(project, workspace)
            if git_mount is not None:
                print(f"Would mount jj git backend: {git_mount[0]} -> {git_mount[1]}")
        else:
            print(f"Would mount .git metadata: {(project / '.git').resolve()}")
    print(f"Would render sandbox assets under: {context_dir}")
    print(f"Would render devcontainer under: {project / '.devcontainer'}")
    _, base_dockerfile, build_context = _resolve_build_source(
        args,
        project=project,
        context_dir=context_dir,
        write_generated=False,
    )
    if base_dockerfile is not None:
        if getattr(args, "python_uv", False) or getattr(args, "rust_cargo", False):
            print(f"Would write synthesised Dockerfile: {base_dockerfile}")
        else:
            print(f"Would append sandbox layers to Dockerfile: {base_dockerfile}")
            for warning in dockerfile.source_warnings(base_dockerfile):
                print(warning)
        print(f"Would use build context: {build_context}")

    for warning in dockerfile_checksum.changed_warnings(
        context_dir, _tracked_project_dockerfiles(base_dockerfile, context_dir)
    ):
        print(warning)

    if run_agent is None:
        print(
            "Would initialize config files only; no agent container would be started."
        )
        return 0

    runtime = container_cli.select_runtime(args.runtime, dry_run=True)
    container_cli.ensure_system_started(
        runtime=runtime, dry_run=True, verbose=args.verbose
    )
    if not args.no_build:
        container_cli.build_image(
            runtime=runtime,
            context_dir=context_dir,
            image_tag=args.image_tag,
            build_context=build_context,
            dockerfile_path=context_dir / "Dockerfile",
            dry_run=True,
            verbose=args.verbose,
        )
    cmd, log_path, unsupervised, container_stop_argv = _build_session_command(
        args,
        project=project,
        context_dir=context_dir,
        workspace=workspace,
        worktree=worktree,
        identity=identity,
        run_agent=run_agent,
        claude_cfg=claude_cfg,
        credential_dirs=credential_dirs,
        codex_cfg=codex_cfg,
        runtime=runtime,
        create_prompt_files=False,
    )
    if unsupervised:
        assert log_path is not None
        session.run(
            cmd,
            log_path=log_path,
            timeout=args.timeout,
            container_stop_argv=container_stop_argv,
            dry_run=True,
            verbose=args.verbose,
        )
    else:
        container_cli.run(cmd, dry_run=True)
    return 0


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_api_key_injection_args(args, run_agent: str | None) -> None:
    requested = bool(
        getattr(args, "api_key_env", []) or getattr(args, "api_key_env_file", [])
    )
    if not requested:
        return
    if not args.no_forward_credentials:
        raise SystemExit(
            "--api-key-env and --api-key-env-file require --no-forward-credentials"
        )
    if run_agent is None:
        raise SystemExit(
            "--api-key-env and --api-key-env-file require --agent, --prompt, or --prompt-text"
        )


def _api_key_env(args, *, redact: bool = False) -> list[str]:
    values: dict[str, str] = {}
    for env_file in getattr(args, "api_key_env_file", []):
        values.update(_read_api_key_env_file(resolve_strict(env_file)))
    for name in getattr(args, "api_key_env", []):
        _validate_env_name(name, source="--api-key-env")
        if name not in os.environ:
            raise SystemExit(
                f"--api-key-env {name}: host environment variable is not set"
            )
        values[name] = os.environ[name]
    if redact:
        return [f"{name}=<redacted>" for name in values]
    return [f"{name}={value}" for name, value in values.items()]


def _read_api_key_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise SystemExit(f"{path}: API key env file must be UTF-8") from exc

    for line_no, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        if "=" not in stripped:
            raise SystemExit(f"{path}:{line_no}: expected KEY=VALUE")
        name, raw_value = stripped.split("=", 1)
        name = name.strip()
        _validate_env_name(name, source=f"{path}:{line_no}")
        values[name] = _parse_env_file_value(raw_value)
    return values


def _parse_env_file_value(raw_value: str) -> str:
    raw_value = raw_value.strip()
    if not raw_value:
        return ""
    if raw_value[0] in {'"', "'"}:
        try:
            parts = shlex.split(raw_value, comments=True, posix=True)
        except ValueError as exc:
            raise SystemExit(
                f"Invalid quoted value in API key env file: {exc}"
            ) from exc
        if len(parts) != 1:
            raise SystemExit("Invalid quoted value in API key env file")
        return parts[0]

    # Match common dotenv behavior: comments start only after whitespace, so
    # values such as sk-abc#123 are preserved.
    return re.sub(r"\s+#.*$", "", raw_value).strip()


def _validate_env_name(name: str, *, source: str) -> None:
    if _ENV_NAME_RE.fullmatch(name):
        return
    raise SystemExit(f"{source}: invalid environment variable name: {name!r}")


def _resolve_build_source(
    args,
    *,
    project: Path,
    context_dir: Path,
    write_generated: bool = True,
) -> tuple[str | None, Path | None, Path]:
    if args.docker_context and not args.dockerfile:
        raise SystemExit("--docker-context requires --dockerfile")

    python_uv = getattr(args, "python_uv", False)
    python_version = getattr(args, "python_version", None)
    rust_cargo = getattr(args, "rust_cargo", False)
    rust_version = getattr(args, "rust_version", None)

    if python_version is not None and not python_uv:
        raise SystemExit("--python is only valid with --python-uv")
    if rust_version is not None and not rust_cargo:
        raise SystemExit("--rust is only valid with --rust-cargo")
    if python_uv and rust_cargo:
        raise SystemExit("--python-uv and --rust-cargo are mutually exclusive")

    if python_uv:
        if args.dockerfile:
            raise SystemExit("--python-uv and --dockerfile are mutually exclusive")
        if args.base_image:
            raise SystemExit("--python-uv and base_image are mutually exclusive")

        effective_version = python_version or "3.11"
        has_pyproject = (project / "pyproject.toml").exists()
        has_uvlock = (project / "uv.lock").exists()

        if not has_pyproject:
            print(
                "[W] --python-uv: pyproject.toml not found — "
                "cache-warming step will be skipped."
            )
        if not has_uvlock:
            print(
                "[W] --python-uv: uv.lock not found — "
                "cache-warming step will be skipped."
            )

        generated = context_dir / "Dockerfile.python-uv"
        if write_generated:
            generated = dockerfile.render_python_uv_dockerfile(
                context_dir,
                python_version=effective_version,
                has_pyproject=has_pyproject,
                has_uvlock=has_uvlock,
            )
        return None, generated, project

    if rust_cargo:
        if args.dockerfile:
            raise SystemExit("--rust-cargo and --dockerfile are mutually exclusive")
        if args.base_image:
            raise SystemExit("--rust-cargo and base_image are mutually exclusive")

        has_cargo_toml = (project / "Cargo.toml").exists()
        has_cargo_lock = (project / "Cargo.lock").exists()

        if not has_cargo_toml:
            print(
                "[W] --rust-cargo: Cargo.toml not found — "
                "cache-warming step will be skipped."
            )
        if not has_cargo_lock:
            print(
                "[W] --rust-cargo: Cargo.lock not found — "
                "cache-warming step will be skipped."
            )

        is_workspace, ws_members, ws_root_is_pkg = (
            _detect_cargo_workspace(project) if has_cargo_toml else (False, [], False)
        )

        generated = context_dir / "Dockerfile.rust-cargo"
        if write_generated:
            generated = dockerfile.render_rust_cargo_dockerfile(
                context_dir,
                rust_version=rust_version,
                has_cargo_toml=has_cargo_toml,
                has_cargo_lock=has_cargo_lock,
                workspace_members=ws_members if is_workspace else None,
                workspace_root_is_package=ws_root_is_pkg,
            )
        return None, generated, project

    if args.dockerfile:
        if args.base_image:
            raise SystemExit("Use either base_image or --dockerfile, not both")
        base_dockerfile = resolve_strict(args.dockerfile)
        if not base_dockerfile.is_file():
            raise SystemExit(f"--dockerfile must point to a file: {base_dockerfile}")
        build_context = (
            resolve_strict(args.docker_context) if args.docker_context else project
        )
        if not build_context.is_dir():
            raise SystemExit(
                f"--docker-context must point to a directory: {build_context}"
            )
        try:
            context_dir.resolve(strict=False).relative_to(build_context.resolve())
        except ValueError as exc:
            raise SystemExit(
                "--docker-context must contain the generated .project-sandbox directory"
            ) from exc
        return None, base_dockerfile, build_context

    if not args.base_image:
        raise SystemExit("base_image is required unless --dockerfile is used")
    return args.base_image, None, context_dir


def _detect_cargo_workspace(project: Path) -> tuple[bool, list[str], bool]:
    """Parse Cargo.toml to detect a Rust workspace and its member crates.

    Returns (is_workspace, member_paths, root_is_package):
    - is_workspace: True if the root Cargo.toml has a [workspace] table
    - member_paths: sorted relative paths of member crates that have a Cargo.toml
    - root_is_package: True if the root Cargo.toml also has a [package] table
    """
    import tomllib

    try:
        data = tomllib.loads((project / "Cargo.toml").read_text(encoding="utf-8"))
    except Exception:
        return False, [], False

    workspace = data.get("workspace")
    if workspace is None:
        return False, [], False

    root_is_package = "package" in data
    excludes = set(workspace.get("exclude", []))
    members: list[str] = []
    for pattern in workspace.get("members", []):
        for member_path in sorted(project.glob(pattern)):
            rel = member_path.relative_to(project).as_posix()
            if rel in excludes or rel == ".":
                continue
            if (member_path / "Cargo.toml").exists():
                members.append(rel)
    return True, members, root_is_package


def _tracked_project_dockerfiles(
    base_dockerfile: Path | None, context_dir: Path
) -> list[Path]:
    """Return the project Dockerfiles whose checksum is worth tracking.

    Only a user-supplied ``--dockerfile`` that lives outside ``.project-sandbox``
    qualifies: it sits in the writable workspace where an agent could rewrite it.
    Generated Dockerfiles under ``.project-sandbox`` are masked inside the sandbox
    and so cannot be tampered with, and the bare ``base_image`` flow has no
    project Dockerfile at all.
    """
    if base_dockerfile is None:
        return []
    sandbox_dir = context_dir.resolve(strict=False)
    try:
        base_dockerfile.resolve(strict=False).relative_to(sandbox_dir)
    except ValueError:
        return [base_dockerfile]
    return []


def _validate_session_inputs(args) -> None:
    """Validate fatal session inputs before any worktree is created.

    These checks otherwise fire inside ``_build_session_command``, after a
    worktree (and its branch) may already exist. This is non-mutating: nothing is
    written or staged here — the actual prompt/log handling still happens later.
    """
    if args.prompt:
        source_prompt = resolve_strict(args.prompt)
        if not source_prompt.is_file():
            raise SystemExit(f"--prompt must point to a file: {source_prompt}")
    if args.log:
        log_parent = Path(args.log).expanduser().resolve().parent
        if not log_parent.is_dir():
            raise SystemExit(f"--log parent directory does not exist: {log_parent}")


def _allow_github(args, run_agent: str | None) -> bool:
    return bool(args.allow_github or _uses_github_copilot_cli(args, run_agent))


def _uses_github_copilot_cli(args, run_agent: str | None) -> bool:
    if run_agent == "copilot":
        return True
    if run_agent != "bash":
        return False
    command = args.prompt_text
    if command is None and args.prompt:
        try:
            command = resolve_strict(args.prompt).read_text(encoding="utf-8")
        except OSError:
            return False
    if command is None:
        return False
    try:
        words = shlex.split(command, comments=True)
    except ValueError:
        return False
    return bool(words and words[0] == "copilot")


def _warn_opencode_provider_allowlist(args, run_agent: str | None) -> None:
    if run_agent != "opencode" or args.no_firewall:
        return
    print(
        "[W] OpenCode provider network access depends on the selected provider. "
        "OpenAI and Anthropic endpoints are allowed by default; use --allow-github "
        "for GitHub Copilot or --extra-domain DOMAIN for another provider."
    )


def _setup_worktree(args, project: Path):
    """Return (Worktree | JjWorkspace | None, workspace_path). main() validates the project first."""
    if not args.branch:
        return None, project

    if (project / ".jj").is_dir():
        ws = jj_workspace_mod.setup(
            repo=project,
            bookmark=args.branch,
            base=args.worktree_base,
            workspace_dir=_worktree_dir(args),
        )
        return ws, ws.path

    wt = worktree_mod.setup(
        repo=project,
        branch=args.branch,
        base=args.worktree_base,
        worktree_dir=_worktree_dir(args),
    )
    return wt, wt.path


def _plan_worktree(args, project: Path):
    """Return a non-mutating placeholder and workspace path for dry-run."""
    if not args.branch:
        return None, project

    if (project / ".jj").is_dir():
        ws_path = jj_workspace_mod.path_for(
            project,
            args.branch,
            workspace_dir=_worktree_dir(args),
        )
        return jj_workspace_mod.JjWorkspace(path=ws_path, bookmark=args.branch), ws_path

    wt_path = worktree_mod.path_for(
        project,
        args.branch,
        worktree_dir=_worktree_dir(args),
    )
    return worktree_mod.Worktree(path=wt_path, branch=args.branch), wt_path


def _worktree_dir(args) -> Path | None:
    return Path(args.worktree_dir) if args.worktree_dir else None


def _validate_worktree_project(project: Path) -> None:
    if (project / ".jj").is_dir():
        return  # jj workspace support

    git_dir = project / ".git"
    if not git_dir.is_dir():
        raise SystemExit(
            "--branch requires a git or jj repo at the project root "
            "(.git is a file or missing — worktree-of-worktree and submodules are not supported)."
        )


def _teardown_worktree(args, *, project: Path, wt, exit_code: int) -> None:
    after = args.after_session
    if exit_code != 0:
        if after != "nothing":
            print(
                f"session exited {exit_code}; skipping {after} — worktree left at {wt.path}"
            )
        after = "nothing"
    _teardown_any(project, wt, after=after)


def _teardown_any(project: Path, wt, *, after: str) -> None:
    if isinstance(wt, jj_workspace_mod.JjWorkspace):
        jj_workspace_mod.teardown(project, wt, after=after)
    else:
        worktree_mod.teardown(project, wt, after=after)


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _warn_forwarded_credential_lifetime(
    *,
    run_mode_agent: str,
    credential_dirs: dict[str, Path],
    forward_credentials: bool,
) -> None:
    """Warn at session start about the remaining lifetime of forwarded credentials.

    Sessions are never killed; if one outlives the forwarded token the agent will
    log out inside the sandbox and the user must re-authenticate on the host and
    re-run. We surface that up front. Unknown lifetimes (e.g. opencode) are
    silent — there is nothing meaningful to show.
    """
    if not forward_credentials:
        return
    expiry = token_expiry.staged_token_expiry(credential_dirs, run_mode_agent)
    if expiry is None:
        return
    remaining_seconds = int(token_expiry.remaining(expiry).total_seconds())
    if remaining_seconds <= 0:
        print(
            "[!] Forwarded credentials have already expired; on first use the agent "
            "will refresh the token inside the sandbox, which logs you out on the "
            "host. You will likely need to re-login on the host."
        )
        return
    human = _format_duration(remaining_seconds)
    print(
        f"[!] Forwarded credentials are valid for ~{human}. If this session runs "
        "longer, the agent refreshes the token inside the sandbox, which logs you "
        "out on the host — you'll then need to re-authenticate on the host and re-run."
    )


def _build_session_command(
    args,
    *,
    project: Path,
    context_dir: Path,
    workspace: Path,
    worktree,
    identity,
    run_agent: str,
    claude_cfg: Path,
    credential_dirs: dict[str, Path],
    codex_cfg: Path,
    runtime: container_cli.Runtime,
    create_prompt_files: bool,
) -> tuple[list[str], Path | None, bool, list[str] | None]:
    # Agent availability is validated up front in main() via _ensure_agent_available.
    extra_mounts = list(args.extra_mounts)
    if worktree is not None:
        if isinstance(worktree, jj_workspace_mod.JjWorkspace):
            vcs_dir = (project / ".jj").resolve()
            conflict_msg = (
                f"--mount conflicts with the workspace .jj metadata mount at {vcs_dir}"
            )
            source, target = jj_workspace_mod.repo_store_mount(project, worktree.path)
            metadata_mounts = [f"type=bind,source={source},target={target}"]
            # An additional workspace's store points its git backend at the main
            # repo's git dir, which is outside the /workspace and .jj/repo mounts.
            # Mount it too, or every in-container `jj` command fails to open the
            # repo. (The default workspace gets it via the /workspace mount.)
            git_mount = jj_workspace_mod.git_backend_mount(project, worktree.path)
            if git_mount is not None:
                git_source, git_target = git_mount
                metadata_mounts.append(
                    f"type=bind,source={git_source},target={git_target}"
                )
        else:
            vcs_dir = (project / ".git").resolve()
            conflict_msg = (
                f"--mount conflicts with the worktree .git metadata mount at {vcs_dir}"
            )
            metadata_mounts = [f"type=bind,source={vcs_dir},target={vcs_dir}"]
        vcs_dir_str = str(vcs_dir)
        if any(vcs_dir_str in m for m in extra_mounts):
            raise SystemExit(conflict_msg)
        extra_mounts.extend(metadata_mounts)
    extra_env: list[str] = []
    extra_env.extend(_api_key_env(args, redact=not create_prompt_files))
    if not args.verbose:
        # Silence the in-container firewall/startup banner; the entrypoint still
        # surfaces firewall errors on failure.
        extra_env.append("PROJECT_SANDBOX_QUIET=1")
    else:
        # Have the entrypoint echo the resolved coding-agent config and argv, so
        # the actual --model/--effort the agent receives are visible.
        extra_env.append("PROJECT_SANDBOX_VERBOSE=1")
    # Model/effort apply in both interactive and headless runs; the entrypoint
    # branch for each agent turns these into the agent's own --model/--effort
    # flags (and ignores them for bash).
    if getattr(args, "model", None):
        extra_env.append(f"PROJECT_SANDBOX_MODEL={args.model}")
    if getattr(args, "effort", None):
        extra_env.append(f"PROJECT_SANDBOX_EFFORT={args.effort}")
    run_mode_agent = run_agent
    unsupervised = bool(args.prompt or args.prompt_text)
    log_path: Path | None = None
    # Unsupervised runs get a named container so that on timeout the runtime can
    # be told to stop it explicitly (rather than relying on SIGKILL to the CLI
    # process, which does not guarantee the backing VM is reclaimed).
    container_name = (
        f"project-sandbox-{uuid.uuid4().hex[:12]}" if unsupervised else None
    )
    container_stop_argv = (
        container_cli.build_stop_argv(runtime, container_name)
        if container_name is not None
        else None
    )

    if unsupervised:
        log_path = (
            Path(args.log).resolve()
            if args.log
            else session.default_log_path(
                project, args.branch, run_agent, create=create_prompt_files
            )
        )
        run_mode_agent = f"{run_agent}-headless"
        if args.prompt:
            source_prompt = resolve_strict(args.prompt)
            # Copy the prompt into a private staging dir so we mount only the
            # prompt file, not its source parent (which could be $HOME).
            prompt_staging = context_dir / "prompt"
            prompt_file = prompt_staging / source_prompt.name
            if create_prompt_files:
                ensure_dir(prompt_staging)
                shutil.copyfile(source_prompt, prompt_file)
            else:
                print(f"Would stage prompt to: {prompt_file}")
            prompt_target = f"{PROMPT_MOUNT_TARGET}/{source_prompt.name}"
            extra_mounts.append(
                f"type=bind,source={prompt_staging.resolve()},"
                f"target={PROMPT_MOUNT_TARGET},readonly"
            )
            extra_env.append(f"PROJECT_SANDBOX_PROMPT_FILE={prompt_target}")
        elif args.prompt_text:
            prompts_dir = context_dir / "prompts"
            prompt_file = prompts_dir / "prompt.txt"
            if create_prompt_files:
                ensure_dir(prompts_dir)
                prompt_file.write_text(args.prompt_text, encoding="utf-8")
            else:
                print(f"Would write prompt to: {prompt_file}")
            extra_mounts.append(
                f"type=bind,source={prompts_dir.resolve()},"
                f"target={PROMPT_MOUNT_TARGET},readonly"
            )
            extra_env.append(
                f"PROJECT_SANDBOX_PROMPT_FILE={PROMPT_MOUNT_TARGET}/prompt.txt"
            )

    if not unsupervised:
        shell_dir, claude_projects = ensure_history_paths(
            project, create=create_prompt_files
        )
        extra_mounts.append(
            f"type=bind,source={shell_dir.resolve()},target={HISTORY_SHELL_TARGET}"
        )
        extra_mounts.append(
            f"type=bind,source={claude_projects.resolve()},target={HISTORY_CLAUDE_PROJECTS_TARGET}"
        )
        extra_env.append(f"HISTFILE={HISTORY_HISTFILE}")

    workspace_mask = ensure_workspace_sandbox_mask(
        project,
        create=create_prompt_files,
    )
    if create_prompt_files:
        mask_source = workspace_mask.resolve(strict=False)
    else:
        mask_source = workspace_mask.resolve(strict=False)
        print(f"Would mask workspace sandbox files with: {mask_source}")
    # Keep this after user-supplied mounts so a writable --mount cannot expose
    # the generated files at /workspace/.project-sandbox again.
    extra_mounts.append(
        f"type=bind,source={mask_source},target={WORKSPACE_SANDBOX_TARGET},readonly"
    )
    # Hide the .devcontainer directory from inside the sandbox by masking it with
    # the same empty dir. It holds host-path mounts and config the agent has no
    # reason to read or edit. Only mask when it exists so the bind target is
    # present (apple/container rejects a bind onto a missing target).
    if (workspace / ".devcontainer").exists():
        extra_mounts.append(
            f"type=bind,source={mask_source},target={WORKSPACE_DEVCONTAINER_TARGET},readonly"
        )
    # Hide the Rust build artifact tree from the sandbox when using --rust-cargo.
    # CARGO_TARGET_DIR=/opt/cargo-target already redirects cargo output, but a
    # visible host target/ would expose thousands of files to the agent and allow
    # an agent that unsets CARGO_TARGET_DIR to write back into the host tree.
    if getattr(args, "rust_cargo", False) and (workspace / "target").exists():
        extra_mounts.append(
            f"type=bind,source={mask_source},target={WORKSPACE_CARGO_TARGET},readonly"
        )

    forward_credentials = not getattr(args, "no_forward_credentials", False)
    _warn_forwarded_credential_lifetime(
        run_mode_agent=run_mode_agent,
        credential_dirs=credential_dirs,
        forward_credentials=forward_credentials,
    )

    if args.verbose and run_agent != "bash":
        _print_agent_config(run_agent, args, unsupervised=unsupervised)

    return (
        container_cli.build_run_argv(
            runtime=runtime,
            image=args.image_tag,
            project_abs=workspace,
            claude_cfg=claude_cfg,
            claude_credentials_dir=credential_dirs.get("claude"),
            codex_cfg=codex_cfg,
            codex_credentials_dir=credential_dirs.get("codex"),
            opencode_credentials_dir=credential_dirs.get("opencode"),
            identity=identity,
            memory=args.memory,
            cpus=args.cpus,
            extra_mounts=extra_mounts,
            agent=run_mode_agent,
            firewall_enabled=not args.no_firewall,
            interactive=not unsupervised,
            extra_env=extra_env,
            container_name=container_name,
            forward_credentials=forward_credentials,
        ),
        log_path,
        unsupervised,
        container_stop_argv,
    )


def _print_agent_config(run_agent: str, args, *, unsupervised: bool) -> None:
    """Summarize the coding-agent config the container is launched with.

    Printed on --verbose so the resolved --model/--effort are visible host-side;
    the entrypoint echoes the same values (and the full argv) from inside the
    container, where a blank value reveals an env var that did not arrive.
    """
    mode = "headless" if unsupervised else "interactive"
    print(f"=== coding agent ({mode}) ===")
    print(f"  agent:  {run_agent}")
    print(f"  model:  {args.model or '(agent default)'}")
    print(f"  effort: {args.effort or '(agent default)'}")


def _print_next_steps(
    *,
    context_dir: Path,
    project: Path,
    available_agents: tuple[str, ...],
    launching: bool = False,
) -> None:
    print("\n=== project-sandbox ready ===")
    print(f"  Project:  {project}")
    print(f"  Sandbox:  {context_dir}")
    print()
    print("  Devcontainer:")
    print(f"    {project / '.devcontainer' / 'devcontainer.json'}")
    print("  → Open this project in VS Code / Cursor and choose 'Reopen in Container'.")
    print()
    if not launching:
        # When an agent is about to start, this hint is redundant noise.
        print("  To run an agent from the CLI:")
        for agent in available_agents:
            print(f"    project-sandbox --agent {agent} ...")
        print()


def _update_project_gitignore(project: Path) -> None:
    """Idempotently append credential-secret ignore entries to project .gitignore."""
    marker = "# project-sandbox — do not commit agent secrets"
    lines_to_add = [
        marker,
        ".project-sandbox/",
        ".devcontainer/",
    ]
    gi = project / ".gitignore"
    existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
    existing_lines = set(existing.splitlines())
    # Append only entries that are not already present anywhere in the file, so a
    # project that already ignores .project-sandbox/ (without our marker) does not
    # get a duplicate line. The marker is itself an entry, so it is added only once.
    missing = [line for line in lines_to_add if line not in existing_lines]
    if not missing:
        return
    sep = "\n" if existing and not existing.endswith("\n") else ""
    gi.write_text(existing + sep + "\n".join(missing) + "\n", encoding="utf-8")


def _write_project_sandbox_gitignore(context_dir: Path) -> None:
    content = """*
!.gitignore
!claude/
!claude/settings.json
!claude-devcontainer/
!claude-devcontainer/settings.json
!codex/
!codex/config.toml
!codex-devcontainer/
!codex-devcontainer/config.toml
!init-firewall.sh
!init-firewall-devcontainer.sh
!Dockerfile
!Dockerfile.devcontainer
!entrypoint.sh
!project-sandbox-devcontainer-init
history/
"""
    (context_dir / ".gitignore").write_text(content, encoding="utf-8")
