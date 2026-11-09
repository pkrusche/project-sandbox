# Proposal

## Why

In a shared session (`--vcs shared`, see `vcs-session-modes`), the project is mounted in place, with its real `.git` and `.jj` writable. The agent can then write files that the user's *own* host tools execute later, outside the sandbox:

- **Git metadata.** An agent can:
  - plant `.git/hooks/*`;
  - set `core.hooksPath`, `core.fsmonitor`, filter/diff/merge drivers, `core.sshCommand`, `gpg.program`, `!`-aliases or `include.path` in `.git/config`;
  - select drivers through `.git/info/attributes`.

  Each runs as the host user on the user's next `git status`, `commit`, `pull` or `push`. `jj git push` and `jj git fetch` are affected too, because jj runs `git` for them (`git.executable-path`).
- **jj config pointers.** `.jj/repo/config-id` and `.jj/workspace-config-id` select which host-side config the user's `jj` loads. A legacy `.jj/repo/config.toml` is read from the repository itself by older jj releases.
- **Agent and IDE project config** that runs commands without a fresh trust prompt once a project is trusted:
  - Claude Code's `.claude/settings*.json` (hooks, `apiKeyHelper`, `statusLine`) and `.mcp.json` (MCP server commands);
  - Codex's `.codex/`, Pi's `.pi/` and OpenCode's `opencode.json`/`.opencode/` (plugins are code);
  - VS Code's `.vscode/tasks.json` (`runOn: folderOpen`), `settings.json` and `launch.json`;
  - JetBrains run configurations.

Isolated sessions don't have the first two problems: the main repository's metadata is never mounted. They can still change the third group, which then arrives in the session's result commit.

Files with their own change confirmation are out of scope:
- direnv's `.envrc` and mise's config re-prompt on change;
- project-sandbox-tui confirms changes to `.project-sandbox-tui.toml`;
- `.devcontainer/` is always masked by `private-session-state`.

## What Changes

- **Git hooks and info are read-only in shared sessions.** The real `.git/hooks/` and `.git/info/` are bind-mounted read-only over themselves. Existing hooks keep running inside the sandbox, but can't be changed.
- **Config files can't be changed during a shared session, where the platform allows it.** This covers `.git/config`, `.git/worktrees/*/config.worktree`, `.jj/repo/config-id` and `.jj/workspace-config-id`:
  - on Linux, each file is bind-mounted read-only over itself;
  - on macOS, where Apple `container` can't bind single files, the host sets the user-immutable flag on each file for the session's lifetime, and clears it afterwards.

  A spike confirms both before implementation. **BREAKING:** `git config` writes, `git remote add` and similar fail inside a shared session. On macOS the user's own host writes to those files fail too while the session runs.
- **Config files are restored after every shared session.** The host snapshots the files before launch. After the container exits, a file that differs is moved aside to `<name>.project-sandbox-<session-id>`, and the snapshot is restored, without following links. A `.jj/repo/config.toml` that appeared during the session is moved aside. This is the only protection on a platform where the hard read-only isn't available, and a backstop elsewhere.
- **A live watcher reports changes while the session runs.** The CLI process polls the protected files and the watched paths every two seconds. It writes each change to the session log and the session record as it happens, so the user or a launcher can see it before running `git` or `jj git push`. The watcher reports; it doesn't prevent.
- **Changes to agent and IDE project config are reported in both modes.** After each session, the CLI lists each watched path that was added, removed, modified or changed type, and adds the list to the `--json-summary` (`config_changes`). In isolated mode the comparison runs on the session directory. The report never blocks or reverts. Many of these files are legitimately edited and committed.

## Capabilities

### New Capabilities

- `host-executed-config-protection`: which repository files that host tools execute are made read-only in shared sessions, restored after sessions, watched or reported, and how the report is delivered.

### Modified Capabilities

(none. The JSON summary and the session record each gain one additive field. The fields project-sandbox-tui reads, `pid`, `exit_code` and `session_id`, are unchanged.)

## Impact

- **Code:**
  - A new `host_config_guard.py` (snapshot, immutable flag, restore, manifests, watcher, report).
  - `cli.py`: guard setup before `run`, the watcher thread, restore and report right after the container exits (before the summary), the `config_changes` summary field.
  - `container_cli.py`: read-only `hooks/` and `info/` sub-mounts, and the Linux file binds.
  - `observability.py`: the record's `config_changes` field.
- **Depends on:**
  - `vcs-session-modes`, for shared mode and its one-session-per-repository lease. Because of the lease, a per-session snapshot is enough.
  - `harden-host-command-execution`, for the `git config --file` parse.
  - the `PrivateDir` primitive from `private-session-state` for safe restoration. Until then, restoration uses `O_NOFOLLOW` opens and atomic replacement.
- **Docs:** `docs/security.md` (what shared mode exposes, what is protected, what the watcher can miss), `docs/usage.md` (the report, quarantine files, the summary field).
- **Out of scope:**
  - Blocking or reverting agent edits to watched project files: they are reported only.
  - Arbitrary project code the user later runs (build scripts, tests, `Makefile`s), which is reviewed as part of the diff.
