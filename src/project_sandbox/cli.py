import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import sys
import threading
import time
from argparse import ArgumentParser
from collections.abc import Callable, Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from . import (
    __version__,
    agent_proxy,
    build_cache,
    build_network,
    build_proxy_forwarder,
    chroot,
    config_agents,
    container_cli,
    copilot_auth,
    devcontainer,
    dockerfile,
    dockerfile_checksum,
    doctor,
    egress_log,
    egress_policy,
    egress_sidecar,
    firewall,
    generated_files,
    implicit_mounts,
    internet_proxy,
    local_service_network,
    mount_options,
    mount_storage,
    oauth_refresh,
    observability,
    session,
    sidecar_image,
    token_expiry,
    transcript,
    vcs_access,
)
from . import (
    jj_workspace as jj_workspace_mod,
)
from . import (
    session_ca as session_ca_module,
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

SUPPORTED_AGENTS = ("claude", "codex", "opencode", "pi", "copilot", "bash")
FAST_MODE_AGENTS = ("claude", "codex")
PROMPT_MOUNT_TARGET = "/project-sandbox-prompt"
# Headless Chromium uses /dev/shm heavily; Docker's 64m default makes it crash.
NODE_NPM_DEFAULT_SHM_SIZE = "2g"
_SHM_SIZE_RE = re.compile(r"[1-9][0-9]*[bkmg]", re.IGNORECASE)
EGRESS_FIREWALL = "firewall"
EGRESS_SIDECAR = "sidecar"
EGRESS_MODES = (EGRESS_FIREWALL, EGRESS_SIDECAR)


def _default_image_tag(project: Path) -> str:
    resolved = project.resolve()
    # An OCI name component is alphanumeric runs joined by single separators
    # (".", "_", "__", or a run of "-"), so adjacent separators of different
    # kinds ("_-", ".-") and leading or trailing ones make the whole reference
    # invalid and the build fails before it starts. Collapse every separator
    # run to one "-" rather than only "--", which left a directory named e.g.
    # "audit.0fqiy38_" producing the rejected tag "...0fqiy38_-<hash>".
    name = re.sub(r"[^a-z0-9._-]", "-", resolved.name.lower())
    name = re.sub(r"[._-]{2,}", "-", name).strip("._-") or "project"
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
    p.add_argument(
        "--node-npm",
        action="store_true",
        help=(
            "Synthesise a Node.js/npm base Dockerfile with headless Chromium "
            "instead of supplying one. Requires package.json and a committed "
            "npm lockfile. Mutually exclusive with base_image and --dockerfile."
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
    p.add_argument(
        "--build-only",
        action="store_true",
        help="Build/warm the sandbox image and exit without starting an agent.",
    )
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
    p.add_argument(
        "--shm-size",
        default=None,
        metavar="SIZE",
        help=(
            "Shared memory (/dev/shm) size for the container, e.g. 2g "
            f"(default: {NODE_NPM_DEFAULT_SHM_SIZE} with --node-npm, otherwise "
            "the runtime's default)."
        ),
    )
    p.add_argument("--mount", dest="extra_mounts", action="append", default=[])
    p.add_argument(
        "--vcs-access",
        choices=vcs_access.MODES,
        default="full",
        help="Runtime access to existing .git/.jj metadata (default: full). Restricted modes keep source files writable.",
    )
    p.add_argument("--extra-domain", action="append", default=[])
    p.add_argument(
        "--allow-github",
        action="store_true",
        help=(
            "Allow GitHub and GitHub Copilot hosts at runtime. This includes "
            "GitHub's published web/API/git IP ranges and DNS-pinned GitHub "
            "Copilot endpoints. Implied by --agent copilot."
        ),
    )
    p.add_argument("--no-firewall", action="store_true")
    p.add_argument(
        "--build-proxy",
        metavar="URL",
        help=(
            "Use this credential-free HTTP/HTTPS proxy during image builds only. "
            "With Apple container it is reached through a host-loopback forwarder."
        ),
    )
    p.add_argument(
        "--build-no-proxy",
        action="append",
        default=[],
        metavar="HOSTS",
        help=(
            "Additional comma-separated no_proxy entries (hosts, .domains, IPs, "
            "CIDRs) during image builds (repeatable; requires --build-proxy)."
        ),
    )
    p.add_argument(
        "--build-ca-cert",
        action="append",
        default=[],
        metavar="PATH",
        help="Install PEM CAs (single certificate or bundle) before image build downloads (repeatable; trust persists).",
    )
    p.add_argument(
        "--ca-cert",
        action="append",
        default=[],
        metavar="PATH",
        help="Install a PEM CA after build commands (repeatable; requires --internet-proxy or --agent-proxy).",
    )
    p.add_argument(
        "--internet-proxy",
        metavar="URL",
        help="Route Internet traffic through a credential-free HTTP loopback or remote HTTP/HTTPS proxy (default ports 80/443).",
    )
    p.add_argument(
        "--egress",
        choices=EGRESS_MODES,
        default=EGRESS_FIREWALL,
        help=(
            "Internet egress mode. 'firewall' (default) pins allowed domains to IPs "
            "in the sandbox firewall. 'sidecar' routes all traffic through the "
            "runtime's global Squid sidecar, which filters by hostname under "
            "$XDG_CONFIG_HOME/project-sandbox/egress.toml and is managed with "
            "project-sandbox-egress; see docs/egress-sidecar.md."
        ),
    )
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
        "--agent-proxy",
        metavar="URL",
        help="Route pi, OpenCode, or Bash through an authenticated HTTP loopback or remote HTTP/HTTPS gateway; see docs/agent-proxy.md.",
    )
    p.add_argument(
        "--agent-proxy-key-env",
        default=agent_proxy.DEFAULT_KEY_ENV,
        metavar="NAME",
        help="Gateway-key environment fallback (default: AGENTGATEWAY_API_KEY).",
    )
    p.add_argument(
        "--agent-proxy-key",
        metavar="KEY",
        help="Unsafe last-resort gateway key; shell history and process listings may expose it.",
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
        "--pi-tools",
        metavar="TOOLS",
        help=(
            "Comma-separated Pi tool allowlist, e.g. --pi-tools read,grep,find,ls "
            "for a read-only session. Only takes effect together with --agent pi; "
            "when omitted, Pi's own default tool set applies."
        ),
    )
    p.add_argument(
        "--copilot-host",
        metavar="HOST",
        help=(
            "GitHub host for --agent copilot: github.com (default; personal, "
            "Business, and Enterprise Cloud accounts) or <tenant>.ghe.com "
            "(Enterprise Cloud with data residency). Self-hosted GitHub "
            "Enterprise Server is not supported."
        ),
    )
    p.add_argument(
        "--copilot-token-pass",
        metavar="ENTRY",
        help=(
            "pass entry holding the Copilot token for --agent copilot (default: "
            f"{copilot_auth.DEFAULT_PASS_ENTRY}, or "
            f"{copilot_auth.DEFAULT_PASS_ENTRY}/<host> for a GHE.com tenant). "
            "An explicit entry never falls back to `gh auth token`."
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
    p.add_argument(
        "--json-summary",
        metavar="PATH",
        help=(
            "Write a one-line JSON session summary to PATH after the agent exits; "
            "use '-' for stdout."
        ),
    )
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
            "Example for Pi: --agent pi --model sonnet --prompt-text '...'. "
            "Example for Copilot: --agent copilot --model gpt-5-mini "
            "--prompt-text '...'."
        ),
    )
    p.add_argument(
        "--effort",
        choices=["low", "medium", "high", "xhigh", "max"],
        default=None,
        metavar="LEVEL",
        help=(
            "Reasoning effort level for Claude, Codex, OpenCode, Pi, and Copilot, "
            "in both "
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
            "Examples for Pi: --agent pi --model anthropic/sonnet --effort low "
            "or --agent pi --model anthropic/sonnet --effort high (Pi receives "
            "the model unchanged plus a separate --thinking flag). "
            "Examples for Copilot: --agent copilot --model gpt-5-mini --effort low "
            "or --agent copilot --model gpt-5-mini --effort high (Copilot "
            "receives separate --model and --effort flags)."
        ),
    )
    p.add_argument(
        "--fast",
        action="store_true",
        help=(
            "Enable the agent's fast mode, in both interactive and unsupervised "
            "(batch) runs. Supported for Claude (fastMode setting) and Codex "
            '(service_tier = "fast"). Fast mode bills at a higher rate and '
            "only works with models and plans that offer it. "
            "Example: --agent claude --fast or --agent codex --fast."
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


@contextlib.contextmanager
def _exit_on_termination_signals() -> Generator[None]:
    """Turn SIGTERM/SIGHUP into SystemExit so cleanup ``finally`` blocks run.

    Python's default SIGTERM action exits without unwinding, which would leave
    headless containers behind. After the first signal, further ones are
    ignored so teardown itself is not interrupted.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    signums = (signal.SIGTERM, signal.SIGHUP)

    def handler(signum: int, _frame) -> NoReturn:
        for other in signums:
            signal.signal(other, signal.SIG_IGN)
        raise SystemExit(128 + signum)

    previous = {signum: signal.signal(signum, handler) for signum in signums}
    try:
        yield
    finally:
        for signum, action in previous.items():
            signal.signal(signum, action)


def main(argv: list[str] | None = None) -> int:
    with _exit_on_termination_signals():
        return _main(argv)


def _main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv[:2] == ["sessions", "list"]:
        extra = raw_argv[2:]
        if any(item != "--json" for item in extra):
            raise SystemExit("usage: project-sandbox sessions list [--json]")
        observability.print_sessions(as_json="--json" in extra)
        return 0
    if raw_argv[:1] == ["doctor"]:
        return doctor.main(raw_argv[1:])
    args = build_parser().parse_args(raw_argv)

    _validate_egress_args(args)
    sidecar = args.egress == EGRESS_SIDECAR
    internet_proxy_config = _validate_internet_proxy_args(args)

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
    if args.build_only and args.no_build:
        raise SystemExit("--build-only and --no-build are mutually exclusive")
    run_agent = _requested_agent(args)
    copilot_host = _validate_copilot_args(args, run_agent)
    pi_ollama_enabled = _pi_ollama_enabled(args, run_agent)
    proxy_port = _validate_agent_proxy_args(args, run_agent)
    proxy_endpoint = agent_proxy.parse(args.agent_proxy) if args.agent_proxy else None
    ca_certificates = dockerfile.read_ca_certificates(args.ca_cert)
    build_ca_certificates = dockerfile.read_ca_certificates(
        args.build_ca_cert, option="--build-ca-cert", allow_bundle=True
    )
    build_proxy = build_network.parse(args.build_proxy) if args.build_proxy else None
    build_no_proxy = build_network.parse_no_proxy(args.build_no_proxy)
    if build_no_proxy and build_proxy is None:
        raise SystemExit("--build-no-proxy requires --build-proxy")
    if args.runtime == "chroot" and (args.build_proxy or args.build_ca_cert):
        raise SystemExit(
            "--build-proxy, --build-no-proxy and --build-ca-cert require an "
            "image-based runtime"
        )
    _validate_api_key_injection_args(args, run_agent)
    _validate_fast_mode_args(args, run_agent)
    _validate_ollama_models(args.ollama_model)
    _validate_shm_size(args.shm_size)
    is_chroot = args.runtime == container_cli.CHROOT.name
    if is_chroot:
        _reject_image_only_flags_for_chroot(args)
        _validate_chroot_session(run_agent)
    if args.branch and run_agent is None:
        raise SystemExit("--branch requires --agent, --prompt, or --prompt-text")
    if args.branch_start_at and not args.branch:
        raise SystemExit("--branch-start-at requires --branch")
    if args.keep_workspace and not args.branch:
        raise SystemExit("--keep-workspace requires --branch")
    if args.build_only and args.branch:
        raise SystemExit("--build-only cannot be combined with --branch")
    if args.build_only and (args.prompt or args.prompt_text):
        raise SystemExit("--build-only cannot be combined with a prompt")
    if args.build_only and args.agent:
        raise SystemExit("--build-only cannot be combined with --agent")
    if args.json_summary and run_agent is None:
        raise SystemExit("--json-summary requires --agent, --prompt, or --prompt-text")

    project = _resolve_required_path(args.project, what="project path")
    if args.image_tag is None:
        args.image_tag = _default_image_tag(project)
    # --egress sidecar: the global policy, checked to lie outside every mount
    # before any worktree exists. The sandbox is then planned exactly like
    # firewall mode with --internet-proxy at the sidecar's published endpoint.
    egress: egress_policy.PolicyFile | None = None
    if sidecar:
        _, planned_workspace = (
            _plan_worktree(args, project) if run_agent else (None, project)
        )
        egress = _egress_sidecar_policy(
            args, project=project, workspace=planned_workspace, run_agent=run_agent
        )
        internet_proxy_config = internet_proxy.for_sidecar(egress.port)
    identity = read_identity()
    available_agents = config_agents.available_agents()
    # --no-forward-credentials promises to start unauthenticated and let the
    # user log in inside the sandbox, so the requested agent must be treated
    # as available/installable even when its host config dir is missing —
    # that config dir is exactly what --no-forward-credentials means to not
    # depend on.
    if (
        run_agent is not None
        and (args.no_forward_credentials or args.agent_proxy)
        and run_agent not in available_agents
    ):
        available_agents = (*available_agents, run_agent)
    # Copilot's credential is a GitHub token from pass/gh, not a host config
    # dir, so selecting it is enough to install it into the image.
    if run_agent == "copilot" and run_agent not in available_agents:
        available_agents = (*available_agents, run_agent)

    if run_agent is not None and args.branch:
        _validate_worktree_project(project)
    if run_agent is not None:
        _ensure_agent_available(run_agent, available_agents)
    runtime = (
        container_cli.select_runtime(args.runtime)
        if (run_agent is not None or args.build_only) and not args.dry_run
        else None
    )
    if args.vcs_access != "full":
        validation_runtime = runtime or (
            container_cli.select_runtime(args.runtime, dry_run=True)
            if run_agent or args.runtime == "chroot"
            else container_cli.DOCKER
        )
        try:
            vcs_access.validate(
                vcs_access.inspect(project),
                args.vcs_access,
                validation_runtime,
                args.extra_mounts,
            )
            if (
                args.branch
                and not (project / ".jj").is_dir()
                and validation_runtime == container_cli.APPLE_CONTAINER
            ):
                raise ValueError(
                    "Restricted Git --branch sessions require Docker or Podman because .git is a pointer file"
                )
        except (ValueError, OSError) as exc:
            raise SystemExit(str(exc)) from exc
    # Where the sidecar publishes on this runtime (egress-sidecar-linux
    # Decision 1); rootless Docker is rejected here, before any container work.
    egress_strategy: egress_sidecar.Strategy | None = None
    if sidecar and runtime is not None:
        try:
            egress_strategy = egress_sidecar.endpoint_strategy(runtime)
        except egress_sidecar.SidecarError as exc:
            raise SystemExit(str(exc)) from None
    # Builds and sessions share one pasta address so host-own LAN services
    # (proxies, gateways) stay reachable from both.
    podman_pasta = runtime is not None and local_service_network.uses_local_pasta(
        runtime
    )
    # What the sandbox connects to: the external proxy, or on Apple container
    # a host-loopback relay to it. The original stays authoritative for host
    # preflight and devcontainer configuration.
    sandbox_proxy_config = internet_proxy.for_runtime(internet_proxy_config, runtime)

    if args.dry_run:
        wt, workspace = _plan_worktree(args, project) if run_agent else (None, project)
        return _dry_run(
            args,
            project=project,
            workspace=workspace,
            worktree=wt,
            identity=identity,
            available_agents=available_agents,
            egress=egress,
        )

    # Fail before model discovery, secret loading, filesystem rendering, image
    # work, or owned forwarding resources when the external listener is down.
    # Build-only runs never start a sandbox and therefore need no listener.
    # The egress sidecar is started (or reused) and probed right before the
    # sandbox runs instead.
    if internet_proxy_config is not None and run_agent is not None and not sidecar:
        internet_proxy.preflight(internet_proxy_config, ca_certificates=ca_certificates)

    proxy_key: str | None = None
    proxy_models: list[str] = []
    proxy_base_url: str | None = None
    proxy_selected_model: str | None = None
    proxy_hostname = (
        proxy_endpoint.sandbox_host
        if proxy_endpoint
        else local_service_network.HOSTNAME
    )
    ollama_hostname = local_service_network.HOSTNAME
    ollama_base_url = f"http://{ollama_hostname}:{local_service_network.PORT}/v1"
    if args.agent_proxy:
        proxy_key, key_source = agent_proxy.resolve_key(
            args.agent_proxy_key_env, args.agent_proxy_key
        )
        if key_source == "command line":
            print(
                "[W] --agent-proxy-key was exposed in argv/shell history; prefer pass or an environment variable."
            )
        proxy_models = agent_proxy.discover_models(
            args.agent_proxy, proxy_key, ca_certificates=ca_certificates
        )
        proxy_selected_model = args.model.removeprefix("agent-proxy/")
        if proxy_selected_model not in proxy_models:
            raise SystemExit(
                f"Selected model {proxy_selected_model!r} is unavailable from the agent proxy"
            )
        proxy_base_url = agent_proxy.forwarded_url(
            args.agent_proxy, hostname=proxy_hostname
        )

    # Validate fatal inputs BEFORE creating a worktree, so a bad build source,
    # missing prompt, or unwritable log path fails without first orphaning a
    # branch/worktree. _resolve_build_source also writes the synthesised
    # python-uv/rust-cargo Dockerfile, but only into .project-sandbox
    # (project-level, not the worktree).
    implicit_mounts.validate_sources(project, extra_mounts=args.extra_mounts)
    _validate_generated_dirs(project)
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
    if run_agent == "bash" and proxy_base_url and proxy_key:
        api_key_values = {
            "OPENAI_BASE_URL": proxy_base_url,
            "OPENAI_API_KEY": proxy_key,
            "OPENAI_MODEL": args.model,
        }
    # Resolve the Copilot token after every other input is validated but before
    # any worktree, rendering, or image work, so a missing token fails cleanly.
    copilot_token = _resolve_copilot_token(args, run_agent, copilot_host)
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
    forwarding_plans: list[local_service_network.ForwardingPlan] = []
    internet_proxy_relay: build_proxy_forwarder.BuildProxyForwarder | None = None
    exit_code = 1
    session_id = ""
    container_name: str | None = None
    session_started_at: datetime | None = None
    # Doubles as the "record still open" marker: cleared once the session's
    # outcome has been written, so the finally block can close out a record left
    # behind by an interrupt or an exception. Stays None when the session could
    # not be recorded at all, which never blocks the run.
    record_path: Path | None = None
    copilot_token_staged = False
    # Owns this session's sidecar inputs (sessions/<sid>/) in sidecar mode; the
    # global sidecar itself keeps running after the session.
    egress_stack = contextlib.ExitStack()
    egress_state: egress_sidecar.Sidecar | None = None
    egress_started_at: float | None = None
    vcs_layout = None
    vcs_masks = None
    try:
        _validate_generated_dirs(workspace)
        if run_agent and args.vcs_access != "full":
            try:
                vcs_layout = vcs_access.inspect(workspace)
                vcs_access.validate(
                    vcs_layout, args.vcs_access, runtime, args.extra_mounts
                )
                if args.vcs_access == "hidden":
                    vcs_masks = vcs_access.new_mask_dir()
            except (ValueError, OSError) as exc:
                raise SystemExit(str(exc)) from exc
        if (
            run_agent is not None
            and internet_proxy_config is not None
            and sandbox_proxy_config is not internet_proxy_config
        ):
            # Bind before any image work: the firewall admits only this port,
            # so a busy port must fail now rather than after a long build.
            internet_proxy_relay = build_proxy_forwarder.BuildProxyForwarder(
                internet_proxy_config.endpoint,
                ca_certificates=ca_certificates,
                port=internet_proxy.relay_port(internet_proxy_config),
                fallback=False,
                label="Internet proxy",
                ca_option="--ca-cert",
            )
        if not is_chroot and (args.python_uv or args.node_npm) and args.branch:
            # The venv / npm workspace manifests must match the tree mounted at
            # /workspace. Keep generated
            # assets inside that build context so COPY paths remain valid.
            context_dir = ensure_dir(workspace / ".project-sandbox")
            base_image, base_dockerfile, build_context = _resolve_build_source(
                args, project=workspace, context_dir=context_dir
            )
        implicit_mounts.validate_sources(
            project,
            workspace=workspace,
            build_context=build_context,
            extra_mounts=args.extra_mounts,
        )
        if not is_chroot:
            dockerfile.render(
                context_dir,
                base_image=base_image,
                base_dockerfile=base_dockerfile,
                build_context=build_context,
                install_agents=available_agents,
                ca_certificates=ca_certificates,
                build_ca_certificates=build_ca_certificates,
                warn=print,
            )
        # Trim the whole-project build context only for the generated
        # python-uv/rust-cargo/node-npm flows, whose excluded paths we know are
        # not build inputs. User-supplied --dockerfile builds are left
        # untouched so an injected ignore file can't break a COPY they rely on.
        if not is_chroot and _synthesises_dockerfile(args):
            dockerfile.render_dockerignore(context_dir, build_context=build_context)
        if not is_chroot:
            dockerfile.render_entrypoint(context_dir, egress_sidecar=sidecar)
            dockerfile.render_devcontainer_entrypoint(context_dir)
            firewall.render(
                context_dir,
                extra_domains=args.extra_domain,
                allow_github=allow_github,
                pi_ollama=pi_ollama_enabled,
                ollama_hostname=ollama_hostname,
                agent_proxy_port=proxy_port,
                agent_proxy_hostname=proxy_hostname,
                agent_proxy_url=proxy_base_url,
                policy=(
                    firewall.INTERNET_PROXY
                    if internet_proxy_config is not None
                    else firewall.NORMAL
                ),
                local_destinations=_local_firewall_destinations(
                    sandbox_proxy_config,
                    proxy_port=proxy_port,
                    proxy_url=args.agent_proxy,
                    pi_ollama_enabled=pi_ollama_enabled,
                ),
                devcontainer_destinations=_local_firewall_destinations(
                    internet_proxy_config,
                    proxy_port=proxy_port,
                    proxy_url=args.agent_proxy,
                    pi_ollama_enabled=pi_ollama_enabled,
                ),
                copilot_tenant_hosts=_copilot_tenant_hosts(run_agent, copilot_host),
                egress_sidecar=sidecar,
            )
        else:
            chroot.render(context_dir)

        # Render only secret-free agent configuration before the image build.
        # The build context may be the whole project and synthesized Dockerfiles
        # use ``COPY . .``, so a proxy provider file rendered here could leak the
        # gateway key into the build context or an image layer.  This call also
        # removes secret-bearing proxy files left by an interrupted prior run.
        cfg = config_agents.render(
            context_dir,
            pi_ollama=pi_ollama_enabled,
            ollama_models=args.ollama_model,
            ollama_base_url=ollama_base_url,
        )
        forward_credentials = not args.no_forward_credentials and not args.agent_proxy
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
            credential_dirs = config_agents.sync_credentials(
                context_dir, unsupervised=bool(args.prompt or args.prompt_text)
            )
        else:
            # Read/copy no host credentials, and remove any staged by a previous
            # forwarding run so nothing lingers on disk or can be mounted.
            config_agents.purge_staged_credentials(context_dir)
            credential_dirs = {}

        _write_project_sandbox_gitignore(context_dir)
        _update_project_gitignore(context_dir.parent)

        # The devcontainer integration targets docker-style build/run tooling
        # (VS Code "Reopen in Container"); chroot renders no Dockerfile/firewall
        # for it to reference, so skip it rather than write dangling symlinks.
        if _renders_devcontainer(args, is_chroot=is_chroot):
            devcontainer.render(
                context_dir.parent,
                identity=identity,
                firewall_enabled=not args.no_firewall,
                memory=args.memory,
                cpus=args.cpus,
                extra_mounts=args.extra_mounts,
                vcs_mode=args.vcs_access,
                credential_dirs=credential_dirs,
                forward_credentials=forward_credentials,
                build_context=build_context,
                proxy_environment=(
                    internet_proxy.environment(
                        internet_proxy_config,
                        bypass_local_services=pi_ollama_enabled,
                        bypass_hosts=(proxy_endpoint.sandbox_host,)
                        if proxy_endpoint
                        else (),
                    )
                    if internet_proxy_config
                    else None
                ),
            )

        if run_agent is None and not args.build_only:
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
            # Hold the lock across the cache check and build. A concurrent first
            # caller waits, then observes the state and image written by the
            # winner instead of starting an identical build.
            with build_cache.build_lock(context_dir):
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
                    # Any forwarder starts only for an actual build, inside the
                    # build lock, and stops before any sandbox starts.
                    with _build_proxy_session(
                        args,
                        build_proxy,
                        no_proxy=build_no_proxy,
                        runtime=runtime,
                        ca_certificates=build_ca_certificates,
                    ) as build_proxy_environment:
                        rc = container_cli.build_image(
                            runtime=runtime,
                            context_dir=context_dir,
                            image_tag=args.image_tag,
                            build_context=build_context,
                            dockerfile_path=context_dir / "Dockerfile",
                            build_args=build_network.build_args(
                                build_proxy_environment or {}
                            ),
                            network=local_service_network.network_argument(
                                [], pasta=podman_pasta
                            ),
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

        if args.build_only:
            print(f"Image ready: {args.image_tag}")
            return 0

        assert run_agent is not None

        # The image build is complete, so it is now safe to stage the selected
        # agent's private proxy provider configuration in the project context.
        if proxy_base_url and proxy_key and run_agent:
            cfg = config_agents.render(
                context_dir,
                agent_proxy=(proxy_base_url, proxy_models, proxy_key, run_agent),
                agent_proxy_model=(
                    proxy_selected_model if run_agent == "bash" else None
                ),
            )

        if copilot_token is not None:
            # Staged outside the repo (0600 file, 0700 dir), rewritten every
            # launch, and removed in the finally block below.
            copilot_token_staged = True
            credential_dirs = {
                **credential_dirs,
                "copilot": copilot_auth.stage_token(context_dir, copilot_token),
            }

        services = _local_services(
            sandbox_proxy_config,
            proxy_port=(
                proxy_port
                if not args.agent_proxy or agent_proxy.parse(args.agent_proxy).local
                else None
            ),
            proxy_loopback_host=(
                agent_proxy.loopback_host(args.agent_proxy)
                if args.agent_proxy
                else None
            ),
            pi_ollama_enabled=pi_ollama_enabled,
        )
        sidecar_plan = (
            egress_sidecar.sandbox_plan(egress_strategy, sandbox_proxy_config.port)
            if egress_strategy is not None and sandbox_proxy_config is not None
            else None
        )
        if sidecar_plan is not None:
            # Docker Engine: the sidecar is published on the bridge gateway
            # itself, so its port needs no bridge forwarder; other local
            # services use the shared linux-bridge-forwarder.
            services = [s for s in services if s.label != "Internet proxy"]
        if services or sidecar_plan is not None:
            forwarding_plans = (
                local_service_network.prepare_services(runtime, services)
                if services
                else []
            )
            if sidecar_plan is not None:
                forwarding_plans.insert(0, sidecar_plan)
            if runtime == container_cli.APPLE_CONTAINER:
                print(local_service_network.apple_setup_notice("Local service"))
            if args.verbose:
                for plan in forwarding_plans:
                    print(local_service_network.describe(plan))

        if sandbox_proxy_config is not None:
            # Internet-proxy routing is part of the enforced sandbox policy,
            # not a user-overridable credential value.
            api_key_values = internet_proxy.merge_environment(
                api_key_values,
                sandbox_proxy_config,
                bypass_local_services=pi_ollama_enabled,
                bypass_hosts=(proxy_endpoint.sandbox_host,) if proxy_endpoint else (),
            )

        session_id = observability.new_session_id()
        session_container_name = (
            observability.container_name(session_id) if runtime.is_container else None
        )
        egress_mount = None
        if egress is not None:
            assert internet_proxy_config is not None
            egress_state = _ensure_egress_sidecar(
                runtime,
                egress,
                run_agent=run_agent,
                port=internet_proxy_config.port,
                verbose=args.verbose,
                strategy=egress_strategy,
            )
            egress_mount = _egress_session_inputs(
                egress_stack, egress_state, session_id=session_id
            )
            egress_started_at = egress_sidecar.sidecar_clock(
                runtime, egress_sidecar.default_runner
            )
        # Each live CLI session owns one private mask; cleanup also runs when
        # command construction or container startup fails.
        egress_stack.callback(
            mount_storage.cleanup_mask,
            mount_storage.mask_path(project, session_id),
            root=mount_storage.storage_root(),
        )
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
            copilot_cfg=cfg.get("copilot"),
            copilot_host=copilot_host,
            runtime=runtime,
            create_prompt_files=True,
            vcs_layout=vcs_layout,
            vcs_mask_dir=vcs_masks,
            api_key_values=api_key_values,
            ollama_add_hosts=list(
                dict.fromkeys(p.add_host for p in forwarding_plans if p.add_host)
            ),
            network=local_service_network.network_argument(
                forwarding_plans, pasta=podman_pasta
            ),
            session_id=session_id,
            egress_mount=egress_mount,
        )
        container_name = session_container_name

        print(f"Session: {session_id}")
        if session_container_name:
            print(f"Container: {session_container_name}")
        record_path = observability.start_record(
            session_id=session_id,
            container=session_container_name,
            project=project,
            workspace=workspace,
            agent=run_agent,
            runtime=runtime.name,
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

        waited = _wait_for_worktree_metadata(wt, runtime)
        if waited and args.verbose:
            print(
                f"Waited {waited:.1f}s for the new worktree metadata to become "
                f"visible to {runtime.name}"
            )

        # Lease (or start) the shared bridge forwarders; released in finally.
        for plan in forwarding_plans:
            plan.start()
        if internet_proxy_relay is not None:
            internet_proxy_relay.start()
            if args.verbose:
                print(
                    "Internet proxy forwarding: "
                    f"{build_proxy_forwarder.LISTEN_HOST}:{internet_proxy_relay.port} "
                    f"(sandbox URL http://{local_service_network.HOSTNAME}:"
                    f"{internet_proxy_relay.port})"
                )
        agent_ran = True
        session_started_at = datetime.now().astimezone()
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
                markdown_agent=run_agent,
                redact=(copilot_token,) if copilot_token else (),
            )
            if not args.verbose:
                print(f"Wrote {session.count_lines(log_path)} lines to {log_path}")
            if run_agent in ("claude", "codex", "opencode", "pi", "copilot"):
                _write_transcript_markdown(log_path, run_agent)
            if (
                run_agent == "pi"
                and exit_code == 0
                and observability.pi_json_stream_failed(log_path)
            ):
                # Pi's JSON output mode exits 0 even when the turn errored out.
                exit_code = 1
                print("[W] Pi reported a failed turn; reporting exit 1")
            if run_agent == "copilot":
                exit_code = _reclassify_copilot_exit(exit_code, log_path)
        else:
            exit_code = container_cli.run(cmd, env=api_key_values or None)

        rate_limited = (
            unsupervised
            and run_agent == "copilot"
            and exit_code == observability.RATE_LIMIT_EXIT_CODE
        ) or observability.is_rate_limited_failure(exit_code, log_path)
        if rate_limited:
            exit_code = observability.RATE_LIMIT_EXIT_CODE
            print(
                "[W] Agent rate limited (HTTP 429); returning temporary-failure exit 75"
            )
        observability.finish_record(
            record_path, exit_code=exit_code, rate_limited=rate_limited
        )
        record_path = None
        return exit_code
    finally:
        # The sandbox container has exited (--rm) by now; remove this session's
        # sidecar inputs and count what the shared sidecar denied meanwhile.
        egress_stack.close()
        if vcs_masks is not None:
            shutil.rmtree(vcs_masks)
        if egress_state is not None and agent_ran:
            assert runtime is not None
            _print_egress_denials(runtime, egress_state, started_at=egress_started_at)
        # An interrupted or crashed run never reaches finish_record above; close
        # the record here so it doesn't stay "running" forever (orphan detection
        # would otherwise rest on a PID that the OS is free to reuse).
        if record_path is not None:
            observability.finish_record(
                record_path, exit_code=exit_code, status="interrupted"
            )
        for plan in reversed(forwarding_plans):
            plan.close()
        if internet_proxy_relay is not None:
            internet_proxy_relay.close()
        if copilot_token_staged:
            copilot_auth.remove_staged_token(context_dir)
        if args.agent_proxy:
            for secret_file in (
                context_dir / "pi" / "models.json",
                context_dir / "opencode" / "opencode.json",
                context_dir / "api-keys.env",
            ):
                secret_file.unlink(missing_ok=True)
        if wt is not None:
            if agent_ran:
                capture_allowed = True
                if vcs_layout is not None:
                    try:
                        vcs_layout.check_roots()
                    except ValueError as exc:
                        print(f"[W] {exc}")
                        capture_allowed = False
                if capture_allowed:
                    _finalize_worktree(
                        args, project=project, wt=wt, exit_code=exit_code
                    )
            elif isinstance(wt, jj_workspace_mod.JjWorkspace):
                jj_workspace_mod.remove(project, wt)
        if agent_ran and args.json_summary:
            assert run_agent is not None
            _write_json_summary(
                args.json_summary,
                session_id=session_id,
                container_name=container_name,
                workspace=workspace,
                agent=run_agent,
                worktree=wt,
                project=project,
                exit_code=exit_code,
                started_at=session_started_at,
                ended_at=datetime.now().astimezone(),
            )


def _write_json_summary(
    destination: str,
    *,
    session_id: str,
    container_name: str | None,
    workspace: Path,
    agent: str,
    worktree,
    project: Path,
    exit_code: int,
    started_at: datetime | None,
    ended_at: datetime,
) -> None:
    """Best-effort: report the finished session as one JSON object.

    Runs from ``main()``'s ``finally`` block, so — like the markdown transcript
    — every failure is reported and swallowed: raising here would replace the
    session's own exit code with a traceback after the agent has already run.
    """
    try:
        _write_json_summary_unguarded(
            destination,
            session_id=session_id,
            container_name=container_name,
            workspace=workspace,
            agent=agent,
            worktree=worktree,
            project=project,
            exit_code=exit_code,
            started_at=started_at,
            ended_at=ended_at,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort sidecar, never fatal
        print(f"[W] Could not write JSON session summary: {exc}")


def _write_json_summary_unguarded(
    destination: str,
    *,
    session_id: str,
    container_name: str | None,
    workspace: Path,
    agent: str,
    worktree,
    project: Path,
    exit_code: int,
    started_at: datetime | None,
    ended_at: datetime,
) -> None:
    bookmark = None
    change_id = None
    if worktree is not None:
        bookmark = worktree.branch if _is_git_worktree(worktree) else worktree.bookmark
        if isinstance(worktree, jj_workspace_mod.JjWorkspace):
            change_id = jj_workspace_mod.change_id(project, worktree.bookmark)
    payload = {
        "session_id": session_id,
        "container_name": container_name,
        "workspace_path": str(workspace.resolve()),
        "agent": agent,
        "bookmark": bookmark,
        "jj_change_id": change_id,
        "exit_code": exit_code,
        "started_at": started_at.isoformat() if started_at else None,
        "ended_at": ended_at.isoformat(),
    }
    line = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    if destination == "-":
        print(line)
        return
    path = Path(destination).expanduser()
    generated_files.ensure_dir(path.parent)
    generated_files.write_text(path, line + "\n", mode=0o600)


def _write_transcript_markdown(log_path: Path, agent: str | None = None) -> None:
    """Best-effort: render a markdown transcript beside the JSON session log.

    Transcript generation must never fail the run, so any parsing/IO error is
    reported and swallowed.
    """
    try:
        md_path = transcript.log_to_markdown(log_path, agent)
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


def _validate_copilot_args(args, run_agent: str | None) -> str | None:
    """Validate Copilot options; return the normalized host for --agent copilot.

    Runs before any secret lookup, file write, or container start.
    """
    host_value = getattr(args, "copilot_host", None)
    pass_entry = getattr(args, "copilot_token_pass", None)
    if run_agent != "copilot":
        if host_value is not None or pass_entry is not None:
            raise SystemExit(
                "--copilot-host and --copilot-token-pass require --agent copilot"
            )
        return None
    if getattr(args, "agent_proxy", None):
        raise SystemExit(
            "--agent copilot cannot be combined with --agent-proxy; Copilot runs "
            "on a Copilot subscription only"
        )
    if getattr(args, "fast", False):
        raise SystemExit("--fast supports only --agent claude or --agent codex")
    if pass_entry is not None and (
        not pass_entry.strip() or pass_entry.startswith("-")
    ):
        raise SystemExit(f"Invalid --copilot-token-pass entry: {pass_entry!r}")
    host = copilot_auth.validate_host(
        host_value if host_value is not None else copilot_auth.DEFAULT_HOST
    )
    args.copilot_host = host
    return host


def _copilot_forwards_token(args, run_agent: str | None) -> bool:
    return run_agent == "copilot" and not getattr(args, "no_forward_credentials", False)


def _copilot_pass_entry(args, host: str) -> str:
    return getattr(args, "copilot_token_pass", None) or copilot_auth.default_pass_entry(
        host
    )


def _resolve_copilot_token(args, run_agent: str | None, host: str | None) -> str | None:
    """Resolve the Copilot token on the host (never under --dry-run).

    With --no-forward-credentials nothing is looked up: Copilot starts
    unauthenticated and the user logs in inside the sandbox.
    """
    if not _copilot_forwards_token(args, run_agent) or args.dry_run:
        return None
    assert host is not None
    entry = getattr(args, "copilot_token_pass", None)
    token, source = copilot_auth.resolve_token(host, entry)
    if source == copilot_auth.SOURCE_GH:
        print(copilot_auth.scope_warning(_copilot_pass_entry(args, host)))
    elif args.verbose:
        print(f"Copilot token: pass entry {_copilot_pass_entry(args, host)}")
    return token


def _copilot_tenant_hosts(run_agent: str | None, host: str | None) -> tuple[str, ...]:
    if run_agent != "copilot" or host is None:
        return ()
    return copilot_auth.tenant_hosts(host)


def _reclassify_copilot_exit(exit_code: int, log_path: Path | None) -> int:
    """Map an exit-0 Copilot run whose stream ended in an error to 1 (or 75)."""
    if exit_code != 0:
        return exit_code
    failure = observability.copilot_json_stream_failure(log_path)
    if failure == observability.COPILOT_RATE_LIMITED:
        print(
            "[W] Copilot reported a rate-limited failure; returning "
            "temporary-failure exit 75"
        )
        return observability.RATE_LIMIT_EXIT_CODE
    if failure == observability.COPILOT_FAILED:
        print("[W] Copilot reported a failed run; reporting exit 1")
        return 1
    return exit_code


def _pi_ollama_enabled(args, run_agent: str | None) -> bool:
    return bool(args.pi_ollama and run_agent == "pi")


def _validate_fast_mode_args(args, run_agent: str | None) -> None:
    if args.fast and run_agent not in FAST_MODE_AGENTS:
        raise SystemExit("--fast supports only --agent claude or --agent codex")


def _validate_agent_proxy_args(args, run_agent: str | None) -> int | None:
    if not args.agent_proxy:
        if (
            args.agent_proxy_key
            or args.agent_proxy_key_env != agent_proxy.DEFAULT_KEY_ENV
        ):
            raise SystemExit(
                "--agent-proxy-key-env/--agent-proxy-key require --agent-proxy"
            )
        return None
    if run_agent not in ("pi", "opencode", "bash"):
        raise SystemExit(
            "--agent-proxy supports only --agent pi, --agent opencode, or --agent bash"
        )
    if not args.model:
        raise SystemExit("--agent-proxy requires the regular --model option")
    _validate_env_name(args.agent_proxy_key_env, source="--agent-proxy-key-env")
    if args.no_firewall and agent_proxy.parse(args.agent_proxy).local:
        print(
            "[W] --agent-proxy with --no-firewall: its internal hostname may not resolve; firewall hostname pinning is disabled."
        )
    if args.pi_ollama:
        raise SystemExit("--agent-proxy and --pi-ollama are mutually exclusive")
    if args.api_key_env or args.api_key_env_file:
        raise SystemExit(
            "--agent-proxy cannot be combined with --api-key-env or --api-key-env-file"
        )
    if run_agent == "opencode":
        model = args.model
        if not model.startswith("agent-proxy/") or not model.removeprefix(
            "agent-proxy/"
        ):
            raise SystemExit(
                "OpenCode proxy models must use --model agent-proxy/<model-id>"
            )
    _path, port = agent_proxy.validate_url(args.agent_proxy)
    return port


def _validate_egress_args(args) -> None:
    """Reject unsupported --egress sidecar combinations before any work is done.

    Each message names what to use instead: an egress.toml key, a
    project-sandbox-egress command, or firewall egress mode.
    """
    if args.egress != EGRESS_SIDECAR:
        return
    policy_path = egress_policy.default_policy_path()
    if args.no_firewall:
        raise SystemExit(
            "--egress sidecar cannot be combined with --no-firewall: the sandbox "
            "firewall is what keeps traffic on the sidecar. Use --egress firewall "
            "--no-firewall for an unrestricted session."
        )
    if args.runtime == container_cli.CHROOT.name:
        raise SystemExit(
            "--egress sidecar does not support --runtime chroot; use a container "
            "runtime (apple-container, docker, or podman)"
        )
    if args.extra_domain:
        raise SystemExit(
            "--egress sidecar does not take --extra-domain: every sidecar sandbox "
            "shares the global egress policy. Allow the host for all of them with "
            "`project-sandbox-egress allow add "
            + " ".join(args.extra_domain)
            + f"` (edits {policy_path})."
        )
    if getattr(args, "internet_proxy", None):
        raise SystemExit(
            "--egress sidecar does not take --internet-proxy: the global sidecar's "
            f"parent proxy is [egress.upstream] url in {policy_path}. Set it there, "
            "then run `project-sandbox-egress restart`."
        )
    if getattr(args, "ca_cert", []):
        raise SystemExit(
            "--egress sidecar does not take --ca-cert: the upstream proxy's CA is "
            f"[egress.upstream] ca_cert in {policy_path}, installed into each "
            "sandbox at start (not baked into the image)."
        )
    if args.allow_github:
        raise SystemExit(
            "--egress sidecar blocks GitHub repository access, so --allow-github is "
            "not supported (per-repository access is on the roadmap, see "
            "ROADMAP.md). Use --egress firewall --allow-github for GitHub."
        )
    if getattr(args, "copilot_host", None) not in (None, copilot_auth.DEFAULT_HOST):
        raise SystemExit(
            "--egress sidecar does not support --copilot-host tenants (the Copilot "
            "profile covers github.com accounts only); use --egress firewall"
        )


def _egress_sidecar_policy(
    args, *, project: Path, workspace: Path, run_agent: str | None
) -> egress_policy.PolicyFile:
    """Load the global policy and check it and the sidecar state stay out of reach.

    ``egress.toml`` (whether or not it exists yet) and the sidecar state
    directory, which holds the CA key, must lie outside everything mounted into
    the sandbox: the project, the worktree, the repository metadata and every
    --mount source, checked after resolving symlinks.
    """
    protected = [project, workspace, project / ".git", project / ".jj"]
    try:
        for value in args.extra_mounts:
            protected.append(mount_options.bind_source(value))
    except ValueError as exc:
        raise SystemExit(f"Unsafe --mount for --egress sidecar: {exc}") from exc
    try:
        egress_policy.check_location(
            egress_policy.default_policy_path(), protected, what="egress policy"
        )
        egress_policy.check_location(
            session_ca_module.egress_root(),
            protected,
            what="egress sidecar state directory",
            tree=True,
        )
        policy = egress_policy.load_global()
    except egress_policy.EgressPolicyError as exc:
        raise SystemExit(str(exc)) from None
    if run_agent == "copilot" and not policy.copilot:
        raise SystemExit(
            "--agent copilot needs the sidecar's Copilot profile: set "
            "[egress.copilot] enabled = true in "
            f"{policy.path or egress_policy.default_policy_path()}, then run "
            "`project-sandbox-egress restart`."
        )
    return policy


def _ensure_egress_sidecar(
    runtime: container_cli.Runtime,
    policy: egress_policy.PolicyFile,
    *,
    run_agent: str,
    port: int,
    verbose: bool,
    now: Callable[[], datetime] | None = None,
    host_probe: Callable[[int, str], bool] | None = None,
    strategy: egress_sidecar.Strategy | None = None,
) -> egress_sidecar.Sidecar:
    """Start or reuse the global sidecar and check it can serve this session.

    ``port`` is the endpoint rendered into this sandbox's firewall and proxy
    variables: a sidecar publishing another port is an error, and the endpoint
    must answer from the host before the sandbox starts.
    """
    try:
        image = egress_sidecar.ensure_image(
            runtime,
            build=lambda **kw: container_cli.build_image(verbose=verbose, **kw),
            exists=container_cli.image_exists,
        )
        if egress_sidecar.read_state(egress_sidecar.layout(runtime)) is None:
            print("Starting the egress sidecar…")
        sidecar = egress_sidecar.ensure(
            runtime, policy, image=image, expected_port=port, strategy=strategy
        )
    except egress_sidecar.SidecarError as exc:
        raise SystemExit(f"[E] {exc}") from None
    for message in sidecar.messages:
        print(message)
    print(
        f"Egress sidecar: {'started' if sidecar.started else 'reusing'} "
        f"{egress_sidecar.CONTAINER} at {sidecar.endpoint}"
    )
    if not (host_probe or egress_sidecar.probe_published)(
        sidecar.port, sidecar.address
    ):
        raise SystemExit(
            f"[E] The egress sidecar does not answer on {sidecar.endpoint}; check "
            "`project-sandbox-egress status` and `project-sandbox-egress log`."
        )
    if run_agent == "copilot" and not sidecar.copilot:
        raise SystemExit(
            "--agent copilot needs the Copilot profile, but the running egress "
            "sidecar was started without it. Run `project-sandbox-egress restart` "
            "to apply [egress.copilot] enabled = true."
        )
    check = egress_sidecar.check_ca(sidecar, now=(now or (lambda: datetime.now(UTC)))())
    if check.error:
        raise SystemExit(f"[E] {check.error}")
    if check.warning:
        print(check.warning)
    return sidecar


def _egress_session_inputs(
    stack: contextlib.ExitStack,
    sidecar: egress_sidecar.Sidecar,
    *,
    session_id: str,
) -> str:
    """Write ``sessions/<sid>/sandbox/`` (removed when the stack closes)."""
    sessions = sidecar.layout.sessions
    session_ca_module.remove_stale(sessions)
    directory = stack.enter_context(
        session_ca_module.session_directory(session_id.lower(), root=sessions)
    )
    sandbox = egress_sidecar.write_sandbox_inputs(
        directory, ca=sidecar.ca, upstream_ca=sidecar.upstream_ca
    )
    return egress_sidecar.sandbox_mount(sandbox)


def _print_egress_denials(
    runtime: container_cli.Runtime,
    sidecar: egress_sidecar.Sidecar,
    *,
    started_at: float | None,
    runner=None,
) -> None:
    """Best effort: count the sidecar's denials during this session."""
    if started_at is None:
        return
    runner = runner or egress_sidecar.default_runner
    try:
        ended_at = egress_sidecar.sidecar_clock(runtime, runner)
        if ended_at is None:
            return
        text = egress_sidecar.fetch_logs(runtime, runner, since=started_at)
        count = egress_log.count_in_window(
            text,
            start=started_at,
            # `date +%s` truncates to whole seconds: include the final second.
            end=ended_at + 1,
            upstream=sidecar.upstream_url is not None,
        )
    except (OSError, egress_sidecar.SidecarError):
        return
    for line in egress_log.session_summary(count, since=started_at):
        print(line)


def _renders_devcontainer(args, *, is_chroot: bool) -> bool:
    """Devcontainers are firewall-only; sidecar sessions leave them untouched."""
    if is_chroot:
        # chroot renders no Dockerfile/firewall for a devcontainer to reference.
        return False
    if args.egress == EGRESS_SIDECAR:
        print(
            "[i] --egress sidecar: not rendering .devcontainer/ "
            "(devcontainers support firewall egress only)"
        )
        return False
    return True


def _validate_internet_proxy_args(args) -> internet_proxy.InternetProxy | None:
    value = getattr(args, "internet_proxy", None)
    if getattr(args, "ca_cert", []) and not value and not args.agent_proxy:
        raise SystemExit("--ca-cert requires --internet-proxy or --agent-proxy")
    if not value:
        return None
    config = internet_proxy.parse(value)
    if args.runtime == container_cli.CHROOT.name:
        raise SystemExit(
            "--internet-proxy does not support --runtime chroot because chroot "
            "sessions cannot enforce an isolated firewall; use a container runtime"
        )
    if args.no_firewall:
        raise SystemExit(
            "--internet-proxy cannot be combined with --no-firewall: proxy "
            "environment variables are bypassable without firewall enforcement"
        )
    if args.extra_domain or args.allow_github:
        # Sidecar mode rejects --internet-proxy earlier; there the proxy is the
        # global sidecar's [egress.upstream] parent behind local host policy.
        raise SystemExit(
            "--internet-proxy cannot be combined with --extra-domain or "
            "--allow-github in firewall egress mode; configure Internet "
            "destination policy in the external proxy, or use --egress sidecar "
            "with [egress.upstream] url and [egress] allow in egress.toml to "
            "enforce local host policy in front of it"
        )
    return config


@contextlib.contextmanager
def _build_proxy_session(
    args,
    endpoint,
    *,
    no_proxy: tuple[str, ...],
    runtime: container_cli.Runtime,
    ca_certificates: tuple[bytes, ...],
) -> Generator[dict[str, str] | None]:
    """Yield the build proxy variables for one image build.

    Apple container builders cannot route to a VPN-only proxy, but they can
    reach the host's loopback through the localhost alias, so for them a
    forwarder listens for exactly the duration of the build.
    """
    if endpoint is None:
        yield None
        return
    if runtime != container_cli.APPLE_CONTAINER:
        yield build_network.environment(args.build_proxy, no_proxy)
        return
    forwarder = build_proxy_forwarder.BuildProxyForwarder(
        endpoint, ca_certificates=ca_certificates
    )
    with forwarder:
        print(local_service_network.apple_setup_notice("Build proxy"))
        if args.verbose:
            print(
                "Build proxy forwarding: "
                f"{build_proxy_forwarder.LISTEN_HOST}:{forwarder.port} "
                f"(builder URL {forwarder.sandbox_url})"
            )
        yield build_network.environment(forwarder.sandbox_url, no_proxy)


def _local_services(
    config: internet_proxy.InternetProxy | None,
    *,
    proxy_port: int | None,
    proxy_loopback_host: str | None = None,
    pi_ollama_enabled: bool,
) -> list[local_service_network.LocalService]:
    services: list[local_service_network.LocalService] = []
    if config is not None and config.endpoint.local:
        services.append(config.service)
    if proxy_port is not None:
        services.append(
            local_service_network.LocalService(
                "Agent proxy",
                proxy_port,
                loopback_host=proxy_loopback_host or "127.0.0.1",
            )
        )
    if pi_ollama_enabled:
        services.append(
            local_service_network.LocalService("Ollama", local_service_network.PORT)
        )
    return services


def _local_firewall_destinations(
    config: internet_proxy.InternetProxy | None,
    *,
    proxy_port: int | None,
    pi_ollama_enabled: bool,
    proxy_url: str | None = None,
) -> list[firewall.TcpDestination]:
    destinations = []
    if config:
        destinations.append(
            firewall.TcpDestination.from_endpoint("Internet proxy", config.endpoint)
        )
    if proxy_url:
        destinations.append(
            firewall.TcpDestination.from_endpoint(
                "Agent proxy", agent_proxy.parse(proxy_url)
            )
        )
    elif proxy_port:
        destinations.append(
            firewall.TcpDestination(
                "Agent proxy", local_service_network.HOSTNAME, proxy_port
            )
        )
    if pi_ollama_enabled and config:
        destinations.append(
            firewall.TcpDestination(
                "Ollama", local_service_network.HOSTNAME, local_service_network.PORT
            )
        )
    return destinations


def _runtime_model(args, run_agent: str) -> str | None:
    """Return the provider-qualified model identifier passed to the agent."""
    model = getattr(args, "model", None)
    if not model:
        return None
    if getattr(args, "agent_proxy", None) and run_agent == "pi":
        return f"agent-proxy/{model.removeprefix('agent-proxy/')}"
    return model


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
    egress: egress_policy.PolicyFile | None = None,
) -> int:
    asset_project = (
        workspace if (args.python_uv or args.node_npm) and args.branch else project
    )
    context_dir = asset_project / ".project-sandbox"
    claude_cfg = context_dir / "claude" / "settings.json"
    codex_cfg = context_dir / "codex" / "config.toml"
    pi_cfg = context_dir / "pi" / "settings.json"
    # Only the keys _build_session_command consumes ("claude", and optionally
    # "codex"/"opencode"/"pi" when available); the devcontainer-specific dirs
    # are not used on the CLI run path.
    credential_dirs = {
        "claude": config_agents.credentials_dir(context_dir, "claude"),
        **{
            agent: config_agents.credentials_dir(
                context_dir, agent, unsupervised=bool(args.prompt or args.prompt_text)
            )
            for agent in ("codex", "opencode", "pi")
            if agent in available_agents
        },
    }
    run_agent = _requested_agent(args)
    copilot_host = getattr(args, "copilot_host", None)
    copilot_cfg = context_dir / "copilot" / "config.json"
    if _copilot_forwards_token(args, run_agent):
        credential_dirs["copilot"] = copilot_auth.staged_token_dir(context_dir)
    pi_ollama_enabled = _pi_ollama_enabled(args, run_agent)
    _warn_byok_provider_allowlist(args, run_agent)
    _warn_pi_ollama_no_firewall(args, pi_ollama_enabled)
    if args.runtime == container_cli.CHROOT.name:
        _validate_chroot_session(run_agent)

    print("DRY RUN: no files, worktrees, images, or containers will be created.")
    if run_agent == "copilot":
        assert copilot_host is not None
        _print_copilot_dry_run(args, context_dir=context_dir, host=copilot_host)
    if args.agent_proxy:
        endpoint = agent_proxy.parse(args.agent_proxy)
        print(
            f"Would use agent proxy: {endpoint.sandbox_url} ({'forwarded' if endpoint.local else 'direct'})"
        )
        if args.no_firewall:
            print(
                "Would use ordinary DNS without firewall pinning or egress enforcement."
            )
        else:
            print("Would defer sandbox DNS pinning and TLS verification.")
        print(f"Would select model: {args.model}")
        print("Would defer authenticated model discovery and proxy configuration.")
        if run_agent == "bash":
            print(
                "Would inject Bash proxy environment: OPENAI_BASE_URL, "
                "OPENAI_API_KEY, OPENAI_MODEL (values redacted)."
            )
    if worktree is not None:
        print(f"Would use worktree at: {workspace}")
        if getattr(args, "vcs_access", "full") == "hidden":
            print("Would hide workspace VCS metadata and omit external VCS stores")
        elif isinstance(worktree, jj_workspace_mod.JjWorkspace):
            source, target = jj_workspace_mod.repo_store_mount(project, workspace)
            print(f"Would mount .jj metadata: {source} -> {target}")
            git_mount = jj_workspace_mod.git_backend_mount(project, workspace)
            if git_mount is not None:
                print(f"Would mount jj git backend: {git_mount[0]} -> {git_mount[1]}")
        else:
            print(f"Would mount .git metadata: {(project / '.git').resolve()}")
    if args.build_proxy:
        print(
            "Would pass the build proxy as predefined proxy build-args, which are "
            "not written to Dockerfiles or images (values redacted)."
        )
        build_no_proxy = build_network.parse_no_proxy(args.build_no_proxy)
        if build_no_proxy:
            print("Would exclude from the build proxy: " + ",".join(build_no_proxy))
    for certificate in args.build_ca_cert:
        print(
            f"Would install build CA before downloads in sandbox and devcontainer images: {certificate}"
        )
    for certificate in args.ca_cert:
        print(
            f"Would bake CA certificate into sandbox and devcontainer images: {certificate}"
        )
    print(f"Would render sandbox assets under: {context_dir}")
    if egress is not None:
        print(
            "Would not render devcontainer (devcontainers support firewall egress only)"
        )
    else:
        print(f"Would render devcontainer under: {asset_project / '.devcontainer'}")
    preview_runtime = (
        container_cli.select_runtime(args.runtime, dry_run=True)
        if run_agent or args.build_only
        else None
    )
    if (
        args.build_proxy
        and preview_runtime == container_cli.APPLE_CONTAINER
        and not args.no_build
    ):
        port = build_proxy_forwarder.preferred_port(
            build_network.parse(args.build_proxy)
        )
        print(
            "Would forward the build proxy from "
            f"{build_proxy_forwarder.LISTEN_HOST}:{port} during the image build "
            f"only (builder URL {build_proxy_forwarder.sandbox_url(port)})."
        )
    if preview_runtime == container_cli.CHROOT:
        base_dockerfile, build_context = None, context_dir
    else:
        _, base_dockerfile, build_context = _resolve_build_source(
            args, project=asset_project, context_dir=context_dir, write_generated=False
        )
    if base_dockerfile is not None:
        if _synthesises_dockerfile(args):
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

    if run_agent is None and not args.build_only:
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
            preview_build_args=(
                build_network.redacted_build_args(build_network.environment(""))
                if args.build_proxy
                else ()
            ),
            dry_run=True,
            verbose=args.verbose,
        )
    if args.build_only:
        print(f"Would build image only: {args.image_tag}")
        return 0
    assert run_agent is not None
    proxy_port = (
        agent_proxy.validate_url(args.agent_proxy)[1] if args.agent_proxy else None
    )
    external_proxy_config = (
        internet_proxy.for_sidecar(egress.port)
        if egress is not None
        else _validate_internet_proxy_args(args)
    )
    proxy_config = internet_proxy.for_runtime(external_proxy_config, runtime)
    egress_mount = (
        _print_egress_dry_run(egress, runtime=runtime) if egress is not None else None
    )
    forwarding_plans = local_service_network.prepare_services(
        runtime,
        _local_services(
            proxy_config,
            proxy_port=(
                proxy_port
                if not args.agent_proxy or agent_proxy.parse(args.agent_proxy).local
                else None
            ),
            proxy_loopback_host=(
                agent_proxy.loopback_host(args.agent_proxy)
                if args.agent_proxy
                else None
            ),
            pi_ollama_enabled=pi_ollama_enabled,
        ),
        dry_run=True,
    )
    if forwarding_plans:
        if runtime == container_cli.APPLE_CONTAINER:
            print(local_service_network.apple_setup_notice("Local service"))
        for plan in forwarding_plans:
            print(f"Would use {local_service_network.describe(plan)}")
    preview_api_key_values = (
        {
            "OPENAI_BASE_URL": "[REDACTED]",
            "OPENAI_API_KEY": "[REDACTED]",
            "OPENAI_MODEL": "[REDACTED]",
        }
        if args.agent_proxy and run_agent == "bash"
        else _preview_api_key_env_values(args)
    )
    if args.api_key_env_file:
        print(
            "Would inject API key variables from dotenv file(s) without reading "
            "them during dry-run: " + ", ".join(args.api_key_env_file)
        )
    if proxy_config is not None:
        endpoint = proxy_config.endpoint
        print(
            f"Would use Internet proxy: {endpoint.sandbox_url} ({'forwarded' if endpoint.local else 'direct'})"
        )
        if proxy_config is not external_proxy_config:
            print(
                "Would relay the Internet proxy from "
                f"{build_proxy_forwarder.LISTEN_HOST}:{endpoint.port} to "
                f"{proxy_config.original_url} for the session."
            )
        print(
            "Would defer DNS, TCP/TLS preflight; firewall admits only each endpoint address/port."
        )
        proxy_env = internet_proxy.environment(
            proxy_config,
            bypass_local_services=pi_ollama_enabled,
            bypass_hosts=(agent_proxy.parse(args.agent_proxy).sandbox_host,)
            if args.agent_proxy
            else (),
        )
        preview_api_key_values = internet_proxy.merge_environment(
            preview_api_key_values,
            proxy_config,
            bypass_local_services=pi_ollama_enabled,
            bypass_hosts=(agent_proxy.parse(args.agent_proxy).sandbox_host,)
            if args.agent_proxy
            else (),
        )
        print(
            "Would inject proxy environment: "
            + ", ".join(f"{k}={v}" for k, v in proxy_env.items())
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
        pi_cfg=pi_cfg,
        copilot_cfg=copilot_cfg,
        copilot_host=copilot_host,
        runtime=runtime,
        create_prompt_files=False,
        api_key_values=preview_api_key_values,
        ollama_add_hosts=[p.add_host for p in forwarding_plans if p.add_host],
        network=local_service_network.network_argument(forwarding_plans),
        egress_mount=egress_mount,
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


class _ReadOnlyRunner:
    """The dry-run's runner: only ``inspect`` argv reach the runtime."""

    ALLOWED = (("inspect",),)

    def __call__(self, argv):
        args = tuple(argv[1:])
        if not any(args[: len(prefix)] == prefix for prefix in self.ALLOWED):
            raise AssertionError(f"dry-run must not run {shlex.join(argv)}")
        return egress_sidecar.default_runner(argv)


def _print_egress_dry_run(
    policy: egress_policy.PolicyFile, *, runtime: container_cli.Runtime
) -> str:
    """Describe the sidecar plan; generates no key and creates no resource.

    When the runtime is reachable, a read-only ``inspect`` tells whether the
    sidecar would be started or reused and what differs from egress.toml.
    Returns the session-input mount the sandbox would get.
    """
    effective = egress_policy.effective(policy)
    image = sidecar_image.image_tag()
    lay = egress_sidecar.layout(runtime)
    endpoint = egress_sidecar.endpoint(policy.port)
    print("Egress: sidecar (global Squid sidecar per runtime; hostname policy)")
    exists = "" if policy.path else " (missing: built-in defaults)"
    print(
        f"  Policy file: {policy.path or egress_policy.default_policy_path()}{exists}"
    )
    providers = set(egress_policy.PROVIDER_HOSTS)
    own = [h for h in effective.allow if h not in providers]
    print("  Allowed hosts: " + (", ".join(own) or "none"))
    print("  Agent provider hosts (always allowed): " + ", ".join(sorted(providers)))
    print(f"  Copilot profile: {'on' if policy.copilot else 'off'}")
    print(
        "  GitHub repository access: blocked"
        + (
            " (api.github.com only for Copilot's /copilot_internal/)"
            if policy.copilot
            else ""
        )
    )
    constraints = session_ca_module.name_constraints_for(effective.bump_set)
    if effective.bump_set:
        print("  TLS interception: " + ", ".join(effective.bump_set))
        print(
            "  Sidecar CA: EC P-256, name constraints "
            + ", ".join(constraints)
            + f", {policy.ca_lifetime_days}-day lifetime; certificate trusted in "
            "the sandbox, key stays in the sidecar"
        )
    else:
        print("  TLS interception: none (everything spliced)")
        print("  Sidecar CA: constrained to 'invalid', not mounted into the sandbox")
    if policy.upstream_url is not None:
        via = (
            "the sidecar-owned host relay"
            if egress_sidecar.needs_relay(runtime, policy.upstream_url)
            else "a direct connection from the sidecar"
        )
        if (
            runtime == container_cli.DOCKER
            and egress_sidecar.loopback_parent_port(policy.upstream_url) is not None
        ):
            via += (
                " (on Docker Engine: the sidecar-owned parent forwarder on the "
                "default bridge gateway)"
            )
        print(f"  Parent proxy: {policy.upstream_url} via {via}")
    print(
        f"  Published endpoint: {endpoint} (host loopback only"
        + (
            "; on Docker Engine, the default bridge gateway instead)"
            if runtime == container_cli.DOCKER
            else ")"
        )
    )
    print(f"  Resources: {policy.memory} memory, {policy.cpus} CPU(s)")
    print(f"  Sidecar image: {image}")
    report = None
    if shutil.which(runtime.binary) is not None:
        try:
            report = egress_sidecar.status(
                runtime,
                policy,
                image=image,
                deps=egress_sidecar.Collaborators(runner=_ReadOnlyRunner()),
                probe=False,
            )
        except (OSError, egress_sidecar.SidecarError):
            report = None
    if report is None:
        print(f"  Sidecar: {runtime.name} not reachable; a launch would start it")
    elif report["would"] == "reuse" or (report["running"] and "endpoint" in report):
        print(
            f"  Sidecar: would reuse the running {egress_sidecar.CONTAINER} at "
            f"{report['endpoint']}"
        )
        drift = report["drift"]
        if drift.get("reload"):
            print("  Would apply egress.toml changes live (reload, no restart)")
        if report["endpoint"] != endpoint:
            print(
                "  [W] The running sidecar publishes another port; the launch would "
                "fail until `project-sandbox-egress restart`."
            )
        if drift.get("restart"):
            print(
                "  [W] Restart-only differences (not applied by a launch): "
                + ", ".join(drift["restart"])
            )
    else:
        print(f"  Sidecar: would start {egress_sidecar.CONTAINER}")
        print(
            "Would generate or reuse the sidecar CA and config in: " + str(lay.sidecar)
        )
        print(
            "Would run: "
            + shlex.join(
                egress_sidecar.sidecar_run_argv(
                    runtime,
                    image=image,
                    sidecar_dir=lay.sidecar,
                    port=policy.port,
                    memory=policy.memory,
                    cpus=policy.cpus,
                    restart_fingerprint="<fingerprint>",
                )
            )
        )
    session_dir = (
        lay.sessions
        / observability.new_session_id().lower()
        / egress_sidecar.SANDBOX_SUBDIR
    )
    print(f"Would write this session's sandbox inputs to: {session_dir}")
    return egress_sidecar.sandbox_mount(session_dir)


def _print_copilot_dry_run(args, *, context_dir: Path, host: str) -> None:
    """Describe the Copilot token plan without invoking pass or gh."""
    print(f"Would run GitHub Copilot CLI for host: {host}")
    if getattr(args, "no_forward_credentials", False):
        print(
            "Would not resolve or mount a Copilot token (--no-forward-credentials); "
            "log in inside the sandbox."
        )
    else:
        entry = _copilot_pass_entry(args, host)
        if getattr(args, "copilot_token_pass", None):
            print(f"Would resolve the Copilot token from: pass show {entry}")
        else:
            print(
                f"Would resolve the Copilot token from: pass show {entry}, "
                f"falling back to gh auth token --hostname {host}"
            )
        token_dir = copilot_auth.staged_token_dir(context_dir)
        print(
            f"Would stage the Copilot token at {token_dir / copilot_auth.TOKEN_FILENAME} "
            f"(0600) and mount it read-only at {copilot_auth.SECRETS_TARGET}"
        )
    print(
        "Would set Copilot environment: " + ", ".join(copilot_auth.container_env(host))
    )
    if args.no_firewall:
        return
    if args.egress == EGRESS_SIDECAR:
        print(
            "Would use the sidecar Copilot profile ([egress.copilot] enabled in "
            "egress.toml); GitHub repository access stays blocked"
        )
        return
    print("Would allow the --allow-github host set (implied by --agent copilot)")
    tenant = copilot_auth.tenant_hosts(host)
    if tenant:
        print("Would DNS-pin Copilot tenant hosts: " + ", ".join(tenant))


def _validate_chroot_session(run_agent: str | None) -> None:
    if run_agent != "bash":
        raise SystemExit("--runtime chroot requires --agent bash")


def _synthesises_dockerfile(args) -> bool:
    return any(
        getattr(args, flag, False) for flag in ("python_uv", "rust_cargo", "node_npm")
    )


def _reject_image_only_flags_for_chroot(args) -> None:
    if getattr(args, "node_npm", False):
        raise SystemExit(
            "--node-npm requires an image-based runtime "
            "(apple-container, docker, or podman), not --runtime chroot"
        )
    if getattr(args, "shm_size", None) is not None:
        raise SystemExit(
            "--shm-size requires an image-based runtime "
            "(apple-container, docker, or podman), not --runtime chroot"
        )


def _validate_shm_size(value: str | None) -> None:
    if value is not None and _SHM_SIZE_RE.fullmatch(value) is None:
        raise SystemExit(
            f"Invalid --shm-size {value!r}: expected a positive number with a "
            "b, k, m, or g suffix, e.g. 2g"
        )


def _effective_shm_size(args) -> str | None:
    if getattr(args, "shm_size", None) is not None:
        return args.shm_size
    return NODE_NPM_DEFAULT_SHM_SIZE if getattr(args, "node_npm", False) else None


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


def _preview_api_key_env_values(args) -> dict[str, str]:
    """Return only explicitly named variables without reading secret values."""
    values: dict[str, str] = {}
    for env_file in getattr(args, "api_key_env_file", []):
        # Preserve dry-run input validation while deliberately never opening
        # the dotenv file or parsing its secret contents.
        _resolve_required_path(env_file, what="--api-key-env-file")
    for name in getattr(args, "api_key_env", []):
        _validate_env_name(name, source="--api-key-env")
        values[name] = "[REDACTED]"
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
    node_npm = getattr(args, "node_npm", False)
    if node_npm and python_uv:
        raise SystemExit("--node-npm and --python-uv are mutually exclusive")
    if node_npm and rust_cargo:
        raise SystemExit("--node-npm and --rust-cargo are mutually exclusive")

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

    if node_npm:
        if args.dockerfile:
            raise SystemExit("--node-npm and --dockerfile are mutually exclusive")
        if args.base_image:
            raise SystemExit("--node-npm and base_image are mutually exclusive")

        if not (project / "package.json").is_file():
            raise SystemExit(
                f"--node-npm requires package.json at the project root: {project}"
            )
        lockfiles = [
            name
            for name in ("npm-shrinkwrap.json", "package-lock.json")
            if (project / name).is_file()
        ]
        if not lockfiles:
            raise SystemExit(
                "--node-npm requires a committed npm lockfile "
                "(package-lock.json or npm-shrinkwrap.json) at the project root; "
                "run `npm install` on the host and commit the lockfile first"
            )

        workspace_members = _detect_npm_workspaces(project, project / lockfiles[0])
        generated = context_dir / "Dockerfile.node-npm"
        if write_generated:
            generated = dockerfile.render_node_npm_dockerfile(
                context_dir,
                lockfiles=lockfiles,
                workspace_members=workspace_members,
                has_playwright=_lockfile_has_playwright(project / lockfiles[0]),
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
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
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


def _detect_npm_workspaces(project: Path, lockfile: Path) -> list[str]:
    """Use npm's locked local package paths rather than reimplementing its globs.

    Workspace-capable lockfiles contain both local package metadata and links
    to those packages. Preserve lexical paths for COPY and npm's relative
    links, while checking resolved paths stay inside the build context.
    """

    def fail(reason: str) -> NoReturn:
        raise SystemExit(
            f"--node-npm: {reason}; run `npm install` on the host and commit "
            "the updated manifests and lockfile"
        )

    try:
        data = json.loads((project / "package.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"cannot read package.json: {exc}")
    if not isinstance(data, dict):
        fail("package.json must contain an object")

    def patterns(declaration) -> list[str]:
        if isinstance(declaration, dict):
            declaration = declaration.get("packages")
        if not isinstance(declaration, list) or not all(
            isinstance(pattern, str) for pattern in declaration
        ):
            fail("workspaces must be an array of strings or a packages array")
        return declaration

    workspaces = patterns(data.get("workspaces", []))
    if not workspaces:
        return []

    try:
        locked = json.loads(lockfile.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"cannot read {lockfile.name}: {exc}")
    if not isinstance(locked, dict) or locked.get("lockfileVersion") not in (2, 3):
        fail("npm workspaces require a version 2 or 3 lockfile")
    packages = locked.get("packages")
    if not isinstance(packages, dict) or not isinstance(packages.get(""), dict):
        fail(f"{lockfile.name} is missing root package metadata")
    if patterns(packages[""].get("workspaces", [])) != workspaces:
        fail(f"{lockfile.name} has stale root workspace declarations")

    root = project.resolve()

    def local_path(value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or "node_modules" in path.parts:
            fail(f"invalid local package path {value!r} in {lockfile.name}")
        try:
            (project / path / "package.json").resolve().relative_to(root)
        except (OSError, RuntimeError, ValueError):
            fail(f"local package path {value!r} escapes the project")
        if path == Path(".") or not (project / path / "package.json").is_file():
            fail(f"missing local package manifest {value!r}/package.json")
        return path.as_posix()

    members: set[str] = set()
    for key, package in packages.items():
        if not isinstance(package, dict):
            fail(f"invalid package metadata for {key!r} in {lockfile.name}")
        if key and "node_modules" not in Path(key).parts:
            members.add(local_path(key))
        if package.get("link"):
            target = package.get("resolved")
            if not isinstance(target, str) or target not in packages:
                fail(f"missing local package metadata for link {key!r}")
            members.add(local_path(target))
    return sorted(members)


_PLAYWRIGHT_PACKAGES = ("playwright", "@playwright/test")


def _lockfile_has_playwright(lockfile: Path) -> bool:
    """Return True when the lockfile installs Playwright at the root node_modules.

    Only top-level installs count: a Playwright nested under another package's
    node_modules does not expose the ``playwright`` CLI the image build runs.
    """
    try:
        data = json.loads(lockfile.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    packages = data.get("packages")
    if isinstance(packages, dict):
        return any(f"node_modules/{name}" in packages for name in _PLAYWRIGHT_PACKAGES)
    # lockfileVersion 1 has no "packages" map; its top-level "dependencies"
    # keys are the root node_modules entries.
    dependencies = data.get("dependencies")
    return isinstance(dependencies, dict) and any(
        name in dependencies for name in _PLAYWRIGHT_PACKAGES
    )


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
        log_parent = Path(args.log).expanduser().absolute().parent
        generated_files.validate_dir(log_parent)
        if not log_parent.is_dir():
            raise SystemExit(f"--log parent directory does not exist: {log_parent}")
    # The summary is written from main()'s finally block, where a failure can
    # only be warned about — so reject an unusable destination while the run can
    # still be aborted cleanly.
    if getattr(args, "json_summary", None) and args.json_summary != "-":
        summary_path = Path(args.json_summary).expanduser().resolve()
        if summary_path.is_dir():
            raise SystemExit(f"--json-summary must not be a directory: {summary_path}")
        if not summary_path.parent.is_dir() and summary_path.parent.exists():
            raise SystemExit(
                f"--json-summary parent is not a directory: {summary_path.parent}"
            )


def _allow_github(args, run_agent: str | None) -> bool:
    """GitHub network access is an explicit choice: the flag or the agent.

    Prompt text never widens the firewall. Proxy mode is gateway-only unless
    --allow-github is given (and --agent copilot rejects --agent-proxy).
    """
    if getattr(args, "agent_proxy", None):
        return bool(args.allow_github)
    return bool(args.allow_github or run_agent == "copilot")


def _warn_byok_provider_allowlist(args, run_agent: str | None) -> None:
    if (
        run_agent not in ("opencode", "pi")
        or args.no_firewall
        or args.agent_proxy
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
    # The port-scoped allow rule, runtime-selected host pin, and connectivity
    # probe live in the firewall script; with --no-firewall none of them runs.
    if not pi_ollama_enabled or not args.no_firewall:
        return
    print(
        "[W] --pi-ollama with --no-firewall: the host pin and gateway route "
        "are only set up as part of firewall initialization. "
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


def _wait_for_worktree_metadata(wt, runtime) -> float:
    """Give a freshly created git worktree time to become visible in the VM.

    Container runtimes on macOS share the host filesystem through a metadata
    cache that can hide a just-recreated ``.git/worktrees`` directory for about
    a second; a container started in that window sees no gitdir and every git
    command in it fails (see worktree.METADATA_CACHE_WINDOW). Linux containers
    bind-mount the host filesystem directly and chroot does not mount at all,
    so neither needs the wait. Returns the seconds waited.
    """
    if not isinstance(wt, worktree_mod.Worktree):
        return 0.0
    if not runtime.is_container or sys.platform != "darwin":
        return 0.0
    return worktree_mod.wait_for_metadata_visibility(wt)


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
    return f"{name} — {datetime.now().astimezone():%Y-%m-%dT%H:%M}"


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


def _vcs_layout_for_session(
    project: Path, workspace: Path, worktree, *, preview: bool
) -> vcs_access.Layout:
    if not preview or workspace.exists():
        return vcs_access.inspect(workspace)
    # A dry-run worktree does not exist yet. Describe its expected metadata
    # without creating mount sources or invoking either VCS.
    workspace = workspace.resolve()
    if isinstance(worktree, jj_workspace_mod.JjWorkspace):
        root = workspace / ".jj"
        planned = [container_cli.MountSpec(root, "/workspace/.jj", True)]
        source, target = jj_workspace_mod.repo_store_mount(project, workspace)
        planned.append(container_cli.MountSpec(source, target, True))
        backend = jj_workspace_mod.git_backend_mount(project, workspace)
        if backend is not None:
            planned.append(container_cli.MountSpec(*backend, True))
        return vcs_access.Layout(workspace, (root,), tuple(planned))
    root = workspace / ".git"
    common = (project / ".git").resolve()
    return vcs_access.Layout(
        workspace,
        (root,),
        (
            container_cli.MountSpec(root, "/workspace/.git", True),
            container_cli.MountSpec(common, str(common), True),
        ),
        frozenset((root,)),
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
    copilot_cfg: Path | None = None,
    copilot_host: str | None = None,
    runtime: container_cli.Runtime,
    create_prompt_files: bool,
    api_key_values: dict[str, str] | None = None,
    ollama_add_host: str | None = None,
    ollama_add_hosts: list[str] | None = None,
    network: str | None = None,
    session_id: str | None = None,
    egress_mount: str | None = None,
    vcs_layout: vcs_access.Layout | None = None,
    vcs_mask_dir: Path | None = None,
) -> tuple[list[str], Path | None, bool, list[str] | None]:
    try:
        implicit_mounts.validate_sources(
            project,
            workspace=workspace,
            extra_mounts=args.extra_mounts,
            sources=[*credential_dirs.values(), claude_cfg.parent, codex_cfg.parent],
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(
            f"Invalid --mount for --runtime {runtime.name}: {exc}"
        ) from exc
    # Agent availability is validated up front in main() via _ensure_agent_available.
    extra_mounts = list(args.extra_mounts)
    if egress_mount is not None:
        # --egress sidecar: this session's public CA certificates, read-only.
        extra_mounts.append(egress_mount)
    if getattr(args, "agent_proxy", None) and run_agent in ("opencode", "bash"):
        extra_mounts.append(
            f"type=bind,source={(context_dir / 'opencode').resolve(strict=False)},"
            "target=/project-sandbox-config/opencode,readonly"
        )
    vcs_mode = getattr(args, "vcs_access", "full")
    if vcs_mode != "full":
        try:
            if vcs_layout is None:
                vcs_layout = _vcs_layout_for_session(
                    project, workspace, worktree, preview=not create_prompt_files
                )
            vcs_access.validate(vcs_layout, vcs_mode, runtime, list(args.extra_mounts))
            if vcs_mode == "hidden" and vcs_mask_dir is None:
                if create_prompt_files:
                    raise ValueError(
                        "Hidden VCS access requires session-owned mask sources"
                    )
                vcs_mask_dir = Path("/tmp/project-sandbox-vcs-preview")
            extra_mounts.extend(
                container_cli._mount_arg(m)
                for m in vcs_access.mounts(vcs_layout, vcs_mode, vcs_mask_dir)
            )
        except (ValueError, OSError) as exc:
            raise SystemExit(str(exc)) from exc
        if not create_prompt_files:
            print(
                f"Would use VCS access: {vcs_mode}; host capture after exit for --branch only"
            )
    elif worktree is not None:
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
    if vcs_mode == "read-only":
        extra_env.append("GIT_OPTIONAL_LOCKS=0")
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
    # Model/effort/fast apply in both interactive and headless runs; the
    # entrypoint branch for each agent turns these into the agent's own
    # --model/--effort/fast-mode flags (and ignores them for bash).
    runtime_model = _runtime_model(args, run_agent)
    if runtime_model:
        extra_env.append(f"PROJECT_SANDBOX_MODEL={runtime_model}")
    if getattr(args, "effort", None):
        extra_env.append(f"PROJECT_SANDBOX_EFFORT={args.effort}")
    if getattr(args, "fast", False) and run_agent in FAST_MODE_AGENTS:
        extra_env.append("PROJECT_SANDBOX_FAST=1")
    if run_agent == "pi" and getattr(args, "pi_tools", None):
        extra_env.append(f"PROJECT_SANDBOX_PI_TOOLS={args.pi_tools}")
    run_mode_agent = run_agent
    unsupervised = bool(args.prompt or args.prompt_text)
    log_path: Path | None = None
    # Every container is named from the reported session id. Besides making
    # timeout cleanup reliable, this lets a restarted orchestrator correlate a
    # persistent session record with `container ls --format json`.
    container_name: str | None = None
    if runtime.is_container:
        container_name = observability.container_name(
            session_id or observability.new_session_id()
        )
    container_stop_argv = (
        container_cli.build_stop_argv(runtime, container_name)
        if container_name is not None and runtime.is_container
        else None
    )

    if unsupervised:
        log_path = (
            Path(args.log).expanduser().absolute()
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
                # apple/container can only bind-mount *directories* (see
                # HISTORY_SHELL_TARGET in paths.py), so the staging dir itself is
                # mounted below rather than the prompt file. Clear it first so a
                # stale prompt file left by an earlier unsupervised run in this
                # project is never exposed alongside the current one.
                generated_files.remove(prompt_staging, recursive=True)
                ensure_dir(prompt_staging)
                generated_files.write_bytes(
                    prompt_file, source_prompt.read_bytes(), mode=0o600
                )
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
                # Same apple/container constraint and stale-prompt concern as
                # above: mount the directory, but clear it first.
                generated_files.remove(prompts_dir, recursive=True)
                ensure_dir(prompts_dir)
                generated_files.write_text(prompt_file, args.prompt_text, mode=0o600)
            else:
                print(f"Would write prompt to: {prompt_file}")
            extra_mounts.append(
                f"type=bind,source={prompts_dir.resolve()},"
                f"target={PROMPT_MOUNT_TARGET},readonly"
            )
            extra_env.append(
                f"PROJECT_SANDBOX_PROMPT_FILE={PROMPT_MOUNT_TARGET}/prompt.txt"
            )

    implicit_mounts.validate_sources(
        project,
        workspace=workspace,
        extra_mounts=extra_mounts,
    )
    if not unsupervised:
        shell_dir, claude_projects = ensure_history_paths(
            project, create=create_prompt_files
        )
        extra_mounts.append(
            f"type=bind,source={shell_dir},target={HISTORY_SHELL_TARGET}"
        )
        extra_mounts.append(
            f"type=bind,source={claude_projects},target={HISTORY_CLAUDE_PROJECTS_TARGET}"
        )
        extra_env.append(f"HISTFILE={HISTORY_HISTFILE}")

    workspace_mask = ensure_workspace_sandbox_mask(
        project,
        create=create_prompt_files,
        token=session_id
        or (observability.new_session_id() if create_prompt_files else "preview"),
    )
    mask_source = workspace_mask
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

    forward_credentials = not getattr(
        args, "no_forward_credentials", False
    ) and not getattr(args, "agent_proxy", None)
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
            copilot_credentials_dir=runtime_credential_dirs.get("copilot"),
            copilot_cfg=copilot_cfg if run_agent == "copilot" else None,
            copilot_host=copilot_host or copilot_auth.DEFAULT_HOST,
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
            add_hosts=(
                ollama_add_hosts or ([ollama_add_host] if ollama_add_host else [])
            ),
            network=network,
            shm_size=_effective_shm_size(args),
            node_modules_tmpfs=getattr(args, "node_npm", False),
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
    if run_agent == "copilot":
        print(f"  host:   {getattr(args, 'copilot_host', None) or 'github.com'}")
    if run_agent in FAST_MODE_AGENTS:
        print(f"  fast:   {'on' if getattr(args, 'fast', False) else 'off'}")


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


def _validate_generated_dirs(project: Path) -> None:
    """Preflight managed directories before the first generated-file mutation.

    Actual operations still reopen components without following links: this
    check improves failure ordering but is not the security boundary.
    """
    context = project / ".project-sandbox"
    config_agents.validate_project_dirs(context)
    for path in (
        project / ".devcontainer",
        context / "sessions",
        context / "prompt",
        context / "prompts",
    ):
        generated_files.validate_dir(path)


def _update_project_gitignore(project: Path) -> None:
    """Idempotently append credential-secret ignore entries to project .gitignore."""
    marker = "# project-sandbox — do not commit agent secrets"
    lines_to_add = [
        marker,
        ".project-sandbox/",
        ".devcontainer/",
    ]
    gi = project / ".gitignore"
    try:
        existing = generated_files.read_text(gi)
    except FileNotFoundError:
        existing = ""
    existing_lines = set(existing.splitlines())
    # Append only entries that are not already present anywhere in the file, so a
    # project that already ignores .project-sandbox/ (without our marker) does not
    # get a duplicate line. The marker is itself an entry, so it is added only once.
    missing = [line for line in lines_to_add if line not in existing_lines]
    if not missing:
        return
    sep = "\n" if existing and not existing.endswith("\n") else ""
    generated_files.write_text(gi, existing + sep + "\n".join(missing) + "\n")


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
!copilot/
!copilot/config.json
!init-firewall.sh
!init-firewall-devcontainer.sh
!Dockerfile
!Dockerfile.devcontainer
!entrypoint.sh
!project-sandbox-devcontainer-init
"""
    generated_files.write_text(context_dir / ".gitignore", content)
