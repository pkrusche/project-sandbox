"""``project-sandbox-egress``: manage the global egress sidecar of each runtime.

Subcommands (egress-sidecar-macos, design.md Decision 14): ``up``, ``down``,
``restart``, ``status``, ``reload``, ``allow list|add|remove`` and ``log``.
Each takes ``--runtime`` and defaults to the runtime ``project-sandbox``
selects. Exit codes: 0 success, 1 operational failure, 2 usage.

Policy edits change ``$XDG_CONFIG_HOME/project-sandbox/egress.toml`` (comments
kept) and apply to a running sidecar live, under the same lock as launches.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from . import (
    container_cli,
    egress_log,
    egress_policy,
    egress_relay,
    egress_sidecar,
    sidecar_image,
)

PROG = "project-sandbox-egress"
RUNTIMES = ("auto", "apple-container", "docker", "podman")


class OperationalError(RuntimeError):
    """A failure reported on stderr with exit code 1."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Manage the global egress sidecar that serves every "
            "`project-sandbox --egress sidecar` sandbox of a runtime. Policy: "
            f"{egress_policy.default_policy_path()}."
        ),
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)
    # Nested actions (`allow add`, `allow list`) accept --runtime too; SUPPRESS
    # keeps their default from overwriting a value given before the action.
    nested_runtime = argparse.ArgumentParser(add_help=False)
    nested_runtime.add_argument(
        "--runtime", choices=RUNTIMES, default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )

    def command(name: str, help_text: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help_text, description=help_text)
        sub.add_argument(
            "--runtime",
            choices=RUNTIMES,
            default="auto",
            help="Container runtime (default: the one project-sandbox selects).",
        )
        return sub

    command("up", "Start the sidecar from egress.toml unless it is running.")
    command(
        "down",
        "Stop the sidecar and its relay and delete its CA. Running sidecar "
        "sandboxes lose egress.",
    )
    restart = command(
        "restart",
        "Stop and start the sidecar with the current egress.toml. Running sidecar "
        "sandboxes lose egress.",
    )
    restart.add_argument(
        "--new-ca", action="store_true", help="Always generate a new CA."
    )
    status = command("status", "Report the sidecar's state; changes nothing.")
    status.add_argument("--json", action="store_true", help="Print JSON.")
    command(
        "reload",
        "Apply egress.toml's allowlist to the running sidecar without a restart.",
    )

    allow = command("allow", "List or edit the egress allowlist.")
    allow_actions = allow.add_subparsers(dest="action", metavar="ACTION", required=True)
    allow_actions.add_parser(
        "list", help="Print the effective allowlist.", parents=[nested_runtime]
    )
    for name, verb in (("add", "Allow"), ("remove", "Stop allowing")):
        sub = allow_actions.add_parser(
            name, help=f"{verb} hosts, applied live.", parents=[nested_runtime]
        )
        sub.add_argument("hosts", nargs="+", metavar="HOST")

    log = command("log", "Print the sidecar's access log.")
    log.add_argument("--follow", "-f", action="store_true", help="Keep printing.")
    log.add_argument(
        "--denied",
        action="store_true",
        help="Only denials, with their cause and attribution.",
    )
    log.add_argument(
        "--since",
        metavar="TIME",
        help="ISO 8601 time (local unless it has an offset) or a duration (10m, 2h).",
    )
    return parser


class Context:
    """What every subcommand needs; injectable for tests."""

    def __init__(
        self,
        runtime: container_cli.Runtime,
        *,
        deps: egress_sidecar.Collaborators | None = None,
        root: Path | None = None,
        out: Callable[[str], None] = print,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        build_image: Callable[..., str] | None = None,
        installed_image: str | None = None,
    ) -> None:
        self.runtime = runtime
        self.deps = deps or egress_sidecar.Collaborators()
        self.root = root
        self.out = out
        self.popen = popen
        self._build_image = build_image
        self._installed_image = installed_image

    def policy(self) -> egress_policy.PolicyFile:
        return egress_policy.load_global()

    def policy_path(self) -> Path:
        return egress_policy.default_policy_path()

    def installed_image(self) -> str:
        """The tag this version would run (computed, not built)."""
        return self._installed_image or sidecar_image.image_tag()

    def built_image(self) -> str:
        if self._build_image is not None:
            return self._build_image(self.runtime)
        return egress_sidecar.ensure_image(
            self.runtime,
            build=lambda **kw: container_cli.build_image(verbose=False, **kw),
            exists=container_cli.image_exists,
        )


# --------------------------------------------------------------------------
# Subcommands
# --------------------------------------------------------------------------


def _print_messages(ctx: Context, messages: Sequence[str]) -> None:
    for message in messages:
        ctx.out(message)


def cmd_up(ctx: Context, _args) -> int:
    sidecar = egress_sidecar.ensure(
        ctx.runtime,
        ctx.policy(),
        image=ctx.built_image(),
        root=ctx.root,
        deps=ctx.deps,
    )
    _print_messages(ctx, sidecar.messages)
    if sidecar.started:
        ctx.out(
            f"Started {egress_sidecar.CONTAINER} ({ctx.runtime.name}) on "
            f"{sidecar.endpoint}."
        )
    else:
        ctx.out(
            f"The egress sidecar is already up ({ctx.runtime.name}, "
            f"{sidecar.endpoint})."
        )
    return 0


def cmd_down(ctx: Context, _args) -> int:
    existed = egress_sidecar.down(ctx.runtime, root=ctx.root, deps=ctx.deps)
    if existed:
        ctx.out(
            f"Removed {egress_sidecar.CONTAINER}, its relay and its CA "
            f"({ctx.runtime.name}). Running sidecar-mode sandboxes have lost egress."
        )
    else:
        ctx.out(
            f"No {ctx.runtime.name} egress sidecar was running; removed any "
            "leftover relay and CA."
        )
    return 0


def cmd_restart(ctx: Context, args) -> int:
    sidecar = egress_sidecar.restart(
        ctx.runtime,
        ctx.policy(),
        image=ctx.built_image(),
        new_ca=args.new_ca,
        root=ctx.root,
        deps=ctx.deps,
    )
    _print_messages(ctx, sidecar.messages)
    ctx.out(
        f"Restarted {egress_sidecar.CONTAINER} ({ctx.runtime.name}) on "
        f"{sidecar.endpoint}. Sidecar sandboxes started before the restart lost "
        "their open connections; relaunch any that were built for another port."
    )
    return 0


def cmd_status(ctx: Context, args) -> int:
    try:
        policy = ctx.policy()
        policy_error = None
    except egress_policy.EgressPolicyError as exc:
        policy, policy_error = egress_policy.PolicyFile(), str(exc)
    report = egress_sidecar.status(
        ctx.runtime,
        policy,
        image=ctx.installed_image(),
        root=ctx.root,
        deps=ctx.deps,
    )
    if policy_error:
        report["drift"]["error"] = policy_error
    if args.json:
        ctx.out(json.dumps(report, indent=2, sort_keys=True))
    else:
        for line in egress_sidecar.status_lines(report):
            ctx.out(line)
    return 0


def _apply_live(ctx: Context, policy: egress_policy.PolicyFile) -> None:
    """Reload a running sidecar; say so when it is not running."""
    info = egress_sidecar.inspect(ctx.runtime, ctx.deps.runner)
    if info is None or not info.running:
        ctx.out(
            f"The {ctx.runtime.name} egress sidecar is not running; the change "
            "applies when it next starts (`project-sandbox-egress up` or a sidecar "
            "launch)."
        )
    else:
        messages = egress_sidecar.reload(
            ctx.runtime,
            policy,
            image=ctx.installed_image(),
            root=ctx.root,
            deps=ctx.deps,
        )
        _print_messages(
            ctx, messages or ["The running sidecar already matches egress.toml."]
        )
    ctx.out(
        "egress.toml is shared by every runtime: sidecars of other runtimes pick "
        "this up at their next launch or `project-sandbox-egress reload --runtime "
        "<runtime>`."
    )


def cmd_reload(ctx: Context, _args) -> int:
    messages = egress_sidecar.reload(
        ctx.runtime,
        ctx.policy(),
        image=ctx.installed_image(),
        root=ctx.root,
        deps=ctx.deps,
    )
    _print_messages(
        ctx, messages or ["The running sidecar already matches egress.toml."]
    )
    return 0


def cmd_allow(ctx: Context, args) -> int:
    path = ctx.policy_path()
    if args.action == "list":
        policy = ctx.policy()
        effective = egress_policy.effective(policy)
        providers = set(egress_policy.PROVIDER_HOSTS)
        copilot = {*egress_policy.COPILOT_HOSTS, egress_policy.COPILOT_API_HOST}
        for host in effective.allow:
            if host in providers:
                source = "  (agent provider, always allowed)"
            elif policy.copilot and host in copilot:
                source = "  (Copilot profile)"
            else:
                source = ""
            ctx.out(host + source)
        ctx.out(f"# policy: {policy.path or str(path) + ' (missing: defaults)'}")
        return 0
    if args.action == "add":
        policy, changed = egress_policy.add_allowed_hosts(path, args.hosts)
        ctx.out(("Allowed: " + ", ".join(changed)) if changed else "Already allowed.")
    else:
        policy, changed = egress_policy.remove_allowed_hosts(path, args.hosts)
        ctx.out("No longer allowed: " + ", ".join(changed))
    _apply_live(ctx, policy)
    return 0


def _log_filter(args, *, since: float | None, upstream: bool):
    def keep(line: str) -> str | None:
        ts = egress_log.line_time(line)
        if ts is None:
            return None  # cache.log lines share the stream
        if since is not None and ts < since:
            return None
        if args.denied:
            denial = egress_log.classify(line, upstream=upstream)
            return egress_log.format_denial(denial) if denial is not None else None
        return line.rstrip("\n")

    return keep


def cmd_log(ctx: Context, args) -> int:
    since = None
    if args.since is not None:
        try:
            since = egress_log.parse_since(args.since, now=time.time())
        except ValueError as exc:
            raise OperationalError(str(exc)) from None
    if egress_sidecar.inspect(ctx.runtime, ctx.deps.runner) is None:
        raise OperationalError(
            f"no egress sidecar exists for {ctx.runtime.name}; its log went with it"
        )
    state = egress_sidecar.read_state(egress_sidecar.layout(ctx.runtime, ctx.root))
    keep = _log_filter(
        args, since=since, upstream=bool((state or {}).get("upstream_url"))
    )
    argv = egress_sidecar.logs_argv(
        ctx.runtime,
        since=None if since is None else since - 60,
        follow=args.follow,
    )
    if not args.follow:
        result = ctx.deps.runner(argv)
        if result.returncode != 0:
            raise OperationalError(
                "reading the sidecar log failed: " + (result.stderr or "").strip()
            )
        text = (result.stdout or "") + (result.stderr or "")
        for line in text.splitlines():
            kept = keep(line)
            if kept is not None:
                ctx.out(kept)
        return 0
    process = ctx.popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    try:
        assert process.stdout is not None
        for line in process.stdout:
            kept = keep(line)
            if kept is not None:
                ctx.out(kept)
    except KeyboardInterrupt:
        pass
    finally:
        process.terminate()
    return 0


COMMANDS = {
    "up": cmd_up,
    "down": cmd_down,
    "restart": cmd_restart,
    "status": cmd_status,
    "reload": cmd_reload,
    "allow": cmd_allow,
    "log": cmd_log,
}


def main(
    argv: Sequence[str] | None = None,
    *,
    context: Callable[[container_cli.Runtime], Context] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        runtime = container_cli.select_runtime(args.runtime)
        ctx = context(runtime) if context is not None else Context(runtime)
        return COMMANDS[args.command](ctx, args)
    except (
        OperationalError,
        egress_sidecar.SidecarError,
        egress_policy.EgressPolicyError,
        egress_relay.RelayError,
    ) as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 1
    except SystemExit as exc:
        if isinstance(exc.code, str):  # runtime selection and lock errors
            print(f"{PROG}: {exc.code}", file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
