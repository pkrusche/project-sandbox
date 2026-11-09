# Tasks

## 1. Research

- [ ] 1.1 From jj's config reference and changelog:
  - find the first release that stops reading `.jj/repo/config.toml`;
  - list exec-capable settings that non-interactive commands (`workspace add/forget`, `bookmark *`, `status`, `describe`, `log`) can trigger.

  Record both in design.md Decision 3, and extend the override list if needed. Verify with a fixture repository whose repo config sets a signing program writing a marker: host `jj describe` with the overrides writes no marker.
- [ ] 1.2 Create a secondary workspace with the host jj (0.46) in colocated and non-colocated fixture repositories, and record the exact pointer and `git_target` contents jj writes. Verify that design.md Decision 2's computed values match them byte-for-byte.

## 2. Remove the Git backend

- [ ] 2.1 Make `_setup_worktree` and `_plan_worktree` fail for projects without `.jj`, with the `jj git init --colocate` hint and no side effects. Delete the Git paths: `worktree.py`, `_wait_for_worktree_metadata`, the Git finalize dispatch and the `.git` metadata mount branch. Verify with CLI tests for a Git-only project with and without `--dry-run`, and a project without `--branch` that still launches.
- [ ] 2.2 Delete `tests/test_worktree.py` and `scripts/e2e-git-workflow.sh`, and remove them from `scripts/run-e2e-tests.sh` and the e2e docs. Verify that `uv run pytest -q` passes and that `scripts/run-e2e-tests.sh --help` lists no Git workflow suite.

## 3. Mount derivation and validation

- [ ] 3.1 Replace `repo_store_mount` and `git_backend_mount` with host-layout derivation, pointer and `git_target` comparison, and value validation (design Decision 2). Verify with `tests/test_jj_workspace.py` cases:
  - an absolute `git_target`;
  - a relative `../..` `git_target`;
  - a pointer naming `/usr/local/bin`;
  - a comma in a path;
  - a symlinked `.jj/repo`;
  - a non-colocated repository (no Git mount);
  - untouched jj-written pointers, which are accepted.
- [ ] 3.2 Assert the final container argv for a `--branch` session contains exactly the computed store and Git mounts, with no value read from repository files. Verify with a CLI argv test using a fixture repository whose pointer files have been tampered with (launch refused) and one with untouched pointers (launch argv as computed).

## 4. Hardened host jj and tools

- [ ] 4.1 Add `host_tools.resolve`/`run`, and switch `oauth_refresh`, `copilot_auth`, `agent_proxy.resolve_key`, `config_agents` (`security`), `git_identity` and `doctor` to them. Verify with tests on absolute argv[0] and neutral cwd per call site, plus the fake-`claude` cwd test from the `host-command-execution` "Token refresh in a project with hostile settings" scenario.
- [ ] 4.2 Add the jj overrides to every `_jj` call, and resolve `jj` through `host_tools`. Verify with argv tests for every host jj command.
- [ ] 4.3 Add config-id records, the checks before setup and finalization, and `--accept-jj-config`. Verify with tests for first use, unchanged, a changed repo config-id, a changed workspace config-id, and acceptance.
- [ ] 4.4 Add the `doctor` jj version check. Verify with tests for versions below and at the threshold from task 1.1.

## 5. Workspace lease

- [ ] 5.1 Add the workspace lease with the orphaned-container check (design Decision 5). Verify with tests for:
  - a concurrent launch on the same bookmark (refused, naming the session);
  - a `SIGKILL`ed CLI whose container is still running (refused, naming the container);
  - a clean previous exit (acquired);
  - lease release on every finalize path, including session failure and `--keep-workspace`.

## 6. Docs and integration

- [ ] 6.1 Update the docs and repo guidance:
  - `docs/usage.md`: `--branch` requires jj; the migration hint; one session per bookmark; recovery for refused pointers and config-ids;
  - `docs/security.md`: the host execution model, and what remains for colocated `.git`;
  - `README.md` and `CLAUDE.md`: the `--branch` description;
  - ROADMAP: drop the Git-worktree note;
  - TODO.md: remove findings #2 and #9.

  Verify that `uv run pytest -q` and `uv run python -m compileall -q src tests` pass, and that `scripts/e2e-jj-workflow.sh` passes on Docker and Apple `container`.
