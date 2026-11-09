# Design

## Context

- **Today's `--branch` flow** (`jj_workspace.py`): `jj workspace add --no-colocate` creates a workspace next to the project. The container mounts it at `/workspace`, plus the main `.jj/repo` store and, when colocated, the main `.git`, at targets read from pointer files. At teardown, host `jj -R <workspace>` snapshots and sets the bookmark (`jj_workspace.py:271`).
- **Without `--branch`**, the project is mounted in place.
- **Git-only projects** use `worktree.py`, which mounts the main `.git` read-write and runs host Git in the agent's worktree.
- **Apple `container`** rejects single-file binds and binds onto missing targets. Nested directory binds work (the `.project-sandbox` mask is one).
- **Spike, 2026-11-09** (jj 0.46.0, host only, mounts simulated by swapping directories; script: `spike-isolated-snapshot.sh` in this change). All checks passed:
  - a host workspace still reports no changes after its `.jj` was moved aside, a second repository was initialized over the same files, and the `.jj` was moved back;
  - with only the private `.jj` and `.git` in place, `jj` sees one base commit and a clean tree, and `jj commit` and `git status` work;
  - after restoring the host `.jj`, one `jj status` records modified, added and deleted files as one commit in the main repository;
  - the agent's commit message is absent from the main repository;
  - a symlink to `/etc/passwd` is stored with mode 120000;
  - an empty `.git` placeholder directory is not recorded.

## Goals / Non-Goals

**Goals:**
- In isolated mode, no host command reads version-control metadata an agent could write, and no agent can write metadata a host command reads.
- One simple rule per mode for what is mounted.

**Non-Goals:**
- Keeping the agent's commit series from isolated sessions (user decision).
- Isolated mode for Git-only projects. It is feasible with a Git worktree as the host side, but adds a second backend.
- The protections inside shared mode (`protect-host-executed-project-config`).
- Moving session directories into private state (`private-session-state`).

## Decisions

### 1. Two sets of metadata over one directory

```
host disk                                   container
─────────                                   ─────────
W/                 jj workspace of main  →  /workspace          (bind, rw)
W/.jj              host-owned               hidden by ↓
W/.git/            empty placeholder        hidden by ↓
P/jj               agent-private         →  /workspace/.jj      (bind, rw)
P/git              agent-private         →  /workspace/.git     (bind, rw)
```

- `W` is created by `jj workspace add --no-colocate --name <ws> -r <start>` from the main repository.
- `P` is a per-bookmark directory beside `W`'s parent, outside `W` and outside the project.
- The two nested binds hide `W/.jj` and `W/.git`. A bind mount point can't be renamed or removed from inside the container, so the underlying host `.jj` stays out of reach.
- The main repository's `.jj/repo` and `.git` are not mounted at all. `repo_store_mount` and `git_backend_mount` are deleted.

**Alternatives considered:**
- *A real clone with a bundle or fetch on the way back.* It keeps the agent's commit series, but the host must then read a repository the agent wrote, or depend on the container producing a bundle before it dies. It also costs a clone per session. Rejected with the user's decision that one snapshot is enough.
- *Keep the shared store and verify pointers* (the earlier `jj-only-branch-workspaces` draft). It leaves the main `.git` writable by every agent. Superseded.

### 2. Building the private repository at a trusted time

Setup runs on the host before any agent exists, under the existing `_workspace_lock`:

1. `jj workspace add` creates `W` (host `.jj` inside).
2. Move `W/.jj` to a temporary name beside `P`.
3. In `W`: `jj git init --colocate`, then `jj commit -m "session base"`. This runs with the host's identity and the `host-command-execution` overrides.
4. Move `W/.jj` to `P/jj` and `W/.git` to `P/git`.
5. Move the host `.jj` back to `W/.jj`, and create the empty `W/.git`.

A failure at any step removes `W` and `P` and forgets the workspace, as today's failed-setup path does. Step 3 reads only files the host just checked out.

The private repository is colocated because agents commonly call `git`. It has no remote.

**Alternative considered:** initialize the private repository inside the container at first start. That needs the entrypoint to distinguish first start from resume, and it runs before the agent but as the agent user. Doing it on the host keeps the entrypoint unchanged.

### 3. The host snapshot

Finalization keeps today's sequence, now on host-owned metadata:

1. `jj -R W status` snapshots the working copy.
2. If `@` is empty, the target is `@-`; otherwise `@`, described with the session message.
3. `jj bookmark set <bookmark> -r <target>`.
4. Unless the session failed or `--keep-workspace` was given: `jj workspace forget`, then remove `W` and `P`.

- The agent can never have run `jj` against the host metadata, so `@` is always the single working-copy commit the host created.
- jj ignores `.jj` and `.git` directories in a working copy, so the placeholder isn't recorded.
- The agent's `.gitignore` edits decide which new files are recorded. That is the agent's own result, and matches today.
- jj refuses new files above `snapshot.max-new-file-size`. The finalizer reports refused paths in the session output and keeps the session directory in that case.
- Host `jj` calls keep the `--config` overrides from `host-command-execution`. Config-id records are not needed: the identifiers live in the host `.jj`.

### 4. Mode selection and bookmarks

| Project | `--vcs` absent | `--vcs isolated` | `--vcs shared` |
|---|---|---|---|
| has `.jj` | isolated | isolated | in place |
| `.git` only | error with both hints | error with the jj hint | in place |
| neither | in place | error | error |

- `--branch NAME` implies isolated mode and names the bookmark.
- Without `--branch`, the bookmark is `sandbox/<session-id>`. The CLI prints it at the start and the end, and it is in the JSON summary's existing `bookmark` field.
- The start revision keeps today's rule: an existing bookmark; otherwise the main working-copy commit when it has changes; otherwise its parent.
- Errors are raised in the planning step, so `--dry-run` reports them too.

### 5. Leases

- **Isolated:** `host_locks.path_lock("workspace-lease", W)`, non-blocking, from setup to the end of finalization. Acquisition also refuses while a session record for `W` is running and its container is alive.
- **Shared:** `host_locks.path_lock("shared-vcs", <project>)`, non-blocking, for the session's lifetime, with the same orphaned-container check.

### 6. Resume

A kept `W` keeps `P`. A new session for the bookmark finds both and mounts them again. If `W` exists without `P`, or `P` without `W`, the launch fails with the paths and the cleanup commands, as today's "not registered" case does.

Between sessions, the host has already recorded the kept state. On resume the agent's private repository still shows its own earlier commits, which is harmless.

## Risks / Trade-offs

- [The spike simulated the mounts] → Task 1.1 repeats it with real nested binds on Docker, Podman and Apple `container`, and tries to reach the host `.jj` from inside. If a runtime can't hide it, isolated mode is refused on that runtime.
- [Users lose in-place editing by default] → **BREAKING**, stated in the release notes. The end-of-session output names the bookmark and the command to bring the result into the checkout. `--vcs shared` restores the old behavior.
- [Agent commit messages are lost] → Decided. The session message describes the snapshot. Agents that structure work as commits still benefit inside the session.
- [Uncommitted work in the main checkout is the starting point] → As today. The docs say so, and dry-run shows the start revision.
- [Double metadata costs time for large trees] → Setup snapshots the tree once more than today (step 3). Measure on a large project in task 1.2; the base commit could later be built without a second scan.
- [A large generated directory that isn't ignored gets recorded] → As today for `--branch`. The size refusal covers single files only.

## Migration Plan

- jj workspaces kept from older versions have no private repository. The launch refuses them with the bookmark-and-forget instructions already used for colocated leftovers.
- Git worktrees made by older versions are left untouched.
- To roll back, revert the change. Session directories made by this version are ordinary jj workspaces, and older versions can forget them.
