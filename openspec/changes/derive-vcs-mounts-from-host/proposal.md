# Proposal

## Why

Sessions in the existing `--vcs-access` modes build VCS mounts from pointer
files an agent can write:

- jj `--branch`: `jj_workspace.repo_store_mount` takes the container **target**
  of the read-write `.jj/repo` store from the workspace's `.jj/repo` file, which
  lives under `/workspace`. An absolute value such as `/usr/local/bin` would
  mount the agent-writable store over the entrypoint and firewall initializer,
  which run as root before the firewall exists.
- jj `--branch`: `git_backend_mount` takes an absolute **host source** from the
  store's `git_target`, which the session can write. Any host directory (for
  example `~/.ssh`, or the egress state holding the sidecar CA key) would be
  bind-mounted read-write into the next session.
- `read-only`/`hidden`: `vcs_access.inspect` resolves `.git` gitfiles,
  `commondir`, `.jj/repo` and `git_target` to sources anywhere on the host and
  mounts them read-only.

A planted pointer takes effect in the next session on the same repository.
Both functions were confirmed on a scratch layout to return the planted values
unchanged. The `sec-fixing-claude` branch removed these mounts with its
isolated mode; the existing modes still need them, so they must stop trusting
the pointer contents.

## What Changes

- `--branch` sessions (Git and jj, every mode) compute every metadata mount's
  source and target from paths the host created or knows (the main repository,
  the workspace path). Pointer files are compared with the expected value and
  never parsed for paths; a mismatch refuses the launch with recovery steps.
- `git_target` is accepted only when it resolves to the main repository's own
  `.git`.
- In-place sessions with `read-only`/`hidden` accept a pointer-derived source
  only when it is a Git or jj metadata directory and not a protected host
  location; targets must lie under `/workspace` or mirror the source's host
  path, and never under a container system path.

## Capabilities

### New Capabilities
- `vcs-metadata-mounts`: how sessions in the `full`, `read-only` and `hidden`
  modes derive the Git and jj metadata mounts they add, and which pointer-file
  contents they refuse.

### Modified Capabilities

None.

## Impact

- Code: `jj_workspace.py` (`repo_store_mount`, `git_backend_mount`),
  `worktree.py` (expected gitfile), `vcs_access.py` (`inspect`), `cli.py`
  (mount assembly and refusal messages).
- Behaviour: a repository whose pointer files were edited (by an agent or by
  hand) is refused instead of mounted; `jj workspace update-stale` or
  re-creating the workspace fixes it, as the message says.
- Interplay: `isolated-vcs-access` doesn't use these mounts at all;
  `validate-all-mount-sources` supplies the protected-location list.
- Docs: `docs/vcs-access.md`, `docs/security.md`.
