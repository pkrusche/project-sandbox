"""Host-side advisory locks that serialize concurrent project-sandbox runs.

Several host operations mutate state shared by every run against one project: a
jj repository's store, a git repository's refs and worktree registry, and the
generated build context's image/cache pair. Each of those gets an exclusive
``flock`` keyed by the path it protects, so a second run waits instead of
interleaving.

The lock files live in the system temp dir — outside the project, so they are
neither part of a build context nor reachable from a sandbox — which is world
writable and holds names any local user can guess from a project path. Opening
them with ``O_NOFOLLOW`` and without ``O_TRUNC`` keeps a pre-planted symlink at
one of those names from redirecting the open onto (and truncating) a file
elsewhere in the caller's home; the open fails loudly instead.
"""

import fcntl
import hashlib
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def lock_path(kind: str, target: Path) -> Path:
    """Return the lock file protecting ``target`` for ``kind`` of work.

    The name is keyed by the *resolved* target so every process serializes on
    one file however it spelled the path (symlinked repo root, relative
    argument, ``/tmp`` vs ``/private/tmp`` on macOS).
    """
    key = hashlib.sha256(str(target.resolve()).encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"project-sandbox-{kind}-{key}.lock"


@contextmanager
def path_lock(kind: str, target: Path) -> Iterator[None]:
    """Hold an exclusive lock on ``target`` for the duration of the block."""
    path = lock_path(kind, target)
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise SystemExit(
            f"Could not open the {kind} lock at {path}: {exc}. Remove it if it is "
            f"not a regular file owned by you, then retry."
        ) from exc
    with os.fdopen(fd, "r+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
