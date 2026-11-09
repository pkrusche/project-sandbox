# Proposal

## Why

`--branch` has two backends. Both let files that the sandboxed agent can write steer what the host does.

- **Git worktrees (finding #2).**
  - The main repository's `.git` is mounted read-write.
  - The host then runs `git worktree add` (a checkout), `status`, `add -A` and `commit` inside the agent's worktree. These run hooks, `core.fsmonitor`, signing programs, and clean/smudge filters chosen by an agent-writable `.gitattributes`.
  - No set of `-c` overrides neutralizes filters whose driver names the agent chooses.
  - Making the Git backend safe would need a sandboxed finalize helper, an in-container checkout, a hardened metadata wrapper and lock-staleness proofs.
- **jj mounts (new finding, confirmed by reading the code).** `jj_workspace.repo_store_mount` and `git_backend_mount` build bind mounts from agent-writable files.
  - The container *target* comes from the workspace's `.jj/repo` pointer, inside `/workspace`. A target like `/usr/local/bin` places the writable store over image scripts. The root stage would then run agent-controlled code as root, so the firewall falls.
  - The host *source* of the Git-backend mount comes from `.jj/repo/store/git_target`, inside the read-write store. An absolute or `../..` value mounts an arbitrary host directory, such as `$HOME`, read-write into the next session.
  - Both values are pasted unescaped into the comma-separated mount string.
- **jj configuration.** jj 0.46 keeps repository config outside the repository, but selects it through `.jj/repo/config-id` and `.jj/workspace-config-id`, which are agent-writable.
- **Host tools in general (new finding).**
  - `oauth_refresh` runs `claude auth status` / `codex login status` in the caller's directory, usually the project, where agent-writable project settings (Claude's `apiKeyHelper`, `env`, hooks; Codex's project config) may be loaded.
  - Tools are looked up through `PATH` by bare name, so an empty or relative entry makes the project a lookup location.
- **Workspace lifetime (finding #9).** Two sessions can use one branch's workspace at once, and one session's teardown can remove it under the other.

jj doesn't execute repository content: it has no hooks or filters. The project's tooling is already jj-centric: project-sandbox-tui supports only jj repositories. Supporting only jj for `--branch` removes the Git attack surface from project-sandbox's own host operations, rather than containing it.

## What Changes

- **BREAKING: `--branch` requires a jj repository.**
  - The Git worktree backend is removed (`worktree.py`, its tests and `scripts/e2e-git-workflow.sh`).
  - `--branch` in a Git-only repository fails before any change, with: "`--branch` needs a jj repository; run `jj git init --colocate` in <project>".
  - The repository is never initialized automatically.
  - Sessions without `--branch` are unchanged for Git repositories.
- **jj mounts are derived from the host's layout, never from repository files.**
  - The store source is `<repo>/.jj/repo`. Its container target is computed from the host's own relative path between the workspace and the store.
  - The Git-backend source is `<repo>/.git` for a colocated repository, and nothing otherwise. Its target is computed the same way.
  - Before launch, the workspace pointer and `store/git_target` are compared with these computed values. A mismatch means tampering, and refuses the launch with recovery guidance.
  - Sources must stay inside the repository. Targets must end in `/.jj/repo` or `/.git`, contain no `..` after normalization, and carry no commas or control characters.
- **jj host operations are hardened.**
  - Every host `jj` call passes `--config` overrides for its exec-capable settings: signing, fsmonitor and paging.
  - The repository's and workspace's config-ids are recorded on first use, in host state outside the repository. Setup and finalization refuse host jj operations when either differs.
  - `doctor` warns when the host jj predates storing repository config outside the repository.
- **Host tools are resolved absolutely and run from a neutral directory.**
  - One resolver finds `jj`, `git` (identity lookup only), the runtime, agent CLIs, `pass`, `gh` and `security` through absolute `PATH` entries outside the project and workspace.
  - The OAuth refresh delegates, `pass`, `gh`, `security` and `git config --global` run with `cwd` set to a private empty directory.
- **Workspace leases.** A session holds an exclusive lease on its jj workspace from setup to finalization. A second session for the same bookmark, or one running while an orphaned container still uses the workspace, fails fast and names the running session.

## Capabilities

### New Capabilities

- `host-command-execution`: how project-sandbox finds and runs tools on the host (lookup, working directory) and how host jj invocations are hardened.
- `branch-workspaces`: what `--branch` requires (jj), how session workspaces are leased, and how their mounts are derived and validated.

### Modified Capabilities

(none. No existing spec covers `--branch`. The `egress-sidecar` policy-location requirement refers to "the worktree" generically, and still holds for jj workspaces.)

## Impact

- **Code:**
  - Removed: `worktree.py`, `tests/test_worktree.py`, `scripts/e2e-git-workflow.sh`, and the Git branches in `cli.py` (`_setup_worktree`, `_plan_worktree`, `_wait_for_worktree_metadata`, finalize dispatch, the `.git` metadata mount).
  - Changed: `jj_workspace.py` (mount derivation and validation, overrides, config-id records, lease).
  - New: `host_tools.py` (resolver and neutral-cwd runner).
  - Updated: `oauth_refresh.py`, `copilot_auth.py`, `agent_proxy.py`, `config_agents.py`, `git_identity.py`, `doctor.py`.
- **State:** config-id records live in host state: `$XDG_STATE_HOME/project-sandbox/`, or the per-project directory from `private-session-state` once that lands.
- **Docs and repo guidance:**
  - `docs/usage.md` (`--branch` requires jj; migration hint; one session per bookmark), `docs/security.md`, `README.md`;
  - `CLAUDE.md`'s note that `--branch` mounts the main repository's `.git` for Git worktrees;
  - ROADMAP's private-clones item drops its Git-worktree note.
- **Not changed:**
  - In colocated repositories, the main repository's `.git` is still mounted read-write, because jj in the container writes commits there. An agent can therefore still plant `.git/hooks` or `.git/config` that run when *the user* later runs `git`. That is covered by `protect-host-executed-project-config`.
  - project-sandbox's own host operations no longer run Git inside repositories, except `git config --global` for the identity.
- **Supersedes:** the earlier `harden-host-tool-invocations` draft (finalize helper, in-container checkout, Git metadata wrapper, `index.lock` policy). None of that is needed without the Git backend.
