"""Host-side advisory locks that serialize concurrent project-sandbox runs.

Several host operations mutate state shared by every run against one project: a
jj repository's store, a git repository's refs and worktree registry, and the
generated build context's image/cache pair. Each of those gets an exclusive
``flock`` keyed by the path it protects, so a second run waits instead of
interleaving.

The lock files live in a private per-user directory under the sandbox state
root (``$XDG_STATE_HOME/project-sandbox/locks``), outside the project, so they
are neither part of a build context nor reachable from a sandbox, and the
directory is never mounted or exported to a Colima VM. They used to live in the
shared, world-writable system temp dir, where any local user could guess a name
from a project path and pre-create it (our open then failed) or ``flock`` it
(our launches then blocked forever). The directory is created ``0700`` and
validated through a no-follow walk, and lock files are opened relative to that
validated directory fd, so a swapped path component cannot redirect the open.
An unsafe directory is refused, never replaced by a shared fallback.
"""

import fcntl
import hashlib
import os
import stat
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from . import mount_storage


def locks_root() -> Path:
    """``$XDG_STATE_HOME/project-sandbox/locks`` (default ``~/.local/state/...``).

    A relative or empty ``XDG_STATE_HOME`` is invalid per the XDG spec and is
    ignored, as for the other state subtrees.
    """
    configured = os.environ.get("XDG_STATE_HOME", "")
    base = (
        Path(configured)
        if configured and Path(configured).is_absolute()
        else Path.home() / ".local/state"
    )
    return mount_storage.canonical_path(base / "project-sandbox/locks")


def lock_path(kind: str, target: Path) -> Path:
    """Return the lock file protecting ``target`` for ``kind`` of work.

    The name is keyed by the *resolved* target so every process serializes on
    one file however it spelled the path (symlinked repo root, relative
    argument, ``/tmp`` vs ``/private/tmp`` on macOS).
    """
    key = hashlib.sha256(str(target.resolve()).encode()).hexdigest()[:16]
    return locks_root() / f"project-sandbox-{kind}-{key}.lock"


_CREATE_ATTEMPTS = 8


def _open_or_create(name: str, dir_fd: int) -> int:
    """Open ``name`` in ``dir_fd``, creating it ``0600`` if it is missing.

    Two runs (or threads) often reach a fresh lock at the same instant. On
    macOS a plain ``O_CREAT`` open that races another creator of the same name
    can fail with a spurious ``ENOENT``. Splitting it into "open existing" and
    "create exclusively" gives each step a definite answer: whoever loses the
    ``O_EXCL`` race sees ``EEXIST`` and opens the winner's file. ``O_EXCL``
    also refuses a symlink at the name, like ``O_NOFOLLOW``.
    """
    for _ in range(_CREATE_ATTEMPTS):
        try:
            return os.open(name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        try:
            return os.open(
                name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=dir_fd,
            )
        except FileExistsError:
            continue  # created (or replaced) by someone else meanwhile; reopen
    raise OSError(f"lock file {name} kept appearing and disappearing")


@contextmanager
def path_lock(
    kind: str, target: Path, *, blocking: bool = True
) -> Generator[None, None, None]:
    """Hold an exclusive lock on ``target`` for the duration of the block.

    With ``blocking=False``, contention raises ``BlockingIOError`` so session
    ownership can reject a competing launch instead of queueing it.
    """
    path = lock_path(kind, target)
    try:
        with mount_storage.directory(
            path.parent, root=path.parent, create=True
        ) as dir_fd:
            fd = _open_or_create(path.name, dir_fd)
        # A FIFO or device at the lock name opens fine O_RDWR; refuse anything
        # but our own regular file before relying on it.
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            os.close(fd)
            raise ValueError("not a regular file owned by you")
    except (OSError, ValueError) as exc:
        raise SystemExit(
            f"Could not open the {kind} lock at {path}: {exc}. The lock directory "
            f"{path.parent} must be a real directory owned by you with mode 0700, "
            f"and the lock must be a regular file; fix or remove it, then retry."
        ) from exc
    with os.fdopen(fd, "r+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
