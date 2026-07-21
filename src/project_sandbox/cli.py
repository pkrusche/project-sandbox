import hashlib
import os
import re
import shlex
import shutil
import sys
import time
import uuid
from argparse import ArgumentParser
from datetime import datetime
from pathlib import Path

from . import __version__
from . import (
    build_cache,
    chroot,
    config_agents,
    container_cli,
    devcontainer,
    dockerfile,
    dockerfile_checksum,
    firewall,
    oauth_refresh,
    ollama_network,
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

SUPPORTED_AGENTS = ("claude", "codex", "opencode", "pi", "bash")
PROMPT_MOUNT_TARGET = "/project-sandbox-prompt"


def _default_image_tag(project: Path) -> str:
    resolved = project.resolve()
    name = re.sub(r"[^a-z0-9._-]", "-", resolved.name.lower())
    name = re.sub(r"-{2,}", "-", name).strip("-") or "project"
    path_hash = hashlib.sha256(str(resolved).encode()).hexdigest()[:8]
    return f"project-sandbox-{name}-{path_hash}:latest"


def build_parser() -> ArgumentParser:
    p = ArgumentParser(prog="project-sandbox")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
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
        "--pi-ollama",
        action="store_true",
        help=(
            "Extend the firewall to reach a host-run Ollama server and "
            "pre-configure Pi to use it as the default provider. Only takes "
            "effect together with --agent pi; a no-op otherwise."
        ),
    )
    p.add_argument(
        "--ollama-model",
        action="append",
        default=[],
        metavar="MODEL_ID",
        help=(
            "Ollama model ID to make available to Pi, overriding the built-in "
            "default list. Repeatable; only meaningful with --pi-ollama."
        ),
    )
    p.add_argument(
        "--branch",
        help=(
            "Run the agent in a git worktree / jj workspace on this branch "
            "(reused if it exists, else created)."
        ),
    )
    p.add_argument(
        "--branch-start-at",
        metavar="REVISION",
        help=(
            "Starting commit/tag/branch/bookmark for a NEW branch created by "
            "--branch. Errors if the branch/bookmark already exists (delete or "
            "merge it first, or omit this flag to reuse it)."
        ),
    )
    p.add_argument("--worktree-dir")
    p.add_argument(
        "--keep-workspace",
        action="store_true",
        help=(
            "Leave the worktree/workspace in place after the session so a later "
            "--branch run on the same branch can reuse it."
        ),
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
            "--prompt-text '...'. "
            "Example for Pi: --agent pi --model sonnet --prompt-text '...'."
        ),
    )
    p.add_argument(
        "--effort",
        choices=["low", "medium", "high", "xhigh", "max"],
        default=None,
        metavar="LEVEL",
        help=(
            "Reasoning effort level for Claude, Codex, OpenCode, and Pi, in both "
            "interactive and unsupervised (batch) runs. "
            "One of: low, medium, high, xhigh, max. project-sandbox does not "
            "force a default; when omitted, the underlying agent CLI's own "
            "default applies. "
            "Examples for Claude: --agent claude --model sonnet --effort low "
            "or --agent claude --model sonnet --effort high. "
            "Examples for Codex: --agent codex --model gpt-5.4-mini --effort low "
            "or --agent codex --model gpt-5.4-mini --effort high. "
            "Examples for OpenCode: --agent opencode --model openai/gpt-5.4-mini "
            "--effort low or --agent opencode --model openai/gpt-5.4-mini "
            "--effort high. "
            "Examples for Pi: --agent pi --model sonnet --effort low or "
            "--agent pi --model sonnet --effort high (Pi combines these into a "
            "single --model sonnet:high-shaped flag)."
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

    # Expand a leading "~" once, up front, so every later use of args.log (both
    # validation and session-command construction) agrees on the same absolute
    # path instead of one seeing the raw "~/..." string and the other an
    # already-expanded path.
    if args.log:
        args.log = str(Path(args.log).expanduser())

    if args.prompt and args.prompt_text:
        raise SystemExit("Use only one of --prompt or --prompt-text")
    if args.no_build and args.force_build:
        raise SystemExit("--no-build and --force-build are mutually exclusive")
    run_agent = _requested_agent(args)
    pi_ollama_enabled = _pi_ollama_enabled(args, run_agent)
    _validate_api_key_injection_args(args, run_agent)
    _validate_ollama_models(args.ollama_model)
    is_chroot = args.runtime == container_cli.CHROOT.name
    if is_chroot:
        _validate_chroot_session(run_agent)
    if args.branch and run_agent is None:
        raise SystemExit("--branch requires --agent, --prompt, or --prompt-text")
    if args.branch_start_at and not args.branch:
        raise SystemExit("--branch-start-at requires --branch")
    if args.keep_workspace and not args.branch:
        raise SystemExit("--keep-workspace requires --branch")

    project = _resolve_required_path(args.project, what="project path")
    if args.image_tag is None:
        args.image_tag = _default_image_tag(project)
    identity = read_identity()
    available_agents = config_agents.available_agents()
    # --no-forward-credentials promises to start unauthenticated and let the
    # user log in inside the sandbox, so the requested agent must be treated
    # as available/installable even when its host config dir is missing —
    # that config dir is exactly what --no-forward-credentials means to not
    # depend on.
    if (
        run_agent is not None
        and args.no_forward_credentials
        and run_agent not in available_agents
    ):
        available_agents = (*available_agents, run_agent)

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
    if is_chroot:
        base_image, base_dockerfile, build_context = None, None, context_dir
    else:
        base_image, base_dockerfile, build_context = _resolve_build_source(
            args, project=project, context_dir=context_dir
        )
    if run_agent is not None:
        _validate_session_inputs(args)
    # Resolve the injected API key values once, before any worktree or image
    # work: a missing env var or env file fails fast here, and the argv
    # construction plus the subprocess environment below then agree on a
    # single snapshot instead of re-reading the files.
    api_key_values = _api_key_env_values(args)
    allow_github = _allow_github(args, run_agent)
    _warn_byok_provider_allowlist(args, run_agent)
    _warn_pi_ollama_no_firewall(args, pi_ollama_enabled)

    wt, workspace = _setup_worktree(args, project) if run_agent else (None, project)

    # Once a worktree exists, every exit path must run teardown so a failed build
    # or early return does not orphan the worktree (and its branch). agent_ran
    # distinguishes a genuine session (finalize the branch/bookmark) from a
    # setup/build failure before the agent ran (leave git in place, drop the
    # empty jj workspace).
    agent_ran = False
    ollama_plan: ollama_network.ForwardingPlan | None = None
    exit_code = 1
    try:
        if not is_chroot:
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
        if not is_chroot and (
            getattr(args, "python_uv", False) or getattr(args, "rust_cargo", False)
        ):
            dockerfile.render_dockerignore(context_dir, build_context=build_context)
        if not is_chroot:
            dockerfile.render_entrypoint(context_dir)
            dockerfile.render_devcontainer_entrypoint(context_dir)
            firewall.render(
                context_dir,
                extra_domains=args.extra_domain,
                allow_github=allow_github,
                pi_ollama=pi_ollama_enabled,
            )
        else:
            chroot.render(context_dir)

        cfg = config_agents.render(
            context_dir,
            pi_ollama=pi_ollama_enabled,
            ollama_models=args.ollama_model,
        )
        forward_credentials = not args.no_forward_credentials
        # Before staging, ask the agent's own CLI to refresh its host token so the
        # container starts with a near-full window. bash may run claude, so it
        # refreshes the claude token; opencode/pi have no delegated refresh (no-op).
        if run_agent is not None and forward_credentials and not args.no_token_refresh:
            oauth_refresh.refresh_host_token(
                "claude" if run_agent == "bash" else run_agent,
                home=Path.home(),
                verbose=args.verbose,
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

        # The devcontainer integration targets docker-style build/run tooling
        # (VS Code "Reopen in Container"); chroot renders no Dockerfile/firewall
        # for it to reference, so skip it rather than write dangling symlinks.
        if not is_chroot:
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
        if runtime.is_container and not args.no_verify_dockerfile:
            warnings = dockerfile_checksum.changed_warnings(
                context_dir, tracked_dockerfiles
            )
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
                try:
                    answer = input("Rebuild from the changed Dockerfile anyway? [y/N] ")
                except EOFError:
                    raise SystemExit(
                        "No input available to confirm rebuilding from the changed "
                        "Dockerfile. Use --no-verify-dockerfile to skip this check."
                    ) from None
                if answer.strip().lower() not in ("y", "yes"):
                    return 1

        if runtime.is_container and not args.no_build:
            # Reuse an existing image when its build inputs are unchanged. This
            # auto-skip is limited to the default flow where the build context is
            # the generated .project-sandbox dir, which fully determines the
            # image; whole-project contexts (--python-uv / --rust-cargo /
            # --dockerfile) keep building and rely on the runtime's layer
            # cache + the generated .dockerignore instead of fingerprinting an
            # arbitrary source tree.
            host_identity = container_cli.host_build_identity(runtime)
            fingerprint = build_cache.compute_fingerprint(
                context_dir,
                extra={
                    "image_tag": args.image_tag,
                    "base_image": base_image or "",
                    "host_identity": (
                        "default"
                        if host_identity is None
                        else f"{host_identity[0]}:{host_identity[1]}"
                    ),
                },
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
        elif runtime.is_container and not container_cli.image_exists(
            runtime, args.image_tag
        ):
            # Fail early with a clear message instead of letting `container run`
            # surface its own raw runtime error for a nonexistent image.
            raise SystemExit(
                f"--no-build was given but image {args.image_tag!r} does not exist; "
                "drop --no-build so the image can be built."
            )

        if pi_ollama_enabled:
            ollama_plan = ollama_network.prepare(runtime)
            if args.verbose:
                print(ollama_network.describe(ollama_plan))

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
            pi_cfg=cfg.get("pi"),
            runtime=runtime,
            create_prompt_files=True,
            api_key_values=api_key_values,
            ollama_add_host=ollama_plan.add_host if ollama_plan else None,
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

        if ollama_plan is not None:
            ollama_plan.start()
        agent_ran = True
        # Injected API key values are never baked into cmd's argv (see
        # _build_session_command); supply them through the subprocess
        # environment instead so they don't show up via `ps`/process listings.
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
                env=api_key_values or None,
            )
            if not args.verbose:
                print(f"Wrote {session.count_lines(log_path)} lines to {log_path}")
            if run_agent in ("claude", "codex"):
                _write_transcript_markdown(log_path)
        else:
            exit_code = container_cli.run(cmd, env=api_key_values or None)

        return exit_code
    finally:
        if ollama_plan is not None:
            ollama_plan.close()
        if wt is not None:
            if agent_ran:
                _finalize_worktree(args, project=project, wt=wt, exit_code=exit_code)
            elif isinstance(wt, jj_workspace_mod.JjWorkspace):
                jj_workspace_mod.remove(project, wt)


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


def _pi_ollama_enabled(args, run_agent: str | None) -> bool:
    return bool(args.pi_ollama and run_agent == "pi")


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
    pi_cfg = context_dir / "pi" / "settings.json"
    # Only the keys _build_session_command consumes ("claude", and optionally
    # "codex"/"opencode"/"pi" when available); the devcontainer-specific dirs
    # are not used on the CLI run path.
    credential_dirs = {
        "claude": config_agents.credentials_dir(context_dir, "claude"),
        **{
            agent: config_agents.credentials_dir(context_dir, agent)
            for agent in ("codex", "opencode", "pi")
            if agent in available_agents
        },
    }
    run_agent = _requested_agent(args)
    pi_ollama_enabled = _pi_ollama_enabled(args, run_agent)
    _warn_byok_provider_allowlist(args, run_agent)
    _warn_pi_ollama_no_firewall(args, pi_ollama_enabled)
    if args.runtime == container_cli.CHROOT.name:
        _validate_chroot_session(run_agent)

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
    preview_runtime = (
        container_cli.select_runtime(args.runtime, dry_run=True) if run_agent else None
    )
    if preview_runtime == container_cli.CHROOT:
        base_dockerfile, build_context = None, context_dir
    else:
        _, base_dockerfile, build_context = _resolve_build_source(
            args, project=project, context_dir=context_dir, write_generated=False
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

    assert preview_runtime is not None
    runtime = preview_runtime
    container_cli.ensure_system_started(
        runtime=runtime, dry_run=True, verbose=args.verbose
    )
    if runtime.is_container and not args.no_build:
        container_cli.build_image(
            runtime=runtime,
            context_dir=context_dir,
            image_tag=args.image_tag,
            build_context=build_context,
            dockerfile_path=context_dir / "Dockerfile",
            dry_run=True,
            verbose=args.verbose,
        )
    ollama_plan = (
        ollama_network.prepare(runtime, dry_run=True) if pi_ollama_enabled else None
    )
    if ollama_plan is not None:
        print(f"Would use {ollama_network.describe(ollama_plan)}")
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
        pi_cfg=pi_cfg,
        runtime=runtime,
        create_prompt_files=False,
        ollama_add_host=ollama_plan.add_host if ollama_plan else None,
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


def _validate_chroot_session(run_agent: str | None) -> None:
    if run_agent != "bash":
        raise SystemExit("--runtime chroot requires --agent bash")


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


def _validate_ollama_models(ollama_models: list[str]) -> None:
    for model_id in ollama_models:
        if not model_id.strip():
            raise SystemExit("--ollama-model must not be empty or whitespace")


def _api_key_env_values(args) -> dict[str, str]:
    """Resolve the requested --api-key-env / --api-key-env-file values.

    Returns an actual name -> value mapping. Callers must not bake these
    values into subprocess argv (visible via `ps`/process listings); instead
    pass bare names in argv (e.g. docker/podman `--env NAME`) and supply this
    mapping through the subprocess environment so the runtime CLI inherits it
    from project-sandbox's own process environment.
    """
    values: dict[str, str] = {}
    for env_file in getattr(args, "api_key_env_file", []):
        values.update(
            _read_api_key_env_file(
                _resolve_required_path(env_file, what="--api-key-env-file")
            )
        )
    for name in getattr(args, "api_key_env", []):
        _validate_env_name(name, source="--api-key-env")
        if name not in os.environ:
            raise SystemExit(
                f"--api-key-env {name}: host environment variable is not set"
            )
        values[name] = os.environ[name]
    return values


def _write_staged_api_key_env_file(path: Path, values: dict[str, str]) -> None:
    """Stage resolved --api-key-env values as a key=value env file, mode 0600.

    Used for runtimes (apple `container`) whose CLI is not documented to
    inherit a bare ``--env NAME`` from the client environment; the file keeps
    the secret out of argv while still reaching the container via --env-file.
    """
    for name, value in values.items():
        if "\n" in value or "\r" in value:
            raise SystemExit(
                f"--api-key-env {name}: value contains a newline and cannot be "
                "passed via an env file"
            )
    path.unlink(missing_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("".join(f"{name}={value}\n" for name, value in values.items()))


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
        base_dockerfile = _resolve_required_path(args.dockerfile, what="--dockerfile")
        if not base_dockerfile.is_file():
            raise SystemExit(f"--dockerfile must point to a file: {base_dockerfile}")
        build_context = (
            _resolve_required_path(args.docker_context, what="--docker-context")
            if args.docker_context
            else project
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


def _resolve_required_path(path_str: str, *, what: str) -> Path:
    """Resolve a required, user-supplied path, converting a missing path into a
    clean SystemExit instead of a raw FileNotFoundError traceback."""
    try:
        return resolve_strict(path_str)
    except FileNotFoundError:
        raise SystemExit(f"{what} does not exist: {path_str}") from None


def _validate_session_inputs(args) -> None:
    """Validate fatal session inputs before any worktree is created.

    These checks otherwise fire inside ``_build_session_command``, after a
    worktree (and its branch) may already exist. This is non-mutating: nothing is
    written or staged here — the actual prompt/log handling still happens later.
    """
    if args.prompt:
        source_prompt = _resolve_required_path(args.prompt, what="--prompt file")
        if not source_prompt.is_file():
            raise SystemExit(f"--prompt must point to a file: {source_prompt}")
    if args.log:
        log_parent = Path(args.log).expanduser().resolve().parent
        if not log_parent.is_dir():
            raise SystemExit(f"--log parent directory does not exist: {log_parent}")


def _allow_github(args, run_agent: str | None) -> bool:
    return bool(args.allow_github or _uses_github_copilot_cli(args, run_agent))


def _uses_github_copilot_cli(args, run_agent: str | None) -> bool:
    # "copilot" is not a supported --agent value (see SUPPORTED_AGENTS); the
    # only way to run the GitHub Copilot CLI is via `--agent bash` with a
    # prompt that invokes it, detected below.
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


def _warn_byok_provider_allowlist(args, run_agent: str | None) -> None:
    if (
        run_agent not in ("opencode", "pi")
        or args.no_firewall
        or (run_agent == "pi" and args.pi_ollama)
    ):
        return
    agent_label = "OpenCode" if run_agent == "opencode" else "Pi"
    print(
        f"[W] {agent_label} provider network access depends on the selected provider. "
        "OpenAI and Anthropic endpoints are allowed by default; use --allow-github "
        "for GitHub Copilot or --extra-domain DOMAIN for another provider."
    )


def _warn_pi_ollama_no_firewall(args, pi_ollama_enabled: bool) -> None:
    # Both the port-scoped gateway allow rule and the
    # ollama.project-sandbox.internal /etc/hosts pin live in the firewall
    # script; with --no-firewall neither runs, so Pi's baked config points at
    # a hostname nothing inside the container resolves.
    if not pi_ollama_enabled or not args.no_firewall:
        return
    print(
        "[W] --pi-ollama with --no-firewall: the ollama.project-sandbox.internal "
        "hostname baked into Pi's config will not resolve, since the /etc/hosts "
        "pin and gateway route are only set up as part of firewall initialization. "
        "Drop --no-firewall, or point Pi at Ollama manually."
    )


def _setup_worktree(args, project: Path):
    """Return (Worktree | JjWorkspace | None, workspace_path). main() validates the project first."""
    if not args.branch:
        return None, project

    if (project / ".jj").is_dir():
        ws = jj_workspace_mod.setup(
            repo=project,
            bookmark=args.branch,
            start_at=args.branch_start_at,
            workspace_dir=_worktree_dir(args),
        )
        return ws, ws.path

    wt = worktree_mod.setup(
        repo=project,
        branch=args.branch,
        start_at=args.branch_start_at,
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


def _finalize_worktree(args, *, project: Path, wt, exit_code: int) -> None:
    session_failed = exit_code != 0
    message = _session_commit_message(
        wt.branch if _is_git_worktree(wt) else wt.bookmark
    )
    if isinstance(wt, jj_workspace_mod.JjWorkspace):
        jj_workspace_mod.finalize(
            project,
            wt,
            keep_workspace=args.keep_workspace,
            session_failed=session_failed,
            message=message,
        )
    else:
        worktree_mod.finalize(
            project,
            wt,
            keep_workspace=args.keep_workspace,
            session_failed=session_failed,
            message=message,
        )


def _is_git_worktree(wt) -> bool:
    return not isinstance(wt, jj_workspace_mod.JjWorkspace)


def _session_commit_message(name: str) -> str:
    return f"{name} — {datetime.now():%Y-%m-%dT%H:%M}"


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


def _mount_touches_path(mount_value: str, vcs_dir: Path) -> bool:
    """Return True if a --mount value's source or target is ``vcs_dir`` itself
    or nested inside it.

    Structured comparison of parsed fields, not a raw substring search over the
    whole mount spec string: a mount like
    ``source=/repo/.git-backup,target=/x`` must NOT be flagged just because
    "/repo/.git" is a textual substring of "/repo/.git-backup".
    """
    try:
        spec = container_cli.parse_mount(mount_value)
    except SystemExit:
        # An invalid mount value is reported separately when it is actually
        # used; here it simply can't be judged as conflicting or not.
        return False
    # Raw (non-bind / partially modeled) mounts still carry any source/target
    # fields parse_mount saw, so e.g. a tmpfs mount targeting the metadata
    # path is caught here instead of failing later with the runtime's own
    # duplicate-mount error; absent fields are relative placeholders that can
    # never match the absolute vcs_dir.
    candidates = [spec.source]
    target_path = Path(spec.target)
    if target_path.is_absolute():
        candidates.append(target_path)
    return any(
        candidate == vcs_dir or vcs_dir in candidate.parents for candidate in candidates
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
    pi_cfg: Path | None = None,
    runtime: container_cli.Runtime,
    create_prompt_files: bool,
    api_key_values: dict[str, str] | None = None,
    ollama_add_host: str | None = None,
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
        if any(_mount_touches_path(m, vcs_dir) for m in extra_mounts):
            raise SystemExit(conflict_msg)
        extra_mounts.extend(metadata_mounts)
    extra_env: list[str] = []
    if api_key_values is None:
        api_key_values = _api_key_env_values(args)
    staged_api_key_env_file = context_dir / "api-keys.env"
    use_api_key_env_file = bool(api_key_values) and (
        runtime.name == container_cli.APPLE_CONTAINER.name
    )
    if use_api_key_env_file:
        # apple `container` documents only `--env key=value` and `--env-file`;
        # unlike docker/podman, a bare `--env NAME` is not documented to
        # inherit the value from the client's environment. Stage the values in
        # a 0600 env file instead, so the secret still never appears in argv.
        if create_prompt_files:
            _write_staged_api_key_env_file(staged_api_key_env_file, api_key_values)
        else:
            print(f"Would write API key env file: {staged_api_key_env_file}")
    else:
        if create_prompt_files:
            # Don't leave a previous apple-container run's staged keys behind.
            staged_api_key_env_file.unlink(missing_ok=True)
        if runtime.is_container:
            # Pass bare names (no "=VALUE") so docker/podman look the value up
            # from *this process's* environment — set via env= on the
            # subprocess call in main()/_dry_run() — instead of the secret
            # appearing directly in argv, where it would be visible via `ps`.
            extra_env.extend(api_key_values.keys())
    # For the chroot runtime there is no separate daemon to hand a bare name
    # to: the value must already be inherited from the parent process
    # environment through the unshare/env chain, so nothing is added to argv.
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
        if container_name is not None and runtime.is_container
        else None
    )

    if unsupervised:
        log_path = (
            Path(args.log).expanduser().resolve()
            if args.log
            else session.default_log_path(
                project, args.branch, run_agent, create=create_prompt_files
            )
        )
        run_mode_agent = f"{run_agent}-headless"
        if args.prompt:
            source_prompt = _resolve_required_path(args.prompt, what="--prompt file")
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
    mask_source = workspace_mask.resolve(strict=False)
    if not create_prompt_files:
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
    runtime_credential_dirs = config_agents.filter_credential_dirs(
        credential_dirs, run_mode_agent
    )
    _warn_forwarded_credential_lifetime(
        run_mode_agent=run_mode_agent,
        credential_dirs=credential_dirs,
        forward_credentials=forward_credentials,
    )

    if args.verbose and run_agent != "bash":
        _print_agent_config(run_agent, args, unsupervised=unsupervised)

    if runtime.is_container:
        # build_run_argv computes its own MountSpecs from these same inputs.
        command = container_cli.build_run_argv(
            runtime=runtime,
            image=args.image_tag,
            project_abs=workspace,
            claude_cfg=claude_cfg,
            claude_credentials_dir=runtime_credential_dirs.get("claude"),
            codex_cfg=codex_cfg,
            codex_credentials_dir=runtime_credential_dirs.get("codex"),
            opencode_credentials_dir=runtime_credential_dirs.get("opencode"),
            pi_credentials_dir=runtime_credential_dirs.get("pi"),
            pi_cfg=pi_cfg,
            identity=identity,
            memory=args.memory,
            cpus=args.cpus,
            extra_mounts=extra_mounts,
            agent=run_mode_agent,
            firewall_enabled=not args.no_firewall,
            interactive=not unsupervised,
            extra_env=extra_env,
            env_file=staged_api_key_env_file if use_api_key_env_file else None,
            container_name=container_name,
            forward_credentials=forward_credentials,
            add_hosts=[ollama_add_host] if ollama_add_host else (),
        )
    else:
        mounts = container_cli.build_mount_specs(
            project_abs=workspace,
            claude_cfg=claude_cfg,
            claude_credentials_dir=runtime_credential_dirs.get("claude"),
            codex_cfg=codex_cfg,
            codex_credentials_dir=runtime_credential_dirs.get("codex"),
            opencode_credentials_dir=runtime_credential_dirs.get("opencode"),
            pi_credentials_dir=runtime_credential_dirs.get("pi"),
            pi_cfg=pi_cfg,
            extra_mounts=extra_mounts,
            forward_credentials=forward_credentials,
            agent=run_mode_agent,
        )
        try:
            command = container_cli.build_chroot_argv(
                script=context_dir / "chroot-run.sh",
                jail_root=context_dir / "chroot-root",
                mounts=mounts,
                identity=identity,
                agent=run_mode_agent,
                extra_env=extra_env,
            )
        except ValueError as exc:
            raise SystemExit(f"Invalid --mount for --runtime chroot: {exc}") from exc
    return (
        command,
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
    # _update_project_gitignore (see below) unconditionally adds
    # ".project-sandbox/" to the *project's* root .gitignore on every run, and
    # git never descends into an already-ignored directory. So while that root
    # rule is in place, none of the "!"-negation patterns below can ever
    # re-include anything from inside .project-sandbox/ — they are inert by
    # construction. This is documented in the file itself (below) rather than
    # only in this comment, since it's the nested file a user would actually
    # open to understand why a file they expect to be tracked still isn't.
    content = """\
# These "!" patterns only have an effect if you have manually removed or
# edited the ".project-sandbox/" line from this project's root .gitignore.
# Git does not descend into an already-ignored directory, so as long as that
# root-level rule is in place, nothing below can re-include a file — it is
# inert. It only matters if you deliberately opt out of the root ignore rule
# and want to track select, non-secret generated files (Dockerfile,
# settings.json, etc.) instead of ignoring this whole directory.
*
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
"""
    (context_dir / ".gitignore").write_text(content, encoding="utf-8")
