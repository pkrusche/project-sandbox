# Design

## Context

- `mount_options.parse`/`bind_source` implement Docker's CSV grammar and fail
  closed on unclear forms; `validate_target` checks only absolute/no `..`.
- `egress_policy.check_location(path, protected, tree=True)` already checks
  both directions and walks symlinks on the way; `cli._egress_sidecar_policy`
  calls it in sidecar mode only.
- The project-sandbox state root is `$XDG_STATE_HOME/project-sandbox`
  (history and mask storage, session inputs, records, locks).

## Goals / Non-Goals

**Goals:** one source/target check run for every user mount in every egress
mode, for sessions and devcontainers.

**Non-Goals:** the branch's tagged mount model and refusing encoder (this line
keeps CSV quoting with round-trip tests), volume warnings in firewall mode,
implicit mounts (built by project-sandbox from host-known paths).

## Decisions

### 1. One check, called before any rendering

`mount_validation.check(spec, *, project, workspace, egress_layout, home)`
runs for every user mount in `cli` (sessions) and `devcontainer.render`. It
reuses `check_location(..., tree=True)` for the protected roots (policy
directory, egress state, state root) and for the credential list, so a
source containing, equal to or inside a root is refused, with symlinks on the
way resolved.

### 2. Credential list

`~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.config/gh`, `~/.password-store`, plus the
host config directories `config_agents` already knows per agent (`~/.claude`,
`~/.codex`, `~/.config/opencode`, `~/.pi`, Copilot's). Kept in one constant,
also used by `derive-vcs-mounts-from-host`.

### 3. Targets

`validate_target` gains the protected prefixes and the `/workspace`-itself
rule. Mask ordering is unchanged (masks are appended last today); a test pins
it.

### 4. Workspace containment

For a source whose lexical path is under the project or workspace, the
resolved path must also be under it; otherwise refuse. The runtime receives
the resolved path, as `parse_mount` already does.

## Risks / Trade-offs

- [Users mounting `~/.ssh` for git-over-SSH inside the sandbox] → Refused by
  design; the docs explain HTTPS tokens or agent forwarding alternatives are
  out of scope, and that credentials must not reach agents.
- [Legitimate targets under `/opt` or `/srv`] → Allowed; only the listed
  prefixes are refused.
