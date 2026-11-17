# Proposal

## Why

Host advisory locks (`host_locks.lock_path()`) live in the shared, world-writable
system temp dir under names any local user can predict from a project path
(`$TMPDIR/project-sandbox-<kind>-<hash>.lock`). `O_NOFOLLOW` stops symlink
redirection, but another local user can still pre-create a name, which makes our
open fail with `EACCES`, or open and `flock` it, which blocks our launches
forever. The one lock meant to span users, the per-runtime `image` lock, does not
work across users today either: the first user creates it `0600`, so the shared
directory buys no cross-user serialization in exchange for that exposure.

## What Changes

- Host lock files move from the system temp dir to a private per-user
  directory, `$XDG_STATE_HOME/project-sandbox/locks` (default
  `~/.local/state/project-sandbox/locks`). It is created `0700` and validated
  through no-follow traversal, like the other state subtrees.
- Lock names, kinds and keys (sha256 of the resolved target) are unchanged, as
  are every call site and the documented lock ordering.
- The locks directory is host-only: it is never mounted into a sandbox and never
  exported by a Colima profile. The `colima-hardened-profile` spec already
  forbids exporting locks, so its requirements do not change.
- An unsafe locks directory (a symlink, not owned by the user, or group/other
  accessible) is refused with an error naming the path, instead of falling back
  to a shared location.
- **BREAKING (operational, upgrade only):** a process still running the old
  version locks in `$TMPDIR` while a new one locks in the state root, so the two
  do not serialize against each other while both are running. Stale
  `$TMPDIR/project-sandbox-*.lock` files are left alone and are harmless.
- Not in scope: cross-user serialization of a shared Docker store (it does not
  work today and is not a supported setup), devcontainer mask directories
  (deferred to the devcontainer rework), and `session_ca`'s own locks.

## Capabilities

### New Capabilities

- `host-locks`: where host-side advisory locks that serialize concurrent
  project-sandbox runs are stored, who can reach them, and how unsafe lock
  storage is refused.

### Modified Capabilities

None. `colima-hardened-profile` already states that the profile SHALL NOT
export locks; the new location keeps that true without a requirement change.

## Impact

- Code: `src/project_sandbox/host_locks.py` (`lock_path()`, `path_lock()`),
  reusing `mount_storage.directory()` for creation and validation. Callers in
  `build_cache.py`, `egress_policy.py`, `jj_workspace.py`, `worktree.py` and
  `workspace_lease.py` are unchanged.
- Tests: `tests/test_host_locks.py` (asserts the temp-dir parent today),
  `tests/test_build_concurrency.py` (comment and isolation assume `$TMPDIR`);
  test isolation moves to `XDG_STATE_HOME`.
- Docs: `docs/security.md` gains a short note on host lock storage;
  `CHANGELOG.md` notes the move and the upgrade window.
- Platforms: macOS and Linux both use the state root; `$XDG_RUNTIME_DIR` is not
  used because macOS does not provide it.
