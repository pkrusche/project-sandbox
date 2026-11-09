# Proposal

## Why

Sandboxed agents can write files that the user's *own* host tools later execute automatically, outside the sandbox:

- **Git metadata.** In sessions without `--branch`, the project's `.git` is part of the read-write `/workspace` mount. In colocated jj `--branch` sessions, the main repository's `.git` is mounted read-write, because jj in the container writes commits there. An agent can:
  - plant `.git/hooks/*`;
  - set `core.hooksPath`, `core.fsmonitor`, filter/diff/merge drivers, `core.sshCommand`, `gpg.program`, `!`-aliases or `include.path` in `.git/config`;
  - select drivers through `.git/info/attributes`.

  Each runs as the host user on the user's next `git status`, `commit`, `pull` or `push`. This is the user-facing half of finding #2, which `jj-only-branch-workspaces` doesn't cover.
- **Agent and IDE project config** that runs commands without a fresh trust prompt once a project is trusted:
  - Claude Code's `.claude/settings*.json` (hooks, `apiKeyHelper`, `statusLine`) and `.mcp.json` (MCP server commands);
  - Codex's `.codex/`, Pi's `.pi/` and OpenCode's `opencode.json`/`.opencode/` (plugins are code);
  - VS Code's `.vscode/tasks.json` (`runOn: folderOpen`), `settings.json` (interpreter and terminal paths) and `launch.json`;
  - JetBrains run configurations.

These are not bugs in project-sandbox's own host operations. They are the next host step after a session ends, and the user has no signal that a session touched them.

Files with their own change confirmation are out of scope:
- direnv's `.envrc` and mise's config re-prompt on change;
- project-sandbox-tui confirms changes to `.project-sandbox-tui.toml`;
- `.devcontainer/` is always masked by `private-session-state`.

## What Changes

- **Git hooks and info are read-only in every sandbox.** The real `.git/hooks/` and `.git/info/` are bind-mounted read-only over themselves, in both sessions without `--branch` (`/workspace/.git/…`) and colocated jj `--branch` sessions (the Git-backend mount). Existing hooks keep running inside the sandbox, but can't be changed. These are directory mounts, so they work on Apple `container`.
- **Git config files are restored after every session.**
  - Before the container starts, the host snapshots `.git/config` and any `.git/worktrees/*/config.worktree`.
  - After the container exits, before any other teardown step, each changed file is moved aside to `.git/config.project-sandbox-<session-id>` (or the worktree equivalent), and the snapshot is restored, without following links.
  - The session-end report names the changed keys, flagging exec-capable ones.
  - Agents can still run `git config` during a session; the change just doesn't outlive it.
- **Changes to host-executed project config are reported.** Before and after each session, the host records a no-follow manifest (type, size, hash) of:
  - the watched paths above;
  - the directory named by the pre-session `core.hooksPath`, when it is inside the project (for example `.husky/`, `.githooks/`).

  After the session, it prints a report listing each changed, added, removed or retyped path (for example, now a symlink). It adds the same list to the `--json-summary` (`config_changes`). The report never blocks or reverts. Many of these files are legitimately edited and committed.
- **Applies to the right tree.** Sessions without `--branch` watch the project. `--branch` sessions watch the jj workspace *and* restore and protect the main repository's `.git`.

## Capabilities

### New Capabilities

- `host-executed-config-protection`: which repository files that host tools execute are made read-only in sandboxes, restored after sessions, or reported, and how the report is delivered.

### Modified Capabilities

(none. The JSON summary gains one additive field. The fields project-sandbox-tui reads, `pid`, `exit_code` and `session_id`, are unchanged.)

## Impact

- **Code:**
  - A new `host_config_guard.py` (snapshot, restore, manifests, report).
  - `cli.py`: snapshot before `run`, restore and report right after the container exits (before finalization and the summary), the `config_changes` summary field.
  - `container_cli.py` and `jj_workspace.py`: read-only `hooks/` and `info/` sub-mounts.
- **Behavior:**
  - **BREAKING (minor):** agents can no longer install or edit Git hooks or `info/` files, and their `.git/config` edits are reverted after the session (and kept aside).
  - A user editing `.git/config` on the host *during* a session, for example with `git push -u`, has that edit reverted too. It's kept in the same quarantine file, and the report says how to re-apply it.
- **Depends on:**
  - `jj-only-branch-workspaces`, for the computed Git-backend mount;
  - the `PrivateDir` primitive from `private-session-state` for safe restoration. Until then, restoration uses `O_NOFOLLOW` opens and atomic replacement in `.git/`.
- **Docs:** `docs/security.md` (host-executed config; the window while concurrent sessions run), `docs/usage.md` (the session-end report, quarantine files, the summary field).
- **Out of scope:**
  - Blocking or reverting agent edits to watched project files: they are reported only.
  - Arbitrary project code the user later runs (build scripts, tests, `Makefile`s), which is reviewed as part of the diff.
