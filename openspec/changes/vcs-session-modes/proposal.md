# Proposal

## Why

Every session today gives the agent write access to version-control metadata that the host later reads or executes:

- **Sessions without `--branch`** mount the project in place, with `.git` and `.jj` writable.
- **`--branch` sessions** mount a jj workspace plus the main repository's `.jj/repo` store and, in colocated repositories, its `.git`. The mount sources and targets come from pointer files the agent can write: a pointer naming `/usr/local/bin`, or a `git_target` naming any host directory, becomes a mount in the next session.

The agent can therefore plant Git hooks and config, or redirect jj pointers. Earlier drafts contained this with pointer verification, config-id records, read-only sub-mounts and a snapshot-and-restore of `.git/config`. Those are many mechanisms around one cause: the agent shares the user's repository metadata.

Most sessions don't need that. An unattended agent needs the files, a way to see its own changes, and a way to hand the result back. Only work *on history itself*, such as rewriting a commit series together with the user, needs the real repository.

A spike on jj 0.46 (2026-11-09, host side only) showed that a session directory can carry two independent sets of metadata, and that the host can record the agent's changes without reading anything the agent's VCS tools wrote.

## What Changes

- **Two VCS modes, chosen by the user with `--vcs`.**
  - `isolated` (default): the agent works in a separate session directory, with a private throwaway repository. The host records the result as one commit when the session ends.
  - `shared`: the project is mounted in place with its real `.git` and `.jj` writable, as sessions without `--branch` are today. It is an explicit opt-in.
- **Isolated mode.**
  - The host creates a jj workspace of the main repository as the session directory. That workspace's `.jj` is never visible in the container.
  - Before launch, the host creates a private repository over the same files, holding one "session base" commit. Its `.jj` and `.git` are mounted over `/workspace/.jj` and `/workspace/.git`. The agent can use `jj` and `git` freely there.
  - The main repository's `.jj/repo` and `.git` are not mounted.
  - When the session ends, the host snapshots the session directory with its own metadata. Everything the agent changed becomes one commit in the main repository, and the session's bookmark points at it. This also works when the container was killed.
  - The agent's own commits and messages are discarded with its private repository (user decision, 2026-11-09).
  - `--branch NAME` names the bookmark. Without it, a bookmark is generated from the session ID.
- **Shared mode.**
  - Only one shared session may run per repository at a time. Isolated sessions may run beside it.
  - The launch prints that the agent can change repository metadata the host executes later.
  - The protections for that metadata (read-only hooks, config restore, a live watcher) belong to `protect-host-executed-project-config`, which is narrowed to this mode.
- **BREAKING:**
  - A launch without `--branch` in a jj project no longer edits the checkout in place. Its result arrives as a commit on a generated bookmark. `--vcs shared` gives the old behavior.
  - Isolated mode requires a jj repository. In a Git-only project, a launch without `--vcs shared` fails, naming `jj git init --colocate` and `--vcs shared`. The Git worktree backend is removed.
  - Commit series made by an agent in an isolated session are not kept.
  - A directory with no repository at all is mounted in place as before, and `--vcs` and `--branch` are rejected for it.

## Capabilities

### New Capabilities

- `vcs-session-modes`: the VCS modes a session can run in, what each mounts, how an isolated session's result reaches the main repository, and the limits on shared sessions.

### Modified Capabilities

(none. No existing spec covers `--branch` or repository mounts. The `egress-sidecar` policy-location requirement mentions "the worktree" and "the main repository's `.git`" generically, and still holds.)

## Impact

- **Code:**
  - `jj_workspace.py`: session-directory setup with the private repository, the host snapshot, removal. `repo_store_mount` and `git_backend_mount` are deleted.
  - `cli.py`: `--vcs`, the default mode per project type, generated bookmarks, mounts, the shared-session lease, finalization.
  - `container_cli.py`: the two metadata mounts.
  - Removed: `worktree.py`, `tests/test_worktree.py`, `scripts/e2e-git-workflow.sh` and the Git branches in `cli.py`.
- **Other proposals:**
  - `harden-host-command-execution` (the reduced `jj-only-branch-workspaces` draft): supplies tool resolution, the neutral working directory and the jj overrides used by this change's host commands. Its former `branch-workspaces` capability is replaced by this change.
  - `protect-host-executed-project-config`: protects repository metadata in shared mode only, relying on this change's one-shared-session lease, and holds the live watcher.
  - `validate-all-mount-sources`: the two metadata mounts are implicit mounts like any other.
  - `private-session-state`: session directories and agent-private repositories move into private state when it lands.
  - ROADMAP: the "separate clones" item is resolved by this change.
- **project-sandbox-tui:** `--branch`, `workspace_path`, `bookmark` and `jj_change_id` keep their meaning. Host `jj` in the session directory uses host-owned metadata, so the TUI can show live progress safely. The TUI needs a way to select shared mode, and a release note about commit series. It is updated after this change is settled.
- **Docs:** `docs/usage.md`, `docs/security.md`, `README.md`, `CLAUDE.md` (the `--branch` note).
