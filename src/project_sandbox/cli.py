import hashlib
import re
import shutil
import uuid
from argparse import ArgumentParser
from pathlib import Path

from . import (
    config_agents,
    container_cli,
    devcontainer,
    dockerfile,
    firewall,
    session,
    transcript,
)
from . import (
    worktree as worktree_mod,
)
from .git_identity import read as read_identity
from .paths import (
    HISTORY_CLAUDE_PROJECTS_TARGET,
    HISTORY_HISTFILE,
    HISTORY_SHELL_TARGET,
    ensure_dir,
    ensure_history_paths,
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
    p.add_argument("--image-tag", default=None)
    p.add_argument(
        "--runtime",
        choices=list(container_cli.RUNTIME_CHOICES),
        default="auto",
        help="Container runtime for direct CLI runs (default: auto).",
    )
    p.add_argument("--no-build", action="store_true")
    p.add_argument("--memory", default="8g")
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--mount", dest="extra_mounts", action="append", default=[])
    p.add_argument("--extra-domain", action="append", default=[])
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
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.prompt and args.prompt_text:
        raise SystemExit("Use only one of --prompt or --prompt-text")
    run_agent = _requested_agent(args)
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

    wt, workspace = _setup_worktree(args, project) if run_agent else (None, project)

    # Once a worktree exists, every exit path must run teardown so a failed build
    # or early return does not orphan the worktree (and its branch). agent_ran
    # distinguishes a genuine session (honor --after-session) from a setup/build
    # failure before the agent ran (never integrate — just leave it in place).
    agent_ran = False
    exit_code = 1
    try:
        context_dir = ensure_dir(project / ".project-sandbox")
        base_image, base_dockerfile, build_context = _resolve_build_source(
            args,
            project=project,
            context_dir=context_dir,
        )

        dockerfile.render(
            context_dir,
            base_image=base_image,
            base_dockerfile=base_dockerfile,
            build_context=build_context,
            install_agents=available_agents,
            warn=print,
        )
        dockerfile.render_entrypoint(context_dir)
        dockerfile.render_devcontainer_entrypoint(context_dir)
        firewall.render(
            context_dir,
            extra_domains=args.extra_domain,
        )

        cfg = config_agents.render(context_dir)
        credential_dirs = config_agents.sync_credentials(context_dir)

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

        if not args.no_build:
            if not args.verbose:
                print("Building container image…")
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
            if run_agent == "claude":
                _write_transcript_markdown(log_path)
        else:
            exit_code = container_cli.run(cmd)

        return exit_code
    finally:
        if wt is not None:
            if agent_ran:
                _teardown_worktree(args, project=project, wt=wt, exit_code=exit_code)
            else:
                worktree_mod.teardown(project, wt, after="nothing")


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

    print("DRY RUN: no files, worktrees, images, or containers will be created.")
    if worktree is not None:
        print(f"Would use worktree at: {workspace}")
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
        if getattr(args, "python_uv", False):
            print(f"Would write synthesised Dockerfile: {base_dockerfile}")
        else:
            print(f"Would append sandbox layers to Dockerfile: {base_dockerfile}")
            for warning in dockerfile.source_warnings(base_dockerfile):
                print(warning)
        print(f"Would use build context: {build_context}")

    if run_agent is None:
        print("Would initialize config files only; no agent container would be started.")
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

    if python_version is not None and not python_uv:
        raise SystemExit("--python is only valid with --python-uv")

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
            raise SystemExit(f"--docker-context must point to a directory: {build_context}")
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


def _setup_worktree(args, project: Path):
    """Return (Worktree | None, workspace_path). main() validates the project first."""
    if not args.branch:
        return None, project

    wt = worktree_mod.setup(
        repo=project,
        branch=args.branch,
        base=args.worktree_base,
        worktree_dir=_worktree_dir(args),
    )
    return wt, wt.path


def _plan_worktree(args, project: Path):
    """Return a non-mutating Worktree placeholder and workspace path for dry-run."""
    if not args.branch:
        return None, project

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
        raise SystemExit("--branch is not yet supported for jj repos.")

    git_dir = project / ".git"
    if not git_dir.is_dir():
        raise SystemExit(
            "--branch requires a plain git repo at the project root "
            "(.git is a file or missing — worktree-of-worktree and submodules are not supported)."
        )


def _teardown_worktree(args, *, project: Path, wt, exit_code: int) -> None:
    after = args.after_session
    if exit_code != 0:
        if after != "nothing":
            print(f"session exited {exit_code}; skipping {after} — worktree left at {wt.path}")
        after = "nothing"
    worktree_mod.teardown(project, wt, after=after)


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
        git_dir_host = (project / ".git").resolve()
        git_dir_str = str(git_dir_host)
        if any(git_dir_str in m for m in extra_mounts):
            raise SystemExit(
                f"--mount conflicts with the worktree .git metadata mount at {git_dir_str}"
            )
        extra_mounts.append(f"type=bind,source={git_dir_str},target={git_dir_str}")
    extra_env: list[str] = []
    if not args.verbose:
        # Silence the in-container firewall/startup banner; the entrypoint still
        # surfaces firewall errors on failure.
        extra_env.append("PROJECT_SANDBOX_QUIET=1")
    run_mode_agent = run_agent
    unsupervised = bool(args.prompt or args.prompt_text)
    log_path: Path | None = None
    # Unsupervised runs get a named container so that on timeout the runtime can
    # be told to stop it explicitly (rather than relying on SIGKILL to the CLI
    # process, which does not guarantee the backing VM is reclaimed).
    container_name = f"project-sandbox-{uuid.uuid4().hex[:12]}" if unsupervised else None
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

    return (
        container_cli.build_run_argv(
            runtime=runtime,
            image=args.image_tag,
            project_abs=workspace,
            claude_cfg=claude_cfg,
            claude_credentials_dir=credential_dirs["claude"],
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
        ),
        log_path,
        unsupervised,
        container_stop_argv,
    )


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
