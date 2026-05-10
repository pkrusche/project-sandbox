from argparse import ArgumentParser
from pathlib import Path

from . import (
    config_claude,
    config_codex,
    container_cli,
    devcontainer,
    dockerfile,
    firewall,
    launcher,
    session,
)
from .git_identity import read as read_identity
from .paths import ensure_dir, resolve_strict

BRANCH_DISABLED_MESSAGE = (
    "--branch is temporarily disabled. Git worktree support needs a metadata-mount redesign "
    "so git commands work correctly inside the container."
)


def build_parser() -> ArgumentParser:
    p = ArgumentParser(prog="project-sandbox")
    p.add_argument("project")
    p.add_argument("base_image")
    p.add_argument("--image-tag", default="project-sandbox:latest")
    p.add_argument("--rebuild", action="store_true")
    p.add_argument("--refresh-config", action="store_true")
    p.add_argument("--no-build", action="store_true")
    p.add_argument("--memory", default="8g")
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--mount", dest="extra_mounts", action="append", default=[])
    p.add_argument("--extra-domain", action="append", default=[])
    p.add_argument("--no-firewall", action="store_true")
    p.add_argument("--firewall-allow-openai", action="store_true")
    p.add_argument(
        "--branch",
        help="Temporarily disabled until worktree metadata mounting is redesigned.",
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
    p.add_argument("--log")
    p.add_argument("--timeout", type=int)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.prompt and args.prompt_text:
        raise SystemExit("Use only one of --prompt or --prompt-text")
    if args.branch:
        raise SystemExit(BRANCH_DISABLED_MESSAGE)

    project = resolve_strict(args.project)
    identity = read_identity()
    install_claude = True
    install_codex = True

    if args.dry_run:
        return _dry_run(
            args,
            project=project,
            identity=identity,
            install_claude=install_claude,
            install_codex=install_codex,
        )

    context_dir = ensure_dir(project / ".project-sandbox")

    dockerfile.render(
        context_dir,
        base_image=args.base_image,
        install_claude=install_claude,
        install_codex=install_codex,
        refresh=args.rebuild,
    )
    dockerfile.render_entrypoint(context_dir, refresh=args.rebuild)
    dockerfile.render_devcontainer_entrypoint(context_dir, refresh=args.rebuild)
    firewall.render(
        context_dir,
        allow_openai=args.firewall_allow_openai or install_codex,
        extra_domains=args.extra_domain,
    )

    claude_cfg = config_claude.render(context_dir, refresh=args.refresh_config)
    codex_cfg = config_codex.render(context_dir, refresh=args.refresh_config)

    _write_project_sandbox_gitignore(context_dir)
    _update_project_gitignore(project)

    workspace = project

    if not args.devcontainer_only:
        rc = container_cli.ensure_system_started()
        if rc != 0:
            return rc

    if not args.no_build:
        rc = container_cli.build_image(
            context_dir=context_dir, image_tag=args.image_tag
        )
        if rc != 0:
            return rc

    claude_home_host = Path.home() / ".claude"
    codex_home_host = Path.home() / ".codex"

    devcontainer.render(
        project,
        identity=identity,
        install_claude=install_claude,
        install_codex=install_codex,
        firewall_enabled=not args.no_firewall,
        memory=args.memory,
        cpus=args.cpus,
        extra_mounts=args.extra_mounts,
        refresh=args.refresh_config,
    )

    script_dir = ensure_dir(context_dir / "bin")
    for agent in ["claude", "codex"]:
        launcher.render(
            output=script_dir / f"run-{agent}",
            image_tag=args.image_tag,
            memory=args.memory,
            cpus=args.cpus,
            project_abs=workspace,
            claude_settings_abs=claude_cfg,
            codex_config_abs=codex_cfg,
            claude_home_host_abs=claude_home_host
            if claude_home_host.exists()
            else None,
            codex_home_host_abs=codex_home_host if codex_home_host.exists() else None,
            firewall_enabled=not args.no_firewall,
            agent=agent,
            extra_envs=[],
        )

    run_agent = "claude"
    cmd, log_path, unsupervised = _build_session_command(
        args,
        project=project,
        context_dir=context_dir,
        workspace=workspace,
        identity=identity,
        run_agent=run_agent,
        claude_cfg=claude_cfg,
        codex_cfg=codex_cfg,
        create_prompt_files=True,
    )

    if not unsupervised:
        _print_next_steps(
            context_dir=context_dir,
            project=project,
            install_claude=install_claude,
            install_codex=install_codex,
        )

    if unsupervised:
        assert log_path is not None
        exit_code = session.run(cmd, log_path=log_path, timeout=args.timeout)
    else:
        exit_code = container_cli.run(cmd)

    return exit_code


def _dry_run(
    args, *, project: Path, identity, install_claude: bool, install_codex: bool
) -> int:
    context_dir = project / ".project-sandbox"
    claude_cfg = context_dir / "claude" / "settings.json"
    codex_cfg = context_dir / "codex" / "config.toml"
    run_agent = "claude"

    print("DRY RUN: no files, worktrees, images, or containers will be created.")
    print(f"Would render sandbox assets under: {context_dir}")
    print(f"Would render devcontainer under: {project / '.devcontainer'}")

    container_cli.ensure_system_started(dry_run=True)
    if not args.no_build:
        container_cli.build_image(
            context_dir=context_dir, image_tag=args.image_tag, dry_run=True
        )

    cmd, log_path, unsupervised = _build_session_command(
        args,
        project=project,
        context_dir=context_dir,
        workspace=project,
        identity=identity,
        run_agent=run_agent,
        claude_cfg=claude_cfg,
        codex_cfg=codex_cfg,
        create_prompt_files=False,
    )
    if unsupervised:
        assert log_path is not None
        session.run(cmd, log_path=log_path, timeout=args.timeout, dry_run=True)
    else:
        container_cli.run(cmd, dry_run=True)
    if install_claude or install_codex:
        print(f"Would write launcher scripts under: {context_dir / 'bin'}")
    return 0


def _build_session_command(
    args,
    *,
    project: Path,
    context_dir: Path,
    workspace: Path,
    identity,
    run_agent: str,
    claude_cfg: Path,
    codex_cfg: Path,
    create_prompt_files: bool,
) -> tuple[list[str], Path | None, bool]:
    extra_mounts = list(args.extra_mounts)
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

    claude_home_host = Path.home() / ".claude"
    codex_home_host = Path.home() / ".codex"
    return (
        container_cli.build_run_argv(
            image=args.image_tag,
            project_abs=workspace,
            claude_cfg=claude_cfg,
            codex_cfg=codex_cfg,
            claude_home_host=claude_home_host,
            codex_home_host=codex_home_host,
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


def _print_next_steps(
    *,
    context_dir: Path,
    project: Path,
    install_claude: bool,
    install_codex: bool,
) -> None:
    print("\n=== project-sandbox ready ===")
    print(f"  Project:  {project}")
    print(f"  Sandbox:  {context_dir}")
    print()
    print("  Generated launcher scripts:")
    if install_claude:
        print(f"    {context_dir / 'bin' / 'run-claude'}")
    if install_codex:
        print(f"    {context_dir / 'bin' / 'run-codex'}")
    print()
    print("  Devcontainer:")
    print(f"    {project / '.devcontainer' / 'devcontainer.json'}")
    print(
        "  → Open this project in VS Code / Cursor and choose 'Reopen in Container'."
    )
    print()
    print("  To run an agent interactively:")
    if install_claude:
        print(f"    {context_dir / 'bin' / 'run-claude'}")
    if install_codex:
        print(f"    {context_dir / 'bin' / 'run-codex'}")
    print()


def _update_project_gitignore(project: Path) -> None:
    """Idempotently append credential-secret ignore entries to project .gitignore."""
    marker = "# project-sandbox — do not commit agent secrets"
    lines_to_add = [
        marker,
        ".project-sandbox/claude/.credentials.json",
        ".project-sandbox/codex/auth.json",
    ]
    gi = project / ".gitignore"
    existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
    if marker in existing:
        return
    sep = "\n" if existing and not existing.endswith("\n") else ""
    gi.write_text(existing + sep + "\n".join(lines_to_add) + "\n", encoding="utf-8")


def _write_project_sandbox_gitignore(context_dir: Path) -> None:
    content = """*
!.gitignore
!claude/
!claude/settings.json
!codex/
!codex/config.toml
!init-firewall.sh
!Dockerfile
!entrypoint.sh
!project-sandbox-devcontainer-init
!bin/
!bin/run-claude
!bin/run-codex
"""
    (context_dir / ".gitignore").write_text(content, encoding="utf-8")
