from argparse import ArgumentParser
from pathlib import Path

from . import config_claude, config_codex, container_cli, devcontainer, dockerfile, firewall, launcher, session, worktree as worktree_mod
from .git_identity import read as read_identity
from .paths import ensure_dir, resolve_strict



def build_parser() -> ArgumentParser:
    p = ArgumentParser(prog="project-sandbox")
    p.add_argument("project")
    p.add_argument("base_image")
    p.add_argument("--agent", choices=["claude", "codex", "both"], default="both")
    p.add_argument("--image-tag", default="project-sandbox:latest")
    p.add_argument("--rebuild", action="store_true")
    p.add_argument("--refresh-config", action="store_true")
    p.add_argument("--no-build", action="store_true")
    p.add_argument("--memory", default="8g")
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--mount", dest="extra_mounts", action="append", default=[])
    p.add_argument("--credentials-mode", choices=["ro", "rw"], default="rw")
    p.add_argument("--extra-domain", action="append", default=[])
    p.add_argument("--no-firewall", action="store_true")
    p.add_argument("--no-ipv6-firewall", action="store_true")
    p.add_argument("--firewall-allow-openai", action="store_true")
    p.add_argument("--no-devcontainer", action="store_true")
    p.add_argument("--devcontainer-only", action="store_true")
    p.add_argument("--branch")
    p.add_argument("--worktree-base")
    p.add_argument("--worktree-dir")
    p.add_argument("--after-session", choices=["ask", "merge", "rebase", "pr", "nothing"], default="ask")
    p.add_argument("--prompt")
    p.add_argument("--prompt-text")
    p.add_argument("--log")
    p.add_argument("--timeout", type=int)
    p.add_argument("--dry-run", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.prompt and args.prompt_text:
        raise SystemExit("Use only one of --prompt or --prompt-text")
    if args.devcontainer_only and (args.prompt or args.prompt_text):
        raise SystemExit("--prompt/--prompt-text are not compatible with --devcontainer-only")

    project = resolve_strict(args.project)
    context_dir = ensure_dir(project / ".project-sandbox")

    identity = read_identity()
    install_claude = args.agent in ("claude", "both")
    install_codex = args.agent in ("codex", "both")

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
        no_ipv6_firewall=args.no_ipv6_firewall,
    )

    claude_cfg = config_claude.render(context_dir, refresh=args.refresh_config)
    codex_cfg = config_codex.render(context_dir, refresh=args.refresh_config)

    _write_project_sandbox_gitignore(context_dir)

    workspace = project
    wt = None
    if args.branch:
        wt = worktree_mod.setup(
            project,
            args.branch,
            base=args.worktree_base,
            worktree_dir=Path(args.worktree_dir).resolve() if args.worktree_dir else None,
        )
        workspace = wt.path.resolve()

    if not args.devcontainer_only and not args.no_build:
        rc = container_cli.build_image(context_dir=context_dir, image_tag=args.image_tag, dry_run=args.dry_run)
        if rc != 0:
            return rc

    ro_creds = args.credentials_mode == "ro"
    claude_home_host = Path.home() / ".claude"
    codex_home_host = Path.home() / ".codex"

    if not args.no_devcontainer:
        devcontainer.render(
            project,
            identity=identity,
            install_claude=install_claude,
            install_codex=install_codex,
            memory=args.memory,
            cpus=args.cpus,
            ro_creds=ro_creds,
            extra_mounts=args.extra_mounts,
            refresh=args.refresh_config,
        )

    if args.devcontainer_only:
        print(f"Devcontainer written to {project / '.devcontainer'}")
        return 0

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
            claude_home_host_abs=claude_home_host if claude_home_host.exists() else None,
            codex_home_host_abs=codex_home_host if codex_home_host.exists() else None,
            ro_creds=ro_creds,
            firewall_enabled=not args.no_firewall,
            agent=agent,
            extra_envs=[],
        )

    run_agent = "codex" if args.agent == "both" else args.agent
    cmd = container_cli.build_run_argv(
        image=args.image_tag,
        project_abs=workspace,
        claude_cfg=claude_cfg,
        codex_cfg=codex_cfg,
        claude_home_host=claude_home_host,
        codex_home_host=codex_home_host,
        identity=identity,
        memory=args.memory,
        cpus=args.cpus,
        ro_creds=ro_creds,
        extra_mounts=args.extra_mounts,
        agent=run_agent,
        firewall_enabled=not args.no_firewall,
        interactive=not (args.prompt or args.prompt_text),
    )

    if args.prompt or args.prompt_text:
        prompt_text = args.prompt_text
        if args.prompt:
            prompt_text = resolve_strict(args.prompt).read_text(encoding="utf-8")
        cmd += ["--env", f"PROJECT_SANDBOX_PROMPT={prompt_text or ''}"]
        cmd[-1], cmd[-2] = cmd[-2], cmd[-1]
        cmd[-1] = f"{run_agent}-headless"
        log_path = Path(args.log).resolve() if args.log else session.default_log_path(project, args.branch, run_agent)
        exit_code = session.run(cmd, log_path=log_path, timeout=args.timeout, dry_run=args.dry_run)
    else:
        exit_code = container_cli.run(cmd, dry_run=args.dry_run)

    if wt:
        after = args.after_session
        if (args.prompt or args.prompt_text) and after == "ask":
            print("WARNING: --after-session ask is not valid in unsupervised mode. Defaulting to 'nothing'.")
            after = "nothing"
        worktree_mod.teardown(project, wt, after=after)

    return exit_code


def _write_project_sandbox_gitignore(context_dir: Path) -> None:
    content = """*\n!claude/\n!claude/settings.json\n!codex/\n!codex/config.toml\n!init-firewall.sh\n!bin/\n!bin/run-claude\n!bin/run-codex\n!Dockerfile\n!entrypoint.sh\n!project-sandbox-devcontainer-init\n!sessions/\n"""
    (context_dir / ".gitignore").write_text(content, encoding="utf-8")
