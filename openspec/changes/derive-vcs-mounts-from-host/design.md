# Design

## Context

- `jj_workspace.repo_store_mount(repo, ws)`: source `repo/.jj/repo` (host-known),
  target read from `ws/.jj/repo` (absolute allowed), falling back to the
  relative path when the file is absent.
- `jj_workspace.git_backend_mount(repo, ws)`: source from
  `repo/.jj/repo/store/git_target` (absolute allowed), target mirrored under the
  store target.
- `cli.py:3376–3386` adds both mounts read-write in `full` mode; the
  `read-only`/`hidden` path uses `vcs_access.inspect`, whose `pointer()`
  resolves gitfiles, `commondir`, `.jj/repo` and `git_target`.
- Git `--branch` sessions in `full` mode already bind `project/.git` (host-known)
  at its own path; the worktree's gitfile then resolves inside it.
- Writers: jj writes the workspace pointer as a relative path from `ws/.jj`
  to `repo/.jj/repo`, and `git_target` as a relative path from the store to the
  colocated `.git` (`../../../.git`). Git writes a worktree's gitfile as
  `gitdir: <abs>/.git/worktrees/<name>`, its `commondir` as `../..`, and the
  back-reference `<main>/.git/worktrees/<name>/gitdir` as `<abs W>/.git`.
  Task 1.1 confirms these on fixture repositories.

## Goals / Non-Goals

**Goals:**
- No pointer content chooses a mount source or target for host-created layouts.
- Pointer-derived mounts of in-place layouts can only expose VCS metadata, at
  targets that don't cover container system paths.

**Non-Goals:**
- Protecting metadata contents (hooks, config): `protect-host-executed-project-config`.
- Isolated sessions, which mount no host metadata (`isolated-vcs-access`).

## Decisions

### 1. Compute, then compare

For `--branch`, the host knows `repo` and `ws`. It computes:

- jj store: source `repo/.jj/repo`, target
  `posix_join("/workspace/.jj", relpath(repo/.jj/repo, ws/.jj))`;
- jj Git backend: source `repo/.git` (only if `git_target` resolves exactly to
  it), target the store target joined with jj's relative `git_target` form;
- Git: unchanged (`repo/.git` at its own path).

It then reads each pointer only to compare it with the value the writer would
have produced (normalized, relative or absolute spelling of the same path). A
mismatch refuses with the file, the expected value and the recovery command.

*Alternative:* sanitize the pointer (reject absolute values, `..` beyond the
repo). Rejected: it still lets the file choose among valid-looking targets.

### 2. In-place structure checks

For in-place `read-only`/`hidden`, the layout comes from the user's checkout,
which can legitimately be a linked worktree of a repository elsewhere. A
pointer-derived source is accepted only when:

- it is a Git directory (`HEAD` and `objects/`, or a linked worktree entry with
  `gitdir` and `commondir`) or a jj store (`store/type` or `op_store/`);
- for a linked Git worktree, the entry's `gitdir` back-reference names this
  workspace's `.git`;
- it is not, does not contain and is not inside a protected location (the list
  from `validate-all-mount-sources`, or until that lands: `$HOME` itself, the
  egress state and policy directories, the project-sandbox state root, and
  known credential directories).

Targets must be under `/workspace` or equal the host path, and never under the
container system prefixes listed in the spec.

## Risks / Trade-offs

- [Hand-edited or relocated repositories are refused] → The message names the
  file, expected value and fix (`jj workspace update-stale`, re-create the
  workspace, or `git worktree repair`).
- [A writer format changes in a future jj/Git release] → Comparison normalizes
  spelling; fixture tests pin the current formats and fail loudly on change.
- [Structure checks can be satisfied by a crafted directory] → Such a directory
  is still only mounted read-only, at a target outside system paths, and can't
  be a protected location.
