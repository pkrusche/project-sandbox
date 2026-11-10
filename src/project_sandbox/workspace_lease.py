"""Exclusive host ownership from workspace setup through session cleanup."""

import hashlib
from contextlib import ExitStack
from pathlib import Path
from typing import Self

from . import host_locks


class WorkspaceLease:
    def __init__(self, kind: str, repo: Path, ref: str, path: Path) -> None:
        self._locks = ExitStack()
        self.active = False
        # Protect both the ref (even with different workspace roots) and the
        # directory (even with aliases or another repo/backend).
        ref_key = hashlib.sha256(ref.encode()).hexdigest()
        try:
            self._locks.enter_context(
                host_locks.path_lock(f"{kind}-session-{ref_key}", repo, blocking=False)
            )
            self._locks.enter_context(
                host_locks.path_lock("workspace-session", path, blocking=False)
            )
        except BlockingIOError as exc:
            self._locks.close()
            raise SystemExit(
                f"workspace or branch/bookmark '{ref}' is already in use by "
                f"another session: {path}\n"
                "  Wait for that session to finish or use a different branch/bookmark."
            ) from exc
        except BaseException:
            self._locks.close()
            raise
        self.active = True

    def close(self) -> None:
        self.active = False
        self._locks.close()

    def __enter__(self) -> Self:
        if not self.active:
            raise RuntimeError("workspace lease has already been released")
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
