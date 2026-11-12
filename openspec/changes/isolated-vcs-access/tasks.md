# Tasks

## 1. Prerequisite

- [ ] 1.1 Confirm `harden-host-command-execution` has landed and that `host_tools.git`/`jj` apply the overrides this design relies on (Decision 5). Verify by running its marker tests (`uv run pytest -q -k host_tools`) before starting group 2.

## 2. Mode selection and planning

- [ ] 2.1 Add `isolated` to `vcs_access.MODES` and plan the session (design Decision 1): backend choice from `.jj`/`.git`, refusal without a repository and on chroot, generated `sandbox/<session-id>` name without `--branch`, existing `--branch-start-at`/`--worktree-dir`/`--keep-workspace` rules. Verify with CLI tests for each case, with and without `--dry-run`, asserting that refused cases create nothing and that dry-run prints mode, backend, branch/bookmark and session directory.
- [ ] 2.2 Document the mode in `docs/vcs-access.md` (what is mounted, what the host reads at the end, that commit series are not kept, the parked pointer and locked worktree, manual recovery) and add it to `docs/usage.md` and `docs/security.md`. Verify the documented commands against `--dry-run` output.

## 3. Git backend

- [ ] 3.1 Implement isolated setup in `worktree.py` (design Decision 3): worktree add, lock, park the pointer in `P/host-git`, private repository with one base commit through the helper, move to `P/git`, placeholder, full cleanup on failure. Verify with tests on a fixture repository: after setup the worktree is locked, `W/.git` is an empty directory, `P/git` has exactly one commit with `W`'s files and a clean status, and a failure injected after each step leaves no worktree, branch, `P` or lock.
- [ ] 3.2 Implement capture (design Decision 6): the layout checks, pointer restore, nested-repository refusal, `add -A` / `commit --no-verify` through the helper, unlock, and removal unless kept or failed. Verify with tests that modified, added and deleted files land in one commit; an unchanged session adds no commit; a symlink is recorded as a link; a planted `P/git/hooks/pre-commit` and a main-repository hook both stay unrun; a nested `sub/.git` or a replaced placeholder keeps the worktree and prints the paths; the agent's private commit messages are absent from the branch.
- [ ] 3.3 Implement resume and interrupted-session recovery (design Decision 7). Verify with tests for a kept session resumed, a CLI killed with the pointer parked, `W` without `P`, `P` without `W`, and a worktree left by a `full` session.

## 4. jj backend

- [ ] 4.1 Port isolated setup from the `sec-fixing-claude` branch's `jj_workspace.py` (`_create`, `_build_private_repository`, `metadata_mounts`) onto the current workspace code, using the helper. Verify with tests that the host workspace is clean after setup, the private repository has one base commit, and a failure at each step leaves no workspace, bookmark or `P`.
- [ ] 4.2 Port capture and resume (branch Decision 3 and 6) plus the nested-repository refusal. Verify with tests that swap the metadata as the branch's spike did: one commit with the changes, no agent message, a symlink stored as a link, a size-refused file reported with the workspace kept, and resume of a kept session.

## 5. Session wiring

- [ ] 5.1 Assemble the isolated mounts in `cli.py` (design Decision 2): only `W` and the private directories, no `repo_store_mount`/`git_backend_mount`/main `.git` bind, conflicts with `--mount` refused, and the metadata-visibility wait for new `P` directories. Verify with argv tests on Docker, Podman and Apple for a Git, a non-colocated jj and a colocated jj fixture that no mount source lies inside the main `.git`/`.jj`, and that a planted `W/.jj/repo` or store `git_target` does not change any mount.
- [ ] 5.2 Add `safe.directory /workspace` to the entrypoint's identity provisioning for isolated sessions, and route finalize to the isolated capture. Verify with a rendered-entrypoint test and a CLI test that a killed container still reaches capture.
- [ ] 5.3 Report the mode in output and the JSON summary (design Decision 8), and print the devcontainer note. Verify with CLI output and summary tests that pin `workspace_path`, `bookmark`, `session_id`, `exit_code` and the new `vcs_access`.

## 6. End-to-end

- [ ] 6.1 Add `scripts/e2e-isolated-vcs.sh` and its suite in `scripts/run-e2e-tests.sh` (plus `e2e-cleanup.py` name patterns): for a Git and a jj project, a session edits, commits with `git` (and `jj`), plants `.git/hooks/pre-commit` and `.git/config` changes, writes `/workspace/.jj/repo`, and exits; the script asserts one result commit, unchanged main hooks/config, no planted mount on resume, and a killed-container variant. Verify the suite passes on Docker, Podman and Apple `container`, and record any runtime that exposes host metadata.
- [ ] 6.2 Update CHANGELOG (Added), remove this change's row from TODO.md's "Security proposals" table, and update ROADMAP ("VCS defaults and isolated host capture": isolated mode now exists; restricted default and the other modes' hardening remain). Verify `uv run pytest -q`, `uv run ruff check src scripts tests` and `uv run python -m compileall -q src tests` pass.

## Workflow follow-up

- Archive with `/opsx:archive` after implementation, creating `openspec/specs/isolated-vcs-sessions/`.
- Related changes: `harden-host-command-execution` (prerequisite) and `derive-vcs-mounts-from-host` (pointer-derived mounts in the other modes).
- project-sandbox-tui can expose `--vcs-access isolated` once it ships.
