# Proposal

## Why

project-sandbox runs tools on the host in ways a repository, and therefore a
sandboxed agent, can influence:

- **Working directory.** `oauth_refresh` runs `claude auth status` /
  `codex login status` in the caller's directory, usually the project, where
  agent-writable project settings (Claude's `apiKeyHelper`, `env`, hooks;
  Codex's project config) may be loaded.
- **Lookup.** Tools are found through `PATH` by bare name, so an empty or
  relative entry makes the project a lookup location.
- **Host Git capture.** After a Git `--branch` session the host runs
  `git add -A` and `git commit` (`worktree.py`) against the main repository's
  `.git`, which `full` sessions can write. A planted `pre-commit` hook,
  `core.hooksPath` or `core.fsmonitor` program runs on the host.
- **Host jj calls.** `jj_workspace._jj` passes no overrides for exec-capable
  settings (signing, fsmonitor, paging, the Git executable).

The `sec-fixing-claude` branch implemented and archived the resolver,
neutral-cwd and jj parts (2026-11-10). This change ports them and adds the Git
capture hardening that branch didn't need, because it had removed the Git
worktree backend.

## What Changes

- **Host tools are resolved absolutely.** One resolver finds `jj`, `git`, the
  container runtime, agent CLIs, `pass`, `gh` and `security` through absolute
  `PATH` entries outside the project and every session directory. The launch
  narrows `PATH` to those entries for its own duration.
- **Host tools run from a neutral directory.** Token-refresh delegates, `pass`,
  `gh`, `security` and `git config --global` run with a private empty cwd.
  `git` and `jj` always receive their repository with `-C` / `-R`.
- **Host Git calls skip hooks and fsmonitor.** Every host `git` call passes
  `-c core.hooksPath=/dev/null -c core.fsmonitor=false`, and capture commits
  pass `--no-verify`.
- **Host jj calls are hardened.** Every host `jj` call passes `--config`
  overrides for signing, fsmonitor, paging and the Git executable.
- **`doctor` warns** when the current jj repository has a `.jj/repo/config.toml`
  without `.jj/repo/config-id`, which jj adopts and trusts on its next command.

## Capabilities

### New Capabilities
- `host-command-execution`: how project-sandbox finds and runs tools on the
  host (lookup, working directory) and how its host Git and jj invocations are
  kept from running programs that repository configuration names.

### Modified Capabilities

None.

## Impact

- Code: new `host_tools.py` (resolver, neutral-cwd runner, launch-scoped
  `PATH`, Git/jj override lists); updated `worktree.py`, `jj_workspace.py`,
  `oauth_refresh.py`, `copilot_auth.py`, `agent_proxy.py`, `config_agents.py`,
  `git_identity.py`, `doctor.py`, `cli.py`.
- Behaviour: repository hooks no longer run on the snapshot commits
  project-sandbox makes at the end of `--branch` sessions; signing configured
  only in jj repository config is skipped for host jj calls. Neither changes
  the user's own later `git`/`jj` use.
- Interplay: `isolated-vcs-access` uses this change's helper for its setup and
  capture, so this change lands first. `protect-host-executed-project-config`
  covers config-defined filters, which overrides can't enumerate.
- Docs: `docs/security.md` (host execution model), `docs/usage.md` (`doctor`).
