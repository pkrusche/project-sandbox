# Design

## Context

- **`--branch` today.** Git projects use `worktree.py`: `git worktree add` creates
  `<repo>-worktrees/<branch>`, the main `.git` is bind-mounted read-write at its
  absolute host path, and `finalize` runs `git add -A` / `git commit` on the
  host. jj projects use `jj_workspace.py`: `jj workspace add --no-colocate`
  creates the workspace, and `repo_store_mount` / `git_backend_mount` mount the
  main `.jj/repo` store and Git backend at targets read from pointer files in
  the workspace and store. Both pointer files are writable by the agent.
- **`--vcs-access read-only|hidden`** (`vcs_access.py`) re-mount the same
  metadata read-only or mask it. They inspect the layout but still derive
  mounts from pointer files.
- **Leases.** `WorkspaceLease` already holds an exclusive per-branch and
  per-directory lock from setup through finalize, for both backends.
- **Prior work.** The `sec-fixing-claude` branch implemented isolated jj
  sessions (`vcs-session-modes`, archived 2026-11-10) and verified the mount
  shape with real binds on Docker Desktop 29.9, rootless Podman 6.0 (machine)
  and Apple `container` 1.7.1:
  - the session directory is mounted at `/workspace`, with private `jj` and `git`
    directories bound over `/workspace/.jj` and `/workspace/.git`;
  - from inside, the mount points cannot be renamed, removed or unmounted
    (`EBUSY`, and no `CAP_SYS_ADMIN`), `find /` reaches no host metadata, and
    `jj`/`git` work on the private repository;
  - Docker Desktop cannot bind a directory that was mounted, removed and
    recreated at the same path, even seconds later, so private repositories
    need fresh names, and their parent directory is never removed;
  - Apple shows the `/workspace` mount root as root-owned, so Git needs
    `safe.directory /workspace`.

  Native Linux hosts and rootful Podman were not covered by that spike.
- **Runtime limits.** No runtime binds a directory onto a file, and Apple
  `container` can't bind files at all. A Git worktree's `.git` is a file.

## Goals / Non-Goals

**Goals:**
- One mount shape for both backends; nothing mounted is derived from a file the
  agent could have written.
- Host setup and capture read only metadata the host created, and never run
  hooks or filesystem monitors.
- Keep the existing modes and the default untouched.

**Non-Goals:**
- Keeping the agent's commit series. The host never reads a repository the
  agent wrote.
- `--share` for ignored folders (session directories lack them, as `--branch`
  worktrees do today; `--mount` remains available).
- Fixing the other modes' pointer-derived mounts
  (`derive-vcs-mounts-from-host`). Host-command hardening for every mode is
  `harden-host-command-execution`, which this change builds on.
- Isolated generated devcontainers.

## Decisions

### 1. A fourth `--vcs-access` value, not a new flag

`isolated` joins `full`, `read-only` and `hidden`, so one option says how much
version-control access a session has. Isolated mode always uses a session
directory: with `--branch NAME` it is that branch's worktree or workspace;
without `--branch` it creates one for `sandbox/<session-id>`.
`--branch-start-at`, `--worktree-dir` and `--keep-workspace` keep their
meaning. The chroot runtime is refused for now (it has no bind-mount runtime to
hide the host metadata).

*Alternative:* the branch's separate `--vcs isolated|shared` flag. Rejected:
it would overlap `--vcs-access`, whose modes this line already ships.

### 2. Mount shape (both backends)

```
host                                         container
W/                  session directory     →  /workspace           (rw)
W/.git, W/.jj       host-owned, hidden by ↓
P/git               private, agent-owned  →  /workspace/.git      (rw)
P/jj                private (jj backend)  →  /workspace/.jj       (rw)
```

- `P` is `<W's parent>/.session-vcs/<W's name>.<12 hex>`, created `0700`.
  `P` itself is never mounted; only its `git`/`jj` subdirectories are.
- No other VCS mount is added. `repo_store_mount`, `git_backend_mount` and the
  main-`.git` bind are not used in this mode.
- On macOS runtimes the existing metadata-visibility wait applies to the newly
  created `P` directories.

### 3. Git backend setup (host, before any agent exists)

Under the existing repository lock and lease:

1. `git worktree add` as today (new or existing branch, same start rules).
2. `git worktree lock --reason "project-sandbox isolated session"`, so a
   `git worktree prune` can't drop the worktree while its pointer is away.
3. Move the pointer file `W/.git` to `P/host-git`.
4. In `W`: `git init`, `git add -A`, `git commit -m "session base"` with the
   host identity, the hardened invocation (Decision 5) and no template hooks.
   `W`'s files at this point are exactly what the host just checked out.
5. Move `W/.git` (now the private repository) to `P/git`, and create an empty
   `W/.git` directory as the bind target.

Any failure removes `P`, unlocks and removes the new worktree, and deletes a
branch the setup created, as the current failed-setup path does.

*Alternative:* the branch's draft of a `--no-checkout` worktree with checkout
inside the container. It needs entrypoint changes and still mounts Git
metadata. *Alternative:* mounting over the pointer file. Impossible: no
runtime binds a directory onto a file.

### 4. jj backend setup

As in the branch's `vcs-session-modes` Decision 2: `jj workspace add
--no-colocate`; move `W/.jj` aside; `jj git init --colocate` plus a base commit
in `W`; move the new `.jj` and `.git` to `P/jj` and `P/git`; move the host
`.jj` back; create the empty `W/.git` placeholder. Host `jj` calls use the
overrides of Decision 5.

### 5. Hardened host VCS invocation

Every host `git`/`jj` call of this mode goes through `host_tools` from
`harden-host-command-execution`, which lands first: absolutely resolved
programs, a neutral cwd, `-C`/`-R`, Git `-c core.hooksPath=/dev/null -c
core.fsmonitor=false` with `commit --no-verify`, and the jj overrides.
`--no-verify` matters even with hooks disabled: a host pre-commit framework
would read the agent-editable `.pre-commit-config.yaml`. The private
repository's base commit is made with `-c init.templateDir=` so no template
hooks are copied into it.

### 6. Capture

Git backend, under the lease and repository lock:

1. Check that `W/.git` is an empty, non-symlink directory and `P/host-git` a
   regular file whose `gitdir:` names this worktree's entry in the main
   repository. Otherwise keep everything and print recovery steps.
2. Remove the placeholder and move `P/host-git` back to `W/.git`.
3. Refuse (keep, print paths) if any `.git` or `.jj` entry exists below `W`'s
   root, walking without following links.
4. If `W` is dirty: `git add -A`, then `git commit --no-verify -m <message>`.
5. `git worktree unlock`; then, unless failed or kept, remove `W` and `P/git`
   and `P/host-git` (never `.session-vcs/`), as `worktree remove --force` does
   today.

On keep or failure, steps 1–4 still run, so the host metadata is restored and
the result recorded; `P/git` stays for resume.

jj backend: the branch's Decision 3 (snapshot `W` with host `jj`, describe or
use `@-`, set the bookmark, forget and remove unless kept), plus the
nested-repository refusal of step 3.

### 7. Resume and interrupted sessions

| State at launch for the branch | Action |
|---|---|
| no `W`, no `P` | fresh setup |
| `W` with host pointer, `P/git` present | resume: re-run setup steps 2–3 and 5 (lock, park, placeholder), keep `P/git` |
| `W/.git` placeholder, `P/host-git` and `P/git` present | interrupted session: restore the pointer (capture step 1–2 checks), then resume |
| `W` without `P`, or `P` without `W` | refuse with paths and cleanup commands |
| worktree created by an older version (`full` mode) | refuse: it has no private repository; print how to finish it in `full` mode first |

jj follows the branch's resume rules.

### 8. Reporting

The CLI prints the session directory, the branch or bookmark, and at the end
the command to inspect the result (`git log <branch>` / `jj log -r <bookmark>`).
The JSON summary keeps `workspace_path` and `bookmark`, and adds
`vcs_access`. Generated devcontainers are rendered as for `full`; the launch
says that isolation applies to CLI sessions only.

## Risks / Trade-offs

- [Tracked `.gitattributes` can route agent files through a filter or diff
  driver configured in trusted host config, during host `git add`] →
  Unchanged from the restricted modes and documented there. Hooks and
  fsmonitor are closed. A later change can neutralize filters on capture.
- [Agent commit messages and structure are lost] → Intended; the session
  message describes the snapshot. The agent can still use commits inside the
  session.
- [A user runs `git` in `W` during a session and finds an empty `.git`
  directory] → The worktree is locked with a reason naming project-sandbox;
  docs explain it. `git worktree list` shows it as locked.
- [The CLI dies with the pointer parked] → The worktree is locked, so prune
  won't drop it; the next launch for the branch restores it (Decision 7), and
  docs give the manual restore command.
- [Setup scans the tree twice (checkout plus base commit)] → Not measured; the
  branch's users had small repositories. Measure on a large repository before
  advertising the mode for one.
- [Freshly created mount sources can be invisible to VM-backed runtimes for
  about a second] → The existing metadata-visibility wait covers `P`.
- [Native Linux and rootful Podman were not part of the mount spike] → The e2e
  suite runs on every available runtime; isolated mode is refused on a runtime
  whose e2e fails to hide the host metadata.

## Migration Plan

Opt-in; nothing changes for existing users. Worktrees and workspaces from
earlier sessions keep working in `full` mode. Rollback is a revert: a session
directory left by this mode is an ordinary worktree or workspace once its
pointer is restored (`git worktree unlock`, then move `P/host-git` back), which
the docs describe.
