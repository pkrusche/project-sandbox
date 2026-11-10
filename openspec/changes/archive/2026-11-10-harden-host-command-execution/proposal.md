# Proposal

## Why

project-sandbox runs several tools on the host in ways a repository can influence:

- **Working directory.** `oauth_refresh` runs `claude auth status` / `codex login status` in the caller's directory, usually the project. Agent-writable project settings (Claude's `apiKeyHelper`, `env`, hooks; Codex's project config) may be loaded there.
- **Lookup.** Tools are looked up through `PATH` by bare name, so an empty or relative entry makes the project a lookup location.
- **jj configuration.** Host `jj` calls pass no overrides for exec-capable settings such as signing, fsmonitor and paging.

This change was first drafted as `jj-only-branch-workspaces`. Its other half, the `--branch` mounts, pointer checks, config-id records and workspace lease, is replaced by `vcs-session-modes`: in isolated mode the host's jj metadata is never visible to an agent, so those checks have nothing left to check. What remains here is how the host finds and runs tools.

## What Changes

- **Host tools are resolved absolutely.** One resolver finds `jj`, `git` (identity lookup only), the runtime, agent CLIs, `pass`, `gh` and `security` through absolute `PATH` entries outside the project and every session directory.
- **Host tools run from a neutral directory.** The OAuth refresh delegates, `pass`, `gh`, `security` and `git config --global` run with `cwd` set to a private empty directory. `jj` always receives its repository with `-R`.
- **Host jj calls are hardened.** Every host `jj` call passes `--config` overrides for its exec-capable settings: signing, fsmonitor, paging and the Git executable.
- **`doctor` warns** when the current jj repository holds a `.jj/repo/config.toml` that jj would adopt: one without a `.jj/repo/config-id`. Every jj release, including 0.46, trusts such a file, so an agent in a shared session could plant it for the user's own later `jj` use.

## Capabilities

### New Capabilities

- `host-command-execution`: how project-sandbox finds and runs tools on the host (lookup, working directory), and how host jj invocations are hardened.

### Modified Capabilities

(none)

## Impact

- **Code:**
  - New: `host_tools.py` (resolver and neutral-cwd runner).
  - Updated: `jj_workspace.py` (`_jj`), `oauth_refresh.py`, `copilot_auth.py`, `agent_proxy.py`, `config_agents.py`, `git_identity.py`, `doctor.py`.
- **Interplay:**
  - `vcs-session-modes` uses the resolver and the jj overrides for its setup and snapshot commands. Either change can land first; the second one switches the remaining call sites.
  - `protect-host-executed-project-config` runs its `git config --file` parse through this runner.
- **Docs:** `docs/security.md` (the host execution model).
- **Supersedes:** the `branch-workspaces` capability of the earlier `jj-only-branch-workspaces` draft, and before it `harden-host-tool-invocations`.
