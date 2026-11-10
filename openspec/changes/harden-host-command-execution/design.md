# Design

## Context

- **Host jj calls:** `_jj` (`jj_workspace.py:355`) runs `jj -R <path> …` with a bare `jj` and no overrides. After `vcs-session-modes`, these calls are `workspace add/forget`, `git init`, `commit`, `status`, `describe`, `bookmark create/set/delete/list` and `log`.
- **Host tool inventory:** `claude`/`codex` refresh (caller's cwd), `pass`, `gh`, `security`, `git config --global`, and the runtime binary (build cwd is the context, which Apple needs).
- **jj 0.46** reads repository and workspace config from `~/.config/jj/{repos,workspaces}/<id>/config.toml`, selected by `.jj/repo/config-id` and `.jj/workspace-config-id`. Earlier releases read `.jj/repo/config.toml` inside the repository.

## Goals / Non-Goals

**Goals:**
- No host tool is found through, or run from, a directory an agent can write.
- Host jj calls don't run programs named by configuration.

**Non-Goals:**
- What `--branch` and the VCS modes mount (`vcs-session-modes`).
- Protecting the user's own later `git` or `jj` use (`protect-host-executed-project-config`).

## Decisions

### 1. Tool resolution

`host_tools.resolve(name)` walks `PATH`, skips empty and relative entries and entries under the project or any session directory (`<repo>-workspaces/`), and skips a candidate whose real path lies under one of them. A tool found only through a skipped entry is reported as missing, and the message names the skipped entry. Lookups are not cached: they are cheap, and a cache would go stale when `PATH` or the roots change.

The container runtime is run by name from many modules. Rather than resolving it at each one, the CLI narrows `os.environ["PATH"]` to the trusted entries for the whole launch (`host_tools.launch_scope`) and restores it afterwards. This also covers the helpers the runtime itself looks up. Runtime selection uses `resolve`.

### 2. Neutral directory

`host_tools.run` uses a private empty directory: the session's `empty/` after `private-session-state`, otherwise `mkdtemp` with mode 0700. Runtime `build` keeps its context directory as cwd, because Apple requires it; the binary is still resolved absolutely.

### 3. jj overrides

`_jj` always adds `--color=never` and these overrides, and uses `host_tools.resolve("jj")`:

- `signing.behavior=drop`
- `fsmonitor.backend=none`
- `ui.paginate=never`
- `git.executable-path="<host git>"`: the resolved host `git`. When there is none, the value is a missing path in the neutral directory; `workspace forget` still succeeds and skips its prune.

Task 1.1 results (jj 0.46, fixture repository whose config names marker-writing programs, commands run on a terminal):

- Without overrides, the host commands ran `watchman` (fsmonitor), the pager (`status`, `log`, `bookmark list`), the SSH signing program (`describe`, `commit`) and the diff formatter (`diff`).
- `workspace add` without `--no-colocate` runs `git worktree add` through `git.executable-path`, and `workspace forget` in a colocated repository runs `git worktree prune` through it. That setting was missing from the first list.
- With all overrides, `status`, `log`, `describe`, `commit`, `bookmark set/list`, `workspace add --no-colocate`, `workspace forget` and `git init --colocate` ran no configured program.
- `ui.editor` and `ui.diff-editor` are reached only by commands without `-m` or by interactive commands; every host call passes `-m`. Aliases cannot shadow built-in commands ("Cannot define an alias that overrides the built-in command").

Config-id records from the earlier draft are dropped. In isolated mode the identifiers live in host-owned metadata. In shared mode project-sandbox runs no host jj command against the project, and the user's own jj is covered by `protect-host-executed-project-config`.

### 4. `doctor`

Task 1.1 found no version threshold. jj 0.46 stores repository config under `~/.config/jj/repos/<config-id>/`, but when `.jj/repo/config-id` is missing it migrates a `.jj/repo/config.toml` found in the repository and trusts it: the planted signing program ran on the next `jj describe`. Deleting `config-id` and planting the file again works the same way. Releases before the out-of-repo storage read the file directly. Every release therefore lets an agent in a shared session plant repository config for the user's next `jj` command. A planted workspace-level `.jj/config.toml` is not adopted.

`doctor` instead reports the host jj version and, run inside a jj repository, warns when `.jj/repo/config.toml` exists and `.jj/repo/config-id` does not: the next jj command there adopts that file. After a migration jj leaves the old file in place next to `config-id`, so that state is not reported. `protect-host-executed-project-config` must cover this state too: deleting `config-id` is a change to the jj config pointers it restores.

## Risks / Trade-offs

- [Unknown exec-capable jj settings] → Task 1.1's review, plus a marker test for signing. A future jj release may add settings that run programs; the fixture test covers only the ones listed.
- [A user whose only `jj` is on a relative `PATH` entry] → Reported as missing, with the skipped entry named.
