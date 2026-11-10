"""Host-side advisory locks that serialize concurrent project-sandbox runs.

Several host operations mutate state shared by every run against one project: a
jj repository's store, a git repository's refs and worktree registry, and the
generated build context's image/cache pair. Each of those gets an exclusive
``flock`` keyed by the path it protects, so a second run waits instead of
interleaving.

The lock files live in ``$XDG_STATE_HOME/project-sandbox/locks/``, a verified
private directory outside every project, so they are neither part of a build
context nor reachable from a sandbox, and no other local user can plant or
hold them. Opening a lock with ``O_NOFOLLOW`` and without ``O_TRUNC``, then
checking with ``fstat`` that it is a regular file owned by the caller, refuses a
link, FIFO or foreign file left at that name instead of waiting on it. Lock
files are never unlinked: removing one under contention would let two holders
lock different inodes.
"""

import fcntl
import hashlib
import os
import stat
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from . import paths

LOCK_DIR = "locks"


def lock_path(kind: str, target: Path) -> Path:
    """Return the lock file protecting ``target`` for ``kind`` of work.

    The name is keyed by the *resolved* target so every process serializes on
    one file however it spelled the path (symlinked repo root, relative
    argument, ``/tmp`` vs ``/private/tmp`` on macOS).
    """
    key = hashlib.sha256(str(target.resolve()).encode()).hexdigest()[:16]
    return paths.state_root_path() / LOCK_DIR / f"{kind}-{key}.lock"


class LockBusy(Exception):
    """Raised by ``path_lock(..., blocking=False)`` when another process holds it."""


@contextmanager
def path_lock(
    kind: str, target: Path, *, blocking: bool = True
) -> Generator[None, None, None]:
    """Hold an exclusive lock on ``target`` for the duration of the block.

    With ``blocking=False`` the lock is a lease: ``LockBusy`` is raised at once
    when another process holds it, instead of waiting.
    """
    path = lock_path(kind, target)
    with paths.open_state_root() as root, root.subdir(LOCK_DIR) as locks:
        try:
            fd = paths.open_lock_file(locks, path.name)
        except OSError as exc:
            raise SystemExit(
                f"Could not open the {kind} lock at {path}: {exc.strerror}. Remove "
                "it if it is not a regular file owned by you, then retry."
            ) from exc
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != paths._uid():
        os.close(fd)
        raise SystemExit(
            f"Refusing the {kind} lock at {path}: it is not a regular file owned "
            "by you. Remove it, then retry."
        )
    with os.fdopen(fd, "r+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise LockBusy(kind) from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
