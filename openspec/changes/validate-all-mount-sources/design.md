# Design

## Context

- `mount_options.parse`/`bind_source` implement Docker's CSV grammar and fail
  closed on unclear forms; `validate_target` checks only absolute/no `..`.
- `egress_policy.check_location(path, protected, tree=True)` already checks
  both directions and walks symlinks on the way; `cli._egress_sidecar_policy`
  calls it in sidecar mode only.
- The project-sandbox state root is `$XDG_STATE_HOME/project-sandbox`
  (history and mask storage, session inputs, records, Colima VCS masks
  `vcs-masks/`, Colima profile records `colima-profiles/` and, since main's
  `move-host-locks-to-state-root`, the host locks `locks/`).
- `implicit_mounts.validate_sources`, called for sessions and devcontainers,
  already refuses in every mode a project, workspace, build context or
  `--mount` source that overlaps session inputs, the mount storage root or the
  locks root (`mount_storage.check_exposure`, both directions). What is still
  sidecar-only is the egress policy and egress state check.
- `colima_profile.CREDENTIAL_DIRS` (`~/.ssh`, `~/.aws`, `~/.gnupg`,
  `~/.config/gcloud`, `~/.docker`) refuses `colima init` workspace roots that
  are or contain a credential location.

## Goals / Non-Goals

**Goals:** one source/target check run for every user mount in every egress
mode, for sessions and devcontainers.

**Non-Goals:** the branch's tagged mount model and refusing encoder (this line
keeps CSV quoting with round-trip tests), volume warnings in firewall mode,
implicit mounts (built by project-sandbox from host-known paths).

## Decisions

### 1. One check, called before any rendering

`mount_validation.check(spec, *, project, workspace, egress_layout, home)`
runs for every user mount in `cli` (sessions) and `devcontainer.render`,
through `implicit_mounts.validate_sources`, which both already call; the
existing session-inputs, mount-storage and locks checks move into it, so there
is one check. It reuses `check_location(..., tree=True)` for the protected roots (policy
directory, egress state, state root) and for the credential list, so a
source containing, equal to or inside a root is refused, with symlinks on the
way resolved.

### 2. Credential list

`~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.config/gh`, `~/.password-store`,
`~/.config/gcloud`, `~/.docker`, `~/.colima` and `~/.lima`, plus the host
config directories `config_agents` already knows per agent (`~/.claude`,
`~/.codex`, `~/.config/opencode`, `~/.pi`, Copilot's). `~/.colima` and
`~/.lima` hold the Colima VM's SSH keys, configuration and Docker socket, so a
mount of them gives a sandbox control of the VM that runs every sandbox of the
profile. Kept in one constant, also used by `derive-vcs-mounts-from-host`,
and built as a superset of `colima_profile.CREDENTIAL_DIRS`, which stays as it
is: main decided on 2026-11-16 not to extend the `colima init` workspace-root
deny list (it documents roots to avoid instead). That decision concerns VM
exports; `--mount` sources are refused per the 2026-11-09 decision.

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
