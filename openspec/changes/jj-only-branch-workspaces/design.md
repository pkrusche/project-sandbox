# Design

## Context

- **Backend choice:** `cli._setup_worktree` / `_plan_worktree` (`cli.py:2843–2884`) pick jj when `<project>/.jj` exists, and Git worktrees otherwise. Finalization dispatches the same way (`cli.py:2926–2934`).
- **jj session mounts** (`cli.py:3060–3076`):
  - `repo_store_mount` (`jj_workspace.py:183`): the source is `<repo>/.jj/repo`. The target is taken from the content of `<workspace>/.jj/repo` (the pointer jj writes, a path relative to `<workspace>/.jj`), used verbatim if absolute.
  - `git_backend_mount` (`:201`): reads `<repo>/.jj/repo/store/git_target` (here `../../../.git`). If absolute, both source and target are that path; if relative, the source is resolved against the store and the target is mirrored.

  Both are formatted into `type=bind,source=…,target=…` without validation. In the container, `/workspace` (with the pointer) and the store are writable by the agent, so the next launch's mounts are agent-chosen.
- **Host jj calls:** `_jj` (`jj_workspace.py:355`) runs `jj -R <path> …` for `workspace add/forget`, `bookmark create/set/delete/list`, `status`, `describe` and `log`, plus the `workspace add --help` probe. jj 0.46 reads repository and workspace config from `~/.config/jj/{repos,workspaces}/<id>/config.toml`, selected by `.jj/repo/config-id` and `.jj/workspace-config-id`.
- **Host tool inventory** (from the earlier draft; unchanged): `claude`/`codex` refresh (caller's cwd), `pass`, `gh`, `security`, `git config --global`, and the runtime binary (build cwd = context, needed by Apple).

## Goals / Non-Goals

**Goals:**
- project-sandbox's host operations never execute repository-controlled code, and never mount or target anything chosen by repository files.
- One session per jj workspace.

**Non-Goals:**
- Protecting the user's later `git` use in a colocated repository (`protect-host-executed-project-config`).
- Per-session private clones (ROADMAP).
- Converting Git repositories.

## Decisions

### 1. Remove the Git backend instead of hardening it

The hardened alternative was drafted first: a finalize helper container, an in-container checkout, a metadata-only Git wrapper and `index.lock` proofs. It added a container start per session and four new mechanisms, and still depended on how complete a `-c` override list was. jj runs no hooks, filters or fsmonitor commands from repository content. Removing the Git backend deletes the surface instead.

The project-sandbox-tui already supports only jj repositories (`vcs.detect` requires `.jj`).

The error for Git-only projects is raised in `_plan_worktree` and `_setup_worktree` before any side effect, including during `--dry-run`. It doesn't offer to run `jj git init --colocate`, because initializing changes the user's repository and tooling expectations.

### 2. Derive mounts from the host layout and verify the pointers

```
host computes                                   compared with (agent-writable)
──────────────                                  ──────────────────────────────
store_src  = realpath(<repo>/.jj/repo)          —
rel        = relpath(store_src, <ws>/.jj)       <ws>/.jj/repo   (pointer content)
store_tgt  = normpath("/workspace/.jj/" + rel)
git_src    = <repo>/.git  (if colocated)        store/git_target == relpath(git_src, store_src/store)
git_tgt    = normpath(store_tgt + "/store/" + relpath(git_src, store_src/store))
```

- **Colocation:** detected on the host as `<repo>/.git` being a directory and `git_target` being *expected* to point at it. The expected value comes from the host layout, never from the file.
- **Comparison:** the pointer and `git_target` are compared byte-for-byte, after stripping a trailing newline, with the computed relative paths.
  - A mismatch refuses the launch with "recreate the workspace: `jj workspace forget <name>` and run the launch again".
  - jj in the container follows the pointer, so a tampered pointer would also break jj there. Refusing is strictly better than mounting.
- **Validation** of the computed values: sources are realpaths under `<repo>`, not symlinks (checked with `lstat`). Targets are absolute, contain no `..` after `normpath`, and end in `/.jj/repo` or `/.git`. Neither contains `,` or a control character. Because a target always ends in one of those suffixes, it can't overlay image paths such as `/usr/local/bin`.

**Alternative considered:** rewrite the pointer files to the expected content before launch, instead of refusing. That's a host write into the agent-writable workspace, and it hides tampering. Rejected. Refusing is visible, and the recovery is one command.

### 3. jj overrides and config-id records

- **Overrides:** `_jj` always adds `--config signing.behavior=drop --config fsmonitor.backend=none --config ui.paginate=never`, plus `--no-pager` where supported. It uses `host_tools.resolve("jj")`.
- **Config-id records:**
  - Stored as `jj-config-ids/<sha256(realpath repo)>.json` in host state, holding the repo `config-id`, and the `workspace-config-id` per workspace path.
  - Taken on first host jj use, and checked before `setup` and before `finalize`.
  - A mismatch skips host jj operations and leaves the workspace in place. The message offers `project-sandbox --accept-jj-config <project>` to re-record the identifiers once the user has checked them.
  - Until `private-session-state` lands, the records live under `$XDG_STATE_HOME/project-sandbox/`. Afterwards they move to the per-project directory.
- **`doctor`:** reports the host jj version, and warns for releases that still read `.jj/repo/config.toml` (task 1.1 determines the threshold).

### 4. Tool resolution and neutral directory

As in the earlier draft:
- `host_tools.resolve` skips empty and relative `PATH` entries and entries under the project or workspace, and caches results.
- `host_tools.run` uses a private empty directory: the session's `empty/` after `private-session-state`, otherwise `mkdtemp` with mode 0700.
- Runtime `build` keeps its context directory as cwd, because Apple requires it; the binary is still resolved absolutely.

### 5. Workspace lease

- `host_locks.path_lock("workspace-lease", <workspace path>)`, taken non-blocking at setup and held until finalization returns.
- On contention, the launch fails with the holder's session ID, found in session records by `workspace_path`.
- Lease acquisition also refuses while a session record for the workspace is `running` and the runtime reports its container alive. That covers a CLI killed while its container continued.
- The existing `_workspace_lock` (`jj-workspace`) still serializes the short host jj mutations.

## Risks / Trade-offs

- [Users of `--branch` in plain Git repositories] → **BREAKING**. They get a clear one-line migration (`jj git init --colocate`), which keeps Git working side by side. Release notes and `docs/usage.md` explain it.
- [jj in the container can't follow a tampered pointer either] → The refusal message gives the recovery.
- [Unknown jj exec-capable settings beyond signing, fsmonitor and paging] → The config-id records make the effective config the user's own. Task 1.1 reviews jj's config reference for exec-capable keys used by non-interactive commands, and extends the overrides if needed.
- [A colocated `.git` stays writable in the container] → jj needs it. The user-facing risk is handled by `protect-host-executed-project-config`, and documented here.

## Migration Plan

- Existing Git worktrees made by older versions are left untouched. Users remove them with `git worktree remove` as needed.
- jj workspaces from older versions are validated on their next use. Pointers jj wrote match the computed values. Pointers changed by an agent are refused.
