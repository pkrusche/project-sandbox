# Design

## Context

`host_locks` has two functions. `lock_path(kind, target)` builds
`<tempdir>/project-sandbox-<kind>-<sha256(resolved target)[:16]>.lock`.
`path_lock()` opens that path with `O_CREAT | O_NOFOLLOW` and holds an exclusive
`flock`. Seven call sites use it (`build`, `image`, `jj-workspace`,
`git-teardown`, `egress-policy`, and the two `*-session-*` workspace leases),
with documented ordering: context locks before the image lock, and the policy
lock before a sidecar runtime lock.

The other private state already has a safe way to create directories:
`mount_storage.directory(path, root=..., create=True)` walks from the
filesystem root with `O_NOFOLLOW`, creates missing components `0700` whatever
the umask, checks that the root is owned by the user and private, and yields an
open directory fd. `session_inputs` and `vcs_access.new_mask_dir()` both use it.

## Goals / Non-Goals

**Goals:**
- Only the invoking user can create, open or `flock` a lock file.
- Leave lock semantics and ordering unchanged; move only the storage.

**Non-Goals:**
- Cross-user serialization of a shared Docker store.
- Cleaning up stale lock files from earlier versions in `$TMPDIR`.
- `session_ca`'s fd-based exclusivity locks, which are already private.

## Decisions

### D1: Store locks in `<state root>/locks`, not `$XDG_RUNTIME_DIR` or a group directory

`$XDG_RUNTIME_DIR` is the conventional place for locks on Linux, but macOS (the
main platform for Apple `container` and Colima) has no equivalent, so it would
need a second code path. A setgid group directory under `/tmp` would keep
cross-user serialization, but that only matters for the `image` lock, which
does not work across users today (it is created `0600`), and it would need
install-time group setup. The state root is already private, already used by
every CLI run, and the same on both platforms.

The `locks` subtree is a sibling of `sessions`, `session-inputs`, `mounts`,
`egress` and `vcs-masks`. It is not in `colima_mounts.required_state_dirs()`,
so hardened profiles never export it; the `colima-hardened-profile` spec already
requires that.

### D2: Open lock files relative to the validated directory fd

`path_lock()` enters `mount_storage.directory(locks_dir, root=locks_dir,
create=True)` and opens the lock name with `dir_fd=` that fd (still
`O_RDWR | O_CREAT | O_NOFOLLOW`, `0600`). This closes the gap between
validating the directory and opening the file: a component swapped after
validation cannot redirect the open. The alternative, validating and then
opening the full path, leaves that gap open.

`lock_path()` stays a pure function returning the full path, used for error
messages and tests.

### D3: No fallback location

If the locks directory is unsafe, the run fails with the path in the error
(spec: "Unsafe lock storage is refused"). Falling back to `$TMPDIR` would bring
back exactly the exposure this change removes, and would split serialization
between two locations depending on the environment.

### D4: Keep names and keys

Same `project-sandbox-<kind>-<hash>.lock` file names inside the new directory.
Keeping them makes old and new locations easy to compare during debugging, and
keeps the hashing tests valid.

## Risks / Trade-offs

- [A symlinked component in `$HOME` or `$XDG_STATE_HOME` now also blocks lock
  creation] → Every CLI run already stages session inputs through the same
  no-follow walk, so this adds no new failure for a working setup. The error
  names the path; set `XDG_STATE_HOME` to its resolved location.
- [Old and new versions running at the same time don't serialize against each
  other] → Only during an upgrade with sessions still running. Documented in
  the changelog: finish or stop running sessions before upgrading.
- [State root on a network filesystem where `flock` is advisory-only or local
  to the client] → Same exposure as session inputs today; not a supported
  configuration.
- [A different `XDG_STATE_HOME` per process gives separate lock namespaces] →
  Same as for session records. Documented in the security note.

## Migration Plan

No data migration: locks are transient. Existing
`$TMPDIR/project-sandbox-*.lock` files are ignored and disappear with normal
temp cleanup. To roll back, revert `lock_path()`; nothing else depends on the
new location.
