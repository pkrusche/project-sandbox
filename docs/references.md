# References and Related Projects

## Similar Projects

- [agentbox (fletchgqc)](https://github.com/fletchgqc/agentbox/tree/main) -
  ephemeral per-project Docker containers for Claude/OpenCode/Gemini.
- [Claude Code devcontainer](https://github.com/anthropics/claude-code/tree/main/.devcontainer) -
  example for `init-firewall.sh` and devcontainer layout.
- [Jarek Hartman: Codex in the jail](https://jhartman.pl/posts/macos/2026-02-02-codex-in-the-jail/) -
  why `sandbox-exec` falls short and apple/container fills the gap.

## Similar Projects Using apple/container

- [instavm/coderunner](https://github.com/instavm/coderunner) -
  MCP-server-based sandbox for Claude Code, Codex, Gemini, OpenCode, Kiro.
- [banksean/sand](https://github.com/banksean/sand) - per-project disposable
  microVMs with APFS CoW workspace cloning.
- [emarc/claude-contained](https://github.com/emarc/claude-contained) - minimal
  wrapper image for Claude Code/Codex/Gemini/Vibe in apple/container or Docker.

## Docker/Incus-Based Alternatives with Egress Filtering

- [mattolson/agent-sandbox](https://github.com/mattolson/agent-sandbox) -
  per-project Docker containers with iptables + mitmproxy allowlist and
  proxy-injected credentials.
- [pvillega/sandbox-claude](https://github.com/pvillega/sandbox-claude) -
  per-project Incus containers on OrbStack with domain-filtered egress.
- [mensfeld/code-on-incus](https://github.com/mensfeld/code-on-incus) -
  multi-slot Incus sandboxes with real-time network threat detection.
- [trailofbits/claude-code-devcontainer](https://github.com/trailofbits/claude-code-devcontainer) -
  hardened devcontainer for Claude Code with immutable firewall and IPC-socket
  mitigations.

## Worktree-Per-Agent Pattern

- [dagger/container-use](https://github.com/dagger/container-use) - MCP server
  that gives agents isolated Docker environments backed by Git branches and
  worktrees.

## Container-Free / Kernel-Enforced Sandboxing

- [anthropic-experimental/sandbox-runtime](https://github.com/anthropic-experimental/sandbox-runtime) -
  OS-level filesystem and network restrictions without a container
  (Bubblewrap/seccomp on Linux, Seatbelt on macOS).
- [Use-Tusk/fence](https://github.com/Use-Tusk/fence) - Go tool for
  container-free agent sandboxing, inspired by sandbox-runtime.
- [GreyhavenHQ/greywall](https://github.com/GreyhavenHQ/greywall) -
  deny-by-default kernel-syscall sandbox with built-in profiles for coding
  agents.
