# Tasks

## 1. Spike

- [ ] 1.1 Check the hard read-only mechanisms with a throwaway repository. On Linux with Docker and Podman: a read-only bind of `.git/config` over itself inside a read-write project mount. On macOS with Apple `container`, Docker Desktop and Podman machine: `chflags uchg` on the host file, then try from the container to write, truncate, rename over, rename away and delete it. Verify by recording the result per platform and runtime in design.md Decision 2, and by naming the fallback platforms.

## 2. Read-only Git directories

- [ ] 2.1 Add read-only binds of `.git/hooks/` and `.git/info/` (created if missing) after the workspace mount, for shared sessions only. Verify with argv tests per runtime, including mount order, and with a test that isolated sessions get neither.
- [ ] 2.2 Add an opt-in container test (`PROJECT_SANDBOX_CONTAINER_TESTS=1`): writing `.git/hooks/pre-commit` and `.git/info/attributes` fails, and an existing `pre-commit` hook still runs on an in-sandbox `git commit`. Verify on Docker and Apple `container`.

## 3. Config files

- [ ] 3.1 Implement the per-session snapshot and the no-follow quarantine and restore (design Decision 3), called first in `cli.main`'s `finally` block. Verify with tests for: a modified file; a file replaced by a symlink (target untouched); a `config.worktree` or `.jj/repo/config.toml` created during the session (moved aside); changed jj config pointers; restoration on the interrupt and timeout paths.
- [ ] 3.2 Implement the hard read-only per platform from task 1.1, with flag bookkeeping in session state, clearing at teardown, stale-flag clearing at launch and in `doctor`, and the launch notice on fallback platforms. Verify with argv tests (Linux binds), tests on the flag calls with a stubbed `chflags`, and a killed-session test that the next launch clears the flag.
- [ ] 3.3 Parse quarantined configs with `git config --file … --list --null` through `host_tools`, and flag the exec-capable keys from design Decision 5. Verify with table tests covering each key family.
- [ ] 3.4 Document the protected files, the in-session failures, the quarantine files and the manual flag-clearing command in `docs/security.md` and `docs/usage.md`. Verify the documented commands against the tests of 3.1 and 3.2.

## 4. Watcher and report

- [ ] 4.1 Implement no-follow manifests with the enumeration cap and the diff kinds (added, removed, modified, changed type). Watch the `core.hooksPath` directory taken from the snapshot. Verify with tests for each scenario in the "Changes to watched paths are reported" requirement.
- [ ] 4.2 Implement the watcher thread for shared sessions (design Decision 4), writing to the session log and the record's `config_changes`. Verify with tests using a short poll interval: a change appears in the record while the session is still running; a link is never followed; the thread stops before the restore.
- [ ] 4.3 Print the report after the egress-denial summary, and add `config_changes` to the JSON summary without changing existing fields. Run the end-of-session comparison on the session directory in isolated mode. Verify with CLI tests for a shared session, an isolated session, no changes (no output), and a summary consumed the way project-sandbox-tui reads it.
- [ ] 4.4 Document the watcher, what it can miss, the report and the `config_changes` fields in `docs/security.md` and `docs/usage.md`. Verify that `uv run pytest -q` and `uv run python -m compileall -q src tests` pass.
