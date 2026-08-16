## Context

Credential discovery and staging are currently host-wide: `sync_credentials()` copies every detected agent's credential material into project-specific private directories under `/tmp`. The CLI then passes the complete directory map to direct container or chroot mount construction, which mounts every non-null credential directory. Provisioning copies every mounted credential into the shared `agent` user's home.

This makes every named-agent run a multi-agent credential environment. It also conflicts with the Pi requirement that ties its credential mount to Pi selection. At the same time, one CLI invocation generates a devcontainer intended to support all installed agents, and `--agent bash` intentionally provides a general-purpose multi-agent shell. Staging all detected credentials can therefore remain useful; the security boundary must be credential reachability at runtime.

Credentials are already staged outside the repository and build context. The generated `.project-sandbox` directory is masked inside direct runs and devcontainers, while historical in-project credential files are removed during synchronization. These protections should remain explicit invariants and gain regression coverage.

## Goals / Non-Goals

**Goals:**

- Give each direct named-agent run access only to its own credential family.
- Preserve and document all-detected-agent forwarding for bash and devcontainers.
- Enforce selection at both the CLI boundary and runtime mount builder.
- Cover interactive and headless named-agent container runs while preserving the chroot runtime's bash-only, multi-agent behavior.
- Prevent alternate project-sandbox-managed paths from exposing staged credentials.

**Non-Goals:**

- Preventing a user from explicitly exposing credentials through an arbitrary `--mount`.
- Separating Unix users or home directories within a bash session or devcontainer.
- Changing provider firewall allowlists, CLI installation selection, credential formats, or OAuth refresh behavior.
- Adding a flag to make bash or devcontainers single-agent.

## Decisions

### Define an execution-mode credential allowlist

A small policy helper will map the effective runtime agent to the credential names it may receive. `claude-headless`, `codex-headless`, `opencode-headless`, and `pi-headless` normalize to their base agent; a named agent allows only itself; `bash` and `bash-headless` allow all staged credential agents.

This keeps policy independent of host discovery and avoids spreading conditional checks across call sites. An alternative was to filter only inside `sync_credentials()`, but that would prevent the same invocation from rendering an intentionally multi-agent devcontainer and would make stale staged directories the de facto security boundary.

### Filter at the caller and enforce again in mount construction

The CLI will pass only policy-authorized credential paths to a direct run. `build_mount_specs()` will also receive the effective agent (or an explicit allowed set) and reject/filter unrelated credential inputs before emitting mounts. The second check makes the security-sensitive primitive safe when called directly by tests, chroot, or future entry points.

Provisioning remains presence-driven: it copies files only from mounted secret paths. No agent-specific provisioning branch needs to decide authorization independently.

An alternative was caller-only filtering. That has a smaller diff but allows a future or existing caller to silently restore broad access by passing the complete staged map.

### Keep devcontainer selection separate and explicitly multi-agent

Devcontainer rendering will continue using the complete detected credential map. It will not reuse named-agent filtering because the devcontainer is a persistent general development environment rather than the direct `--agent` process selected for the current invocation.

The security and runtime documentation will state that any process in a bash session or devcontainer can read all forwarded agent credentials. `--no-forward-credentials` remains the mechanism for creating an unauthenticated environment.

### Treat staging location and generated config as defense-in-depth boundaries

Credential material remains in mode-0700 project-specific directories under `/tmp`; no credential file may be written beneath `.project-sandbox`. Existing stale-cleanup logic remains in place. Tests will verify that generated config directories contain no legacy `.credentials.json`, `.claude.json`, or `auth.json`, that the workspace masks are ordered after custom mounts, and that Dockerfile rendering copies only generated scripts/config—not staged secret sources.

Staging all detected credentials is accepted because staging is host-side and private, while runtime reachability is allowlisted. Purging all staged credentials with `--no-forward-credentials` remains unchanged.

## Risks / Trade-offs

- **[Risk] A new agent is added without updating credential policy normalization.** → Centralize supported credential names and fail closed for unknown named modes.
- **[Risk] Filtering only command argv lets another direct-container caller remain broad.** → Put the authoritative check in shared mount construction; verify the chroot runtime remains intentionally multi-agent because it supports bash sessions only.
- **[Risk] Documentation leads users to assume bash/devcontainer isolation.** → Add explicit warnings in security and runtime credential-forwarding sections, including the `--no-forward-credentials` alternative.
- **[Trade-off] All credentials remain staged for named-agent invocations.** → Keep staging outside project/build paths and rely on two-layer runtime mount filtering so devcontainers can remain multi-agent.
- **[Trade-off] Explicit custom mounts can bypass managed isolation.** → Document this as user-authorized behavior; blocking arbitrary host paths is outside this change's threat model.

## Migration Plan

1. Add the centralized policy and regression tests around it.
2. Apply caller-side filtering and shared mount-builder enforcement.
3. Update existing broad-mount tests to distinguish named-agent, bash, and devcontainer expectations.
4. Add staging/config/image reachability regressions and documentation.
5. Run compile checks and the full test suite. Rollback consists of reverting the policy/filtering change; credential formats and staged data layouts do not migrate.

## Open Questions

None. Bash and devcontainer multi-agent behavior, named-agent isolation, and explicit custom-mount scope are decided.
