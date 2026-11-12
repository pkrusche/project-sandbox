# Design

## Context

- Host tool call sites on this line: `oauth_refresh` (`claude`/`codex` status in
  the caller's cwd), `copilot_auth` (`pass`, `gh`), `agent_proxy.resolve_key`
  (`pass`), `config_agents` (`security`), `git_identity` and `doctor`
  (`git config --global`, version probes), `jj_workspace._jj` (`jj -R`), and
  `worktree._git` (`git -C`: `worktree add/remove/prune/list`, `branch`,
  `rev-parse`, `status`, `add -A`, `commit`). The runtime binary is run by
  name from several modules.
- The Colima runtime (on main since 2026-11-15) adds host calls of `colima`
  by bare name: `colima_mounts` (`colima ssh` mount and export probes),
  `colima_profile` (`colima list`/`start`/`stop` for `colima init` and
  `add-root`) and `doctor`'s Colima checks. Its Docker calls run as
  `docker --context colima-<profile>`.
- In `full` mode a Git `--branch` session mounts the main `.git` read-write, so
  hooks and config the host later reads are agent-writable. jj `--branch`
  sessions mount the shared `.jj/repo` store read-write.
- The `sec-fixing-claude` branch's archived `harden-host-command-execution`
  (jj 0.46 research, task 1.1 there) found:
  - without overrides, host jj ran `watchman` (fsmonitor), the pager, the SSH
    signing program and the diff formatter;
  - `workspace add` (colocated) and `workspace forget` run Git through
    `git.executable-path`;
  - with the overrides below, `status`, `log`, `describe`, `commit`,
    `bookmark set/list`, `workspace add --no-colocate`, `workspace forget` and
    `git init --colocate` ran no configured program;
  - every jj release adopts and trusts `.jj/repo/config.toml` when
    `.jj/repo/config-id` is missing.

## Goals / Non-Goals

**Goals:**
- No host tool is found through, or run from, a directory an agent can write.
- Host Git and jj calls made by project-sandbox run no program that hooks or
  repository configuration name, as far as an override can express.

**Non-Goals:**
- Filters and diff drivers defined in agent-writable `.git/config` and selected
  by `.gitattributes`: Git has no switch to ignore them, and their names are
  open-ended. `protect-host-executed-project-config` restores `.git/config`
  before capture.
- Protecting the user's own later `git`/`jj` use (also
  `protect-host-executed-project-config`).
- What the VCS modes mount (`derive-vcs-mounts-from-host`, `isolated-vcs-access`).

## Decisions

### 1. Resolver

`host_tools.resolve(name)` walks `PATH`, skipping empty and relative entries,
entries under the project or a session root (`<repo>-worktrees/`, jj
workspace roots), and candidates whose real path lies under one of those. A
miss names the skipped entry. No cache. `colima` is resolved the same way. For the runtime, which many
modules run by name, the CLI narrows `os.environ["PATH"]` to trusted entries for the whole
launch (`host_tools.launch_scope`) and restores it afterwards; runtime
selection uses `resolve`.

### 2. Neutral directory

`host_tools.run` uses a private empty directory (`mkdtemp`, mode 0700, removed
at exit). Runtime `build` keeps its context directory as cwd because Apple
needs it, with the binary resolved absolutely.

### 3. Git overrides

`host_tools.git(repo, args)` runs `git -C <repo> -c core.hooksPath=/dev/null
-c core.fsmonitor=false <args>`. Capture's commit adds `--no-verify`. The
user's signing and identity config stay in effect: they are read from global
or repository config like today, and repository config is covered by
`protect-host-executed-project-config`.

The hooks override and `--no-verify` apply only to project-sandbox's own host
calls, which are automatic snapshot commits of agent output. The user's own
later commits run their hooks as usual. Main's ROADMAP (2026-11-16) weighed
fsmonitor alone as an interim step, because `core.hooksPath=/dev/null` also
skips the user's own hooks (including hooks that sign). The user decided on
2026-11-16 to keep skipping hooks on snapshot commits: hooks the user trusts
can still run scripts that live in the workspace (for example a pre-commit
framework reading the agent-editable `.pre-commit-config.yaml`), and
fsmonitor alone doesn't close that. This change supersedes the TODO.md
fsmonitor-only item.

*Alternative:* `GIT_CONFIG_NOSYSTEM` plus `GIT_CONFIG_GLOBAL=/dev/null`. It
would drop the user's identity and signing, and would not stop repository
config.

### 4. jj overrides

As in the branch: `--color=never`, `signing.behavior=drop`,
`fsmonitor.backend=none`, `ui.paginate=never`,
`git.executable-path=<resolved git>`, or a missing path in the neutral
directory when there is none, in which case `workspace forget` skips its prune.

### 5. doctor

As in the branch (Decision 4 there): report the jj version, and inside a jj
repository warn on `.jj/repo/config.toml` without `config-id`. The migrated
state (both present) is not reported.

## Risks / Trade-offs

- [Users relying on repository hooks for project-sandbox's snapshot commits,
  including hooks that sign] → Those commits are automatic snapshots of agent
  output; hooks still run on the user's own later commits, which can re-sign or
  amend the snapshot. Documented in `docs/security.md`.
- [jj signing configured only in repository config is dropped] → The snapshot
  is unsigned; global signing config is also dropped by the override. Users who
  need signed snapshots re-sign with their own `jj`.
- [Future exec-capable jj settings] → The marker tests cover the known ones.
- [A user whose only `jj`/`git` is on a relative `PATH` entry] → Reported as
  missing with the skipped entry named.
