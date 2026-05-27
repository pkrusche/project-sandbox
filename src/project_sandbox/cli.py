from argparse import ArgumentParser
from pathlib import Path

from . import (
    config_agents,
    container_cli,
    devcontainer,
    dockerfile,
    firewall,
    session,
)
from . import (
    worktree as worktree_mod,
)
from .git_identity import read as read_identity
from .paths import ensure_dir, resolve_strict

CONFIGURED_AGENTS = ("claude", "codex", "opencode")
SUPPORTED_AGENTS = (*CONFIGURED_AGENTS, "bash")


def _agent_host_paths() -> dict[str, Path]:
    home = Path.home()
    return {
        "claude": home / ".claude",
        "codex": home / ".codex",
        "opencode": home / ".config" / "opencode",
    }


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
    p.add_argument("--image-tag", default="project-sandbox:latest")
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
    identity = read_identity()
    host_paths = _agent_host_paths()
    available_agents = _available_agents(host_paths)

    if run_agent is not None and args.branch:
        _validate_worktree_project(project)
    if run_agent is not None:
        _ensure_agent_available(run_agent, available_agents)

    if args.dry_run:
        wt, workspace = _plan_worktree(args, project) if run_agent else (None, project)
        return _dry_run(
            args,
            project=project,
            workspace=workspace,
            worktree=wt,
            identity=identity,
            available_agents=available_agents,
            host_paths=host_paths,
        )

    wt, workspace = _setup_worktree(args, project) if run_agent else (None, project)

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

    claude_cfg = config_agents.render_claude(context_dir)
    config_agents.render_claude_devcontainer(context_dir)
    credential_dirs = _sync_agent_credentials(
        context_dir,
        available_agents=available_agents,
        host_paths=host_paths,
    )
    codex_cfg = config_agents.render_codex(context_dir)

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

    rc = container_cli.ensure_system_started()
    if rc != 0:
        print(
            "[W] Apple container system not running - if you're on a Mac, you may need to install or start it. Otherwise, you can still work with the devcontainer setup."
        )

    if not args.no_build:
        rc = container_cli.build_image(
            context_dir=context_dir,
            image_tag=args.image_tag,
            build_context=build_context,
            dockerfile_path=context_dir / "Dockerfile",
        )
        if rc != 0:
            return rc

    cmd, log_path, unsupervised = _build_session_command(
        args,
        project=project,
        context_dir=context_dir,
        workspace=workspace,
        worktree=wt,
        identity=identity,
        run_agent=run_agent,
        available_agents=available_agents,
        host_paths=host_paths,
        claude_cfg=claude_cfg,
        credential_dirs=credential_dirs,
        codex_cfg=codex_cfg,
        create_prompt_files=True,
    )

    if not unsupervised:
        _print_next_steps(
            context_dir=context_dir,
            project=project,
            available_agents=available_agents,
        )

    if unsupervised:
        assert log_path is not None
        exit_code = session.run(cmd, log_path=log_path, timeout=args.timeout)
    else:
        exit_code = container_cli.run(cmd)

    if wt is not None:
        _teardown_worktree(args, project=project, wt=wt, exit_code=exit_code)

    return exit_code


def _available_agents(host_paths: dict[str, Path]) -> tuple[str, ...]:
    configured = tuple(agent for agent in CONFIGURED_AGENTS if host_paths[agent].exists())
    return (*configured, "bash")


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
    host_paths: dict[str, Path],
) -> int:
    context_dir = project / ".project-sandbox"
    claude_cfg = context_dir / "claude" / "settings.json"
    credential_dirs = _agent_credential_dirs(context_dir, available_agents)
    codex_cfg = context_dir / "codex" / "config.toml"
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
    )
    if base_dockerfile is not None:
        print(f"Would append sandbox layers to Dockerfile: {base_dockerfile}")
        print(f"Would use build context: {build_context}")
        for warning in dockerfile.source_warnings(base_dockerfile):
            print(warning)

    if run_agent is None:
        print("Would initialize config files only; no agent container would be started.")
        return 0

    container_cli.ensure_system_started(dry_run=True)
    if not args.no_build:
        container_cli.build_image(
            context_dir=context_dir,
            image_tag=args.image_tag,
            build_context=build_context,
            dockerfile_path=context_dir / "Dockerfile",
            dry_run=True,
        )
    cmd, log_path, unsupervised = _build_session_command(
        args,
        project=project,
        context_dir=context_dir,
        workspace=workspace,
        worktree=worktree,
        identity=identity,
        run_agent=run_agent,
        available_agents=available_agents,
        host_paths=host_paths,
        claude_cfg=claude_cfg,
        credential_dirs=credential_dirs,
        codex_cfg=codex_cfg,
        create_prompt_files=False,
    )
    if unsupervised:
        assert log_path is not None
        session.run(cmd, log_path=log_path, timeout=args.timeout, dry_run=True)
    else:
        container_cli.run(cmd, dry_run=True)
    return 0


def _resolve_build_source(
    args,
    *,
    project: Path,
    context_dir: Path,
) -> tuple[str | None, Path | None, Path]:
    if args.docker_context and not args.dockerfile:
        raise SystemExit("--docker-context requires --dockerfile")

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
    """Return (Worktree | None, workspace_path)."""
    if not args.branch:
        return None, project

    _validate_worktree_project(project)

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

    _validate_worktree_project(project)
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
    if exit_code != 0 and after == "ask":
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
    available_agents: tuple[str, ...],
    host_paths: dict[str, Path],
    claude_cfg: Path,
    credential_dirs: dict[str, Path],
    codex_cfg: Path,
    create_prompt_files: bool,
) -> tuple[list[str], Path | None, bool]:
    if run_agent not in available_agents:
        available = ", ".join(available_agents)
        raise SystemExit(
            f"--agent={run_agent} is unavailable on this host; available agents: {available}"
        )

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
    run_mode_agent = run_agent
    unsupervised = bool(args.prompt or args.prompt_text)
    log_path: Path | None = None

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
            prompt_file = resolve_strict(args.prompt)
            extra_mounts.append(
                f"type=bind,source={prompt_file},target=/workspace/.project-sandbox-prompt,readonly"
            )
            extra_env.append(
                "PROJECT_SANDBOX_PROMPT_FILE=/workspace/.project-sandbox-prompt"
            )
        elif args.prompt_text:
            if len(args.prompt_text) <= 4096:
                extra_env.append(f"PROJECT_SANDBOX_PROMPT={args.prompt_text}")
            else:
                prompts_dir = context_dir / "prompts"
                long_prompt = prompts_dir / "prompt.txt"
                if create_prompt_files:
                    ensure_dir(prompts_dir)
                    long_prompt.write_text(args.prompt_text, encoding="utf-8")
                else:
                    print(f"Would write long prompt to: {long_prompt}")
                extra_mounts.append(
                    f"type=bind,source={long_prompt.resolve()},target=/workspace/.project-sandbox-prompt,readonly"
                )
                extra_env.append(
                    "PROJECT_SANDBOX_PROMPT_FILE=/workspace/.project-sandbox-prompt"
                )

    return (
        container_cli.build_run_argv(
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
        ),
        log_path,
        unsupervised,
    )


def _agent_credential_dirs(
    context_dir: Path, available_agents: tuple[str, ...]
) -> dict[str, Path]:
    return {
        agent: config_agents.credentials_dir(context_dir, agent)
        for agent in CONFIGURED_AGENTS
        if agent == "claude" or agent in available_agents
    }


def _sync_agent_credentials(
    context_dir: Path,
    *,
    available_agents: tuple[str, ...],
    host_paths: dict[str, Path],
) -> dict[str, Path]:
    credential_dirs = _agent_credential_dirs(context_dir, available_agents)
    config_agents.sync_credentials(context_dir)
    credential_dirs["claude"] = config_agents.credentials_dir(context_dir)
    credential_dirs["claude-devcontainer"] = config_agents.sync_credentials_devcontainer(context_dir)
    if "codex" in available_agents:
        credential_dirs["codex"] = config_agents.sync_agent_credentials(
            context_dir,
            "codex",
            host_paths["codex"],
            include_files=("auth.json",),
        )
    if "opencode" in available_agents:
        credential_dirs["opencode"] = config_agents.sync_opencode_credentials(
            context_dir,
            home=host_paths["opencode"].parents[1],
        )
    return credential_dirs


def _print_next_steps(
    *,
    context_dir: Path,
    project: Path,
    available_agents: tuple[str, ...],
) -> None:
    print("\n=== project-sandbox ready ===")
    print(f"  Project:  {project}")
    print(f"  Sandbox:  {context_dir}")
    print()
    print("  Devcontainer:")
    print(f"    {project / '.devcontainer' / 'devcontainer.json'}")
    print("  → Open this project in VS Code / Cursor and choose 'Reopen in Container'.")
    print()
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
    ]
    gi = project / ".gitignore"
    existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
    existing_lines = set(existing.splitlines())
    if marker in existing:
        missing = [line for line in lines_to_add if line not in existing_lines]
        if missing:
            sep = "\n" if existing and not existing.endswith("\n") else ""
            gi.write_text(existing + sep + "\n".join(missing) + "\n", encoding="utf-8")
        return
    sep = "\n" if existing and not existing.endswith("\n") else ""
    gi.write_text(existing + sep + "\n".join(lines_to_add) + "\n", encoding="utf-8")


def _write_project_sandbox_gitignore(context_dir: Path) -> None:
    content = """*
!.gitignore
!claude/
!claude/settings.json
!claude-devcontainer/
!claude-devcontainer/settings.json
!codex/
!codex/config.toml
!init-firewall.sh
!init-firewall-devcontainer.sh
!Dockerfile
!Dockerfile.devcontainer
!entrypoint.sh
!project-sandbox-devcontainer-init
"""
    (context_dir / ".gitignore").write_text(content, encoding="utf-8")
