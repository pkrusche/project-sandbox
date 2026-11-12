# Proposal

## Why

In every current `--vcs-access` mode the container mounts the main repository's
real `.git`/`.jj` metadata, and the host later runs `git`/`jj` against
metadata an agent could write. `full` (the default) lets an agent plant hooks,
`core.fsmonitor` or jj config that host capture executes, and pointer files the
next session turns into mounts. `read-only`/`hidden` protect existing metadata
but still mount it and derive mounts from it. The `sec-fixing-claude` branch
built and e2e-tested an isolated model, in which the agent only ever sees a
private throwaway repository. That model was jj-only and the default; this
change brings it over as an additional, opt-in mode that also supports Git.

## What Changes

- New value `--vcs-access isolated`. `full` stays the default; `read-only` and
  `hidden` are unchanged.
- An isolated session runs in a session directory (a Git worktree or a jj
  workspace of the main repository) mounted at `/workspace`. A private
  repository, built on the host before the agent starts, is bind-mounted over
  `/workspace/.git` (and `/workspace/.jj` for jj projects). The main
  repository's `.git`/`.jj` are not mounted at all.
- Git projects: the session worktree's `.git` pointer file is moved out of the
  session directory for the session and the worktree is locked against
  pruning; both are undone before the host captures the result.
- At the end, the host records the session directory's files as one commit on
  the session's branch (Git) or bookmark (jj), reading only host-owned
  metadata. Host commands skip hooks and fsmonitor, and Git commits pass
  `--no-verify`. The agent's own commit series and messages are not kept.
- Without `--branch`, an isolated session gets a generated branch/bookmark
  `sandbox/<session-id>`; with `--branch NAME` it uses `NAME` and continues it
  if it exists.
- Automatic capture is refused, and the session directory kept with
  instructions, when the agent left nested repositories, or when the session's
  metadata layout is not the one the host created.
- Kept or failed isolated sessions can be resumed by the same `--branch`.
- `--dry-run` shows the mode, the branch/bookmark and the session directory.
- Not included: sharing ignored folders into the session (`--share`) and
  changing the default mode.

## Capabilities

### New Capabilities
- `isolated-vcs-sessions`: the opt-in isolated VCS mode, covering selection,
  the session's mounts and private repository, host capture of the result,
  branch/bookmark naming, leases, and resume, for Git and jj projects.

### Modified Capabilities

None. The existing `--vcs-access` modes have no spec, and their behavior does
not change.

## Impact

- Code: `cli.py` (option, planning, mount assembly, dry-run, finalize
  dispatch), `worktree.py` and `jj_workspace.py` (isolated setup and capture,
  through `host_tools` from `harden-host-command-execution`, which lands
  first), `vcs_access.py` (`MODES`),
  entrypoint provisioning (`safe.directory /workspace` on Apple `container`).
- Tests: unit tests for setup/capture on fixture repositories, argv tests for
  mounts, CLI tests for selection and dry-run, and a new e2e suite
  `scripts/e2e-isolated-vcs.sh` in `scripts/run-e2e-tests.sh`.
- Docs: `docs/vcs-access.md`, `docs/security.md`, `docs/usage.md`, README
  option list, CHANGELOG.
- On disk: session directories keep the existing `<repo>-worktrees/` and jj
  workspace locations; private repositories live in
  `<parent>/.session-vcs/<name>.<12 hex>`, which is never removed as a whole.
- project-sandbox-tui: unaffected unless it opts in; it can offer the mode later.
