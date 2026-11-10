# Design

## Context

- **Today's `--branch` flow** (`jj_workspace.py`): `jj workspace add --no-colocate` creates a workspace next to the project. The container mounts it at `/workspace`, plus the main `.jj/repo` store and, when colocated, the main `.git`, at targets read from pointer files. At teardown, host `jj -R <workspace>` snapshots and sets the bookmark (`jj_workspace.py:271`).
- **Without `--branch`**, the project is mounted in place.
- **Git-only projects** use `worktree.py`, which mounts the main `.git` read-write and runs host Git in the agent's worktree.
- **Apple `container`** 1.7.1 rejects single-file binds given with `--mount` ("is not a directory"), which is the syntax `container_cli` uses, and binds onto missing targets. `-v` accepts a file, but it shares the file's parent directory and silently drops a directory mount of that same parent. Nested directory binds work (the `.project-sandbox` mask is one), in any argument order.
- **Spike, 2026-11-09** (jj 0.46.0, host only, mounts simulated by swapping directories; script: `spike-isolated-snapshot.sh` in this change). All checks passed:
  - a host workspace still reports no changes after its `.jj` was moved aside, a second repository was initialized over the same files, and the `.jj` was moved back;
  - with only the private `.jj` and `.git` in place, `jj` sees one base commit and a clean tree, and `jj commit` and `git status` work;
  - after restoring the host `.jj`, one `jj status` records modified, added and deleted files as one commit in the main repository;
  - the agent's commit message is absent from the main repository;
  - a symlink to `/etc/passwd` is stored with mode 120000;
  - an empty `.git` placeholder directory is not recorded.
- **Real-mount spike, 2026-11-09** (task 1.1; host jj 0.46.0, container jj 0.47.1; Docker Desktop 29.9.3, Podman 6.0.0 machine (applehv, rootless, `--userns=keep-id`), Apple `container` 1.7.1). Layout as in Decision 1: `W` at `/workspace`, the private `jj` and `git` directories over `/workspace/.jj` and `/workspace/.git`, with a marker file in the host `W/.jj`. Each runtime ran twice: once as root with `NET_ADMIN`/`NET_RAW` and the nested mounts listed before the parent, once as the agent user (uid 501) with the parent first. The results were the same on all three runtimes:
  - the private `.jj` and `.git` are visible and the host marker is not, in both argument orders (all three runtimes sort the mounts by target);
  - `mv` of either mount point away, `mv` of a directory over `.jj`, and `rmdir`/`rm -rf` of the mount points fail with `EBUSY`. `rm -rf` empties the private `.jj`, which harms only the agent's own repository;
  - `umount`, `umount -l` and `mount --bind /workspace /mnt` fail, also as root, because no runtime grants `CAP_SYS_ADMIN`;
  - `find /` finds neither the host marker nor any `.jj/repo` pointer file, and `/proc/1/root/workspace/.jj` shows the private copy;
  - `/proc/self/mountinfo` names the host source paths on Docker and Podman (Apple shows share IDs). This discloses paths, not contents;
  - `jj st`, `jj commit` and `git status` work in the container on the repository the host jj created;
  - afterwards the host `W/.jj` is byte-identical, the `W/.git` placeholder is still empty, and the host snapshot records the agent's modified and added files with neither the agent's commit message nor any `.git`/`.jj` path.

  No runtime exposes the host `.jj`, so no runtime is refused. Not covered: native Linux hosts (Docker and Podman ran in their macOS VMs) and rootful Podman.

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
- `P` is `<W's parent>/.session-vcs/<W's name>.<12 hex>`: outside `W` and outside the project. Each new private repository gets a fresh name, and neither `<repo>-workspaces/` nor `.session-vcs/` is ever removed. Docker Desktop on macOS could not bind a private directory that had been mounted, removed and recreated at the same path, even 5 s later, nor anything under a recreated `.session-vcs/`; a name it has never seen always works (e2e, 2026-11-10). A kept session's `P` is found by that name pattern.
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

### 7. Sharing ignored folders

`--share PATH[:rw]` takes a project-relative path. A trailing `:rw` or `:ro` selects the mode; the default is read-only.

Checks, before anything is created:
- `PATH` is relative, has no `..`, and `<project>/PATH` is an existing directory that resolves inside the project. Single files are refused, because Apple `container` can't bind them; the message suggests sharing the parent directory.
- `PATH` isn't, and isn't inside or around, `.git`, `.jj`, `.devcontainer`, `.project-sandbox` or a path watched by `protect-host-executed-project-config`.
- After `jj workspace add`, `W/PATH` must not exist. If it does, the checkout contains tracked files there, and a mount would hide the agent's edits from the snapshot. The launch fails and removes the new workspace. jj tracks new files automatically, so a path absent from the checkout is in practice an ignored one.

Setup then:
- creates the empty directory `W/PATH` as the bind target. jj doesn't record empty directories, so the snapshot is unaffected;
- adds `/PATH/` to the private repository's `.git/info/exclude`, so the agent's `jj` and `git` don't try to track the folder when it is ignored in the main repository only through `info/exclude` or a global ignore file;
- adds a bind of `<project>/PATH` at `/workspace/PATH`, after the workspace and metadata mounts and before the protections, through the encoder of `validate-all-mount-sources`.

In shared mode and in directories without a repository, the project is mounted in place, so `--share` is accepted and has no effect.

**Alternative considered:** telling users to pass `--mount`. It works, but leaves the placeholder, the exclude entry and the tracked-path check to the user.

A private per-session copy (`--copy`) is deferred to ROADMAP.

## Risks / Trade-offs

- [The host-side spike simulated the mounts] → Task 1.1 repeated it with real nested binds on Docker, Podman and Apple `container` (Context): every runtime hides the host `.jj`. A runtime that granted `CAP_SYS_ADMIN` could unmount the binds, so the argv tests of task 3.2 also assert that no session adds it.
- [The agent leaves a symlink in `W` that points at `.jj` or elsewhere on the host] → The snapshot stores it as a link (host spike). Host code that later walks `W` must not follow links, which `private-session-state`'s no-follow helpers already require.
- [Users lose in-place editing by default] → **BREAKING**, stated in the release notes. The end-of-session output names the bookmark and the command to bring the result into the checkout. `--vcs shared` restores the old behavior.
- [Agent commit messages are lost] → Decided. The session message describes the snapshot. Agents that structure work as commits still benefit inside the session.
- [Uncommitted work in the main checkout is the starting point] → As today. The docs say so, and dry-run shows the start revision.
- [Double metadata costs time for large trees] → Setup snapshots the tree once more than today (step 3). Not measured (user decision, 2026-11-09): the first users have small repositories. When large ones come up, measure, and build the base commit without a second scan if needed.
- [`--share …:rw` lets an agent change files outside the reviewed result] → Read-only is the default, `:rw` is per path, the launch lists every writable share, and the docs say these writes are not in the commit.
- [A shared folder contains a secret, such as `.env` files] → Sharing is always explicit and per path. Nothing is shared by default or by pattern.
- [A large generated directory that isn't ignored gets recorded] → As today for `--branch`. The size refusal covers single files only.

- [Apple `container` presents the `/workspace` mount root as root-owned, and git refuses the private repository as "dubious ownership"] → The entrypoint's identity provisioning adds `safe.directory /workspace` (found by the e2e test, 2026-11-10).
- [Freshly created mount sources can be invisible to a VM-backed runtime for about a second] → On macOS container runtimes the CLI waits up to 1.5 s after setup created directories, measured from their creation.

## Migration Plan

- jj workspaces kept from older versions have no private repository. The launch refuses them with the bookmark-and-forget instructions already used for colocated leftovers.
- Git worktrees made by older versions are left untouched.
- To roll back, revert the change. Session directories made by this version are ordinary jj workspaces, and older versions can forget them.
