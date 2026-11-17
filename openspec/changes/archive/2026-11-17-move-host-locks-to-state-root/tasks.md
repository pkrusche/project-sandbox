# Tasks

## 1. Move lock storage to the private state root

- [x] 1.1 Add `host_locks.locks_root()` returning `<state root>/locks`, using the same `XDG_STATE_HOME` rule as the other state subtrees (absolute values only, else `~/.local/state`), and make `lock_path()` return names under it. Verify: unit test asserts the parent is `<XDG_STATE_HOME>/project-sandbox/locks` and the default when the variable is unset or relative.
- [x] 1.2 In `path_lock()`, create and validate the directory with `mount_storage.directory(locks_root(), root=locks_root(), create=True)` and open the lock name with `dir_fd=` the yielded fd (`O_RDWR | O_CREAT | O_NOFOLLOW`, `0600`). Keep the existing `SystemExit` message shape, naming the path. Verify: unit tests for directory created `0700` under umask `022`, lock file `0600`, symlinked locks directory refused, group-writable (`0770`) locks directory refused and left unchanged, and no `project-sandbox-*.lock` created in `tempfile.gettempdir()`.
- [x] 1.3 Add an autouse fixture in `tests/conftest.py` that patches `host_locks.locks_root` to `tmp_path / "locks"`, next to `_private_mount_storage`. Replace the `tempfile.tempdir` patch and its comment in `tests/test_build_concurrency.py`, and update `test_lock_lives_outside_the_project` in `tests/test_host_locks.py` to assert the new parent. Verify: `uv run pytest -q tests/test_host_locks.py tests/test_build_concurrency.py` passes, and a full `uv run pytest -q` leaves `~/.local/state/project-sandbox/locks` untouched (check its mtime before and after).
- [x] 1.4 Update the `host_locks` module docstring to describe the private location and why the temp dir was dropped. Verify: `uv run python -m compileall src tests` and `scripts/check-ruff.sh` pass.

## 2. Keep locks out of every sandbox

- [x] 2.1 Add a regression test that `colima_mounts.required_state_dirs()` does not contain `locks_root()` or any parent of it other than paths it already lists, and that `colima_profile.init_mounts()` for a sample root does not export it. Verify: test passes in `tests/test_colima_mounts.py`.
- [x] 2.2 Add a test that `implicit_mounts.validate_sources` (or the mount validation used for sandbox launches) rejects a source inside the locks directory, or record in the test why the state root is already rejected as a mount source. Verify: test passes in `tests/test_cli.py` or the existing mount validation test module.
- [x] 2.3 Refuse a lock name that is not a regular file owned by the user (a FIFO used to be rejected only by an incidental `fdopen` seek error), and keep `scripts/security_e2e/test_host.py` working under the new layout: give each case a private `XDG_STATE_HOME` (cases were order-dependent and would otherwise use the developer's real state root) and turn the private-directory/regular-inode xfail into assertions. Verify: `uv run pytest tests/test_host_locks.py` and each `scripts/security_e2e/test_host.py` case run alone with `--security-runtime host` pass.

## 3. Documentation and changelog

- [x] 3.1 Add a short "Host locks" paragraph to `docs/security.md` (near "Private session inputs"): location, `0700`/`0600`, never mounted or exported, refused rather than relocated when unsafe, and that a different `XDG_STATE_HOME` per process means separate lock namespaces. Verify: section renders and the anchor link from `CHANGELOG.md` resolves.
- [x] 3.2 Add a `CHANGELOG.md` entry under Unreleased > Changed: host locks moved from the system temp dir to `$XDG_STATE_HOME/project-sandbox/locks`; finish or stop running sessions before upgrading, since old and new versions don't serialize against each other; stale `$TMPDIR/project-sandbox-*.lock` files are harmless. Verify: entry present.
- [x] 3.3 Remove the "In progress" host-lock item from `TODO.md`. Verify: `grep -n "host-locks\|move-host-locks" TODO.md` returns nothing.

## 4. Integration check

- [x] 4.1 Run `uv run pytest -q` and one real concurrent launch pair against the same project (two `uv run project-sandbox --build-only <project> <image>` invocations started together). Verify: the second waits on the first, lock files appear only under the state root, and none appear in `$TMPDIR`.
  - Partial (2026-11-16, no container runtime in the dev environment): full suite passes with the real locks directory untouched, and two real interpreters contending on the `build` lock via the unpatched `locks_root()` serialized (second waited ~1.5 s) with the lock at `$XDG_STATE_HOME/project-sandbox/locks` (`0700`/`0600`) and none in `$TMPDIR`. The `--build-only` pair against a real runtime is still to do.
  - Added `scripts/e2e-host-locks.sh` (suite `host-locks` in `run-e2e-tests.sh`), which covers this with real CLI processes. It passes with `--runtime chroot`, and against the pre-change code it reports 5 BAD results (checks 1, 4, 5, 6). Remaining: run it on a container runtime (`scripts/e2e-host-locks.sh --runtime docker|apple-container`), then tick this task.

## Workflow follow-up

- Archive the change with `/opsx:archive` after merge, creating `openspec/specs/host-locks/spec.md`.
