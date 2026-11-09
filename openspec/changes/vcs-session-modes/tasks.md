# Tasks

## 1. Runtime spikes

- [x] 1.1 Repeat the host spike with real mounts on Docker, rootless Podman and Apple `container`: bind `W` at `/workspace` and the private `jj` and `git` directories over `/workspace/.jj` and `/workspace/.git`. From inside, try to rename, remove and unmount both mount points, and search for the host `.jj` by any other path. Run `jj` and `git` in the container, then snapshot on the host. Verify by recording the results per runtime in design.md's Context, and refuse isolated mode on any runtime that exposes the host `.jj`.
- [ ] 1.2 Time setup (workspace add plus the base commit) and the final snapshot on the largest available jj project. Verify by recording the timings in design.md's Risks.

## 2. Mode selection

- [ ] 2.1 Add `--vcs {isolated,shared}` and the per-project default table (design Decision 4), including the Git-only and no-repository errors, `--branch` with `--vcs shared`, and the generated `sandbox/<session-id>` bookmark. Show mode, bookmark, start revision and session directory in `--dry-run`. Verify with CLI tests for every cell of the table, with and without `--dry-run`, asserting that failing cases create nothing.
- [ ] 2.2 Remove the Git worktree backend: `worktree.py`, `_wait_for_worktree_metadata`, the Git finalize dispatch, `tests/test_worktree.py` and `scripts/e2e-git-workflow.sh` with its entries in `scripts/run-e2e-tests.sh`. Verify that `uv run pytest -q` passes and that `scripts/run-e2e-tests.sh --help` lists no Git workflow suite.
- [ ] 2.3 Document the modes, the default, the generated bookmark and the Git-only hints in `docs/usage.md`, and update the `--branch` notes in `README.md` and `CLAUDE.md`. Verify the documented commands against `--dry-run` output.

## 3. Isolated sessions

- [ ] 3.1 Implement setup (design Decision 2) in `jj_workspace.py`: workspace add, the private repository with its base commit, the placeholder, and cleanup when any step fails. Verify with tests on a fixture repository: the host workspace is clean after setup; the private repository has exactly one base commit; a failure injected at each step leaves no workspace and no directories.
- [ ] 3.2 Delete `repo_store_mount` and `git_backend_mount`, and mount only `W` and the two private directories. Verify with argv tests that no mount source lies in the main repository's `.jj` or `.git`, for colocated and non-colocated fixtures, on each runtime.
- [ ] 3.3 Implement finalization on host metadata (design Decision 3), including the report of files jj refused for size. Verify with tests that swap the metadata as the spike did: modified, added and deleted files land in one commit; a symlink is recorded as a link; an unchanged session creates no non-empty commit; an agent commit message is absent from the main repository; a refused large file is reported and the session directory kept.
- [ ] 3.4 Implement resume and the mismatched-leftover refusals (design Decision 6), and the refusal of workspaces kept by older versions. Verify with tests for a kept session resumed, `W` without `P`, `P` without `W`, and an old-format workspace.
- [ ] 3.4a Add `--share PATH[:rw]` (design Decision 7): parsing, the path checks, the placeholder, the exclude entry, the mount and the list of writable shares in the launch output and `--dry-run`. Verify with CLI tests for every scenario of the two sharing requirements, an argv test on mount order and mode, and a snapshot test that neither a read-only nor a writable share adds anything to the result commit.
- [ ] 3.5 Add `scripts/e2e-isolated-vcs.sh` (replacing `scripts/e2e-jj-workflow.sh`): a session edits and commits with `jj` and `git`, plants `.git/hooks/pre-commit`, reads a `--share` folder and fails to write it, and exits; the script asserts the single commit in the main repository and the unchanged main `.git/hooks` and `.git/config`. Add a variant that kills the container. Verify on Docker and Apple `container`.
- [ ] 3.6 Describe isolated mode in `docs/security.md`: what is mounted, what the host reads at the end, that commit series are not kept, and what a writable share bypasses. Document `--share` in `docs/usage.md`. Verify the text against the argv tests of 3.2.

## 4. Leases and shared mode

- [ ] 4.1 Add the isolated workspace lease and the shared-session lease, both with the orphaned-container check (design Decision 5). Verify with tests for: two launches on one bookmark; two shared launches; isolated beside shared; a `SIGKILL`ed CLI whose container still runs; release on every finalize path.
- [ ] 4.2 Print the shared-metadata warning on every shared launch, and document shared mode and its limits in `docs/usage.md` and `docs/security.md`. Verify with a CLI output test.

## 5. Integration

- [ ] 5.1 Pin the launcher contract: a test that launches with `--branch` and `--json-summary` the way project-sandbox-tui does, and asserts `workspace_path`, `bookmark`, `jj_change_id`, `pid`, `exit_code` and `session_id`. Verify that it passes.
- [ ] 5.2 Update TODO.md and ROADMAP: remove findings #2 (project-sandbox's own host operations) and #9, drop the "separate clones" roadmap item, and note what remains for shared mode. Verify that `uv run pytest -q` and `uv run python -m compileall -q src tests` pass.

## Workflow follow-up

- Update project-sandbox-tui: a shared-mode option, a config key for shared folders, and the note that commit series from isolated sessions are not kept.
