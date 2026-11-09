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

`host_tools.resolve(name)` walks `PATH`, skips empty and relative entries and entries under the project or any session directory, and caches the result. A tool found only through a skipped entry is reported as missing.

### 2. Neutral directory

`host_tools.run` uses a private empty directory: the session's `empty/` after `private-session-state`, otherwise `mkdtemp` with mode 0700. Runtime `build` keeps its context directory as cwd, because Apple requires it; the binary is still resolved absolutely.

### 3. jj overrides

`_jj` always adds `--config signing.behavior=drop --config fsmonitor.backend=none --config ui.paginate=never`, and uses `host_tools.resolve("jj")`. Task 1.1 reviews jj's config reference for other exec-capable keys that the commands above can trigger, and extends the list.

Config-id records from the earlier draft are dropped. In isolated mode the identifiers live in host-owned metadata. In shared mode project-sandbox runs no host jj command against the project, and the user's own jj is covered by `protect-host-executed-project-config`.

### 4. `doctor`

Reports the host jj version, and warns for releases that still read `.jj/repo/config.toml` (task 1.1 determines the threshold).

## Risks / Trade-offs

- [Unknown exec-capable jj settings] → Task 1.1's review, plus a marker test for signing.
- [A user whose only `jj` is on a relative `PATH` entry] → Reported as missing, with the skipped entry named.
