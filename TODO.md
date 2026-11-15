# TODO - outstanding items for next release

- `docs/runtime.md` step 2 of "End-to-End Flow" (line 11) says agent
  credentials are staged "under `/tmp`". CLI runs now stage them in private
  session inputs under the state directory
  (`$XDG_STATE_HOME/project-sandbox/session-inputs/<id>`, from
  `session_inputs.allocate()`); only the persistent devcontainer staging is
  under `/tmp/project-sandbox-<uid>/...`, which line 105 of the same page states
  correctly. Reword step 2 to match and link `security.md#private-session-inputs`.
  Found while checking Colima mount visibility; not part of `add-colima-runtime`.

## Review findings that need a decision (2026-11-15)

- **Host lock files live in the shared, world-writable temp dir.**
  `host_locks.lock_path()` puts every lock at a predictable name
  (`$TMPDIR/project-sandbox-<kind>-<hash>.lock`). `O_NOFOLLOW` stops symlink
  redirection, but another local user can pre-create a name (our open then
  fails with EACCES) or open and `flock` it (our launches block forever).
  The cross-runtime `image` lock is also unusable across users today, because
  the first user creates it `0600`. Options: move locks into a private per-user
  directory (`$XDG_RUNTIME_DIR` or the 0700 state root), which gives up
  cross-user serialization of a shared Docker store; or keep a shared directory
  with a group-owned, setgid layout. Decide which threat model applies.
- **Colima workspace-root deny list is narrow.** `colima_profile.CREDENTIAL_DIRS`
  (and `openspec/specs/colima-hardened-profile/spec.md`) refuse only `~/.ssh`,
  `~/.aws`, `~/.gnupg`, `~/.config/gcloud` and `~/.docker`. A root of
  `~/Library` (Keychains, browser cookies), `~/.colima` / `~/.lima` (the VM's own
  config and SSH keys; a writable export lets a container change the VM
  definition), `~/.kube` or the rest of `~/.config` is accepted
  read-write. Extending the list changes the spec; decide the set.
- **Headless prompts that look like options or subcommands.** The entrypoint
  passes the prompt positionally to `codex exec` and `opencode run`
  (`set -- --skip-git-repo-check --json --color never "$PROMPT"`,
  `set -- --format json "$PROMPT"`). A prompt starting with `-` is parsed as a
  flag, and `codex exec` treats a first word such as `resume` as a subcommand.
  Inserting `--` before `"$PROMPT"` fixes Codex (clap), but OpenCode's yargs
  parser handling of `--` must be verified in the pinned versions first;
  needs an e2e run against both agents.
- **Devcontainer hidden-VCS masks are created in the system temp dir.**
  `devcontainer.render` calls `vcs_access.new_mask_dir()` (a `mkdtemp` under
  `/tmp`) and writes that path into the persistent `devcontainer.json`. Temp
  cleaners or a reboot delete it, so reopening fails until regenerated
  (documented in `docs/vcs-access.md`), and each regeneration leaks the old
  directory. Moving them under the private state root (as Colima CLI runs now
  do via `colima_mask_parent()`) changes the devcontainer initialize helper's
  validation; decide whether to do that.
- **Host-side finalization still runs repository-controlled Git config.**
  Already tracked in ROADMAP.md ("Host capture can still execute
  workspace-controlled code"); with the default `--vcs-access full` the agent
  can also edit `.git/config` (for example `core.fsmonitor`), which the host's
  `git status`/`git add`/`git commit` in `worktree.finalize` then executes.
  Passing `-c core.fsmonitor=false -c core.hooksPath=/dev/null` to those calls
  is cheap defense in depth but disables users' own hooks (and signing hooks);
  decide alongside the ROADMAP item.
