"""Per-session staging under the runtime root.

Each session renders its inputs into ``<runtime root>/staging/<session-id>/``:

    lease     flock held for the session's lifetime
    build/    rendered build context (Dockerfile, scripts, CA files)
    config/   agent configs, mounted read-only
    secrets/  staged credentials, the Copilot token, api-keys.env (0600)
    prompt/   the headless prompt
    empty/    the session's mask source

A session deletes only its own directory. A later launch deletes directories
whose lease can be taken: their owner is gone (exited without cleanup, crashed
or was killed). Creation and reaping both hold the reaper lock, so the reaper
never sees a directory before its owner holds the lease.
"""

import fcntl
import os
import re
import stat
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Self

from . import paths
from .paths import PrivateDir

STAGING_DIR = "staging"
LEASE = "lease"
REAPER_LOCK = "reaper.lock"
SUBDIRS = ("build", "config", "secrets", "prompt", "empty")

_SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _open_lock(directory: PrivateDir, name: str) -> int:
    fd = paths.open_lock_file(directory, name)
    st = os.fstat(fd)
    if not (stat.S_ISREG(st.st_mode) and st.st_uid == paths._uid()):
        os.close(fd)
        raise paths.UnsafePathError(
            f"Refusing lock {directory.path / name}: not a regular file owned by you"
        )
    return fd


@contextmanager
def _reaper_lock(runtime_root: PrivateDir) -> Generator[None, None, None]:
    fd = _open_lock(runtime_root, REAPER_LOCK)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


class SessionStaging:
    """One session's staging directory, leased until ``close``."""

    def __init__(
        self,
        session_id: str,
        staging: PrivateDir,
        root: PrivateDir,
        lease_fd: int,
        subdirs: dict[str, PrivateDir],
    ) -> None:
        self.session_id = session_id
        self._staging = staging
        self.root = root
        self._lease_fd = lease_fd
        self.dirs = subdirs

    @property
    def path(self) -> Path:
        return self.root.path

    @property
    def build(self) -> PrivateDir:
        return self.dirs["build"]

    @property
    def config(self) -> PrivateDir:
        return self.dirs["config"]

    @property
    def secrets(self) -> PrivateDir:
        return self.dirs["secrets"]

    @property
    def prompt(self) -> PrivateDir:
        return self.dirs["prompt"]

    @property
    def empty(self) -> PrivateDir:
        return self.dirs["empty"]

    @classmethod
    def create(
        cls, session_id: str, env: Mapping[str, str] | None = None
    ) -> "SessionStaging":
        if not _SESSION_ID_RE.fullmatch(session_id):
            raise ValueError(f"Invalid session id: {session_id!r}")
        with paths.open_runtime_root(env) as runtime_root:
            staging = runtime_root.subdir(STAGING_DIR)
            try:
                with _reaper_lock(runtime_root):
                    if staging.lstat(session_id) is not None:
                        raise paths.UnsafePathError(
                            f"Session staging already exists: {staging.path / session_id}"
                        )
                    root = staging.subdir(session_id)
                    lease_fd = _open_lock(root, LEASE)
                    fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BaseException:
                staging.close()
                raise
        subdirs = {name: root.subdir(name) for name in SUBDIRS}
        return cls(session_id, staging, root, lease_fd, subdirs)

    def close(self) -> None:
        """Delete this session's directory, then release the lease."""
        if self._lease_fd < 0:
            return
        for directory in self.dirs.values():
            directory.close()
        try:
            self._staging.remove(self.session_id)
        finally:
            self.root.close()
            self._staging.close()
            os.close(self._lease_fd)
            self._lease_fd = -1

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def staging_path(session_id: str, env: Mapping[str, str] | None = None) -> Path:
    """Where ``session_id``'s staging would live (dry-run; creates nothing)."""
    return paths.runtime_root_path(env) / STAGING_DIR / session_id


def reap(env: Mapping[str, str] | None = None) -> list[str]:
    """Delete staging of sessions whose owner is gone; return their ids."""
    root_path = paths.runtime_root_path(env)
    if not (root_path / STAGING_DIR).is_dir():
        return []
    reaped: list[str] = []
    with (
        paths.open_runtime_root(env) as runtime_root,
        runtime_root.subdir(STAGING_DIR) as staging,
        _reaper_lock(runtime_root),
    ):
        for name in staging.listdir():
            if not _SESSION_ID_RE.fullmatch(name):
                continue
            if _owner_alive(staging, name):
                continue
            staging.remove(name)
            reaped.append(name)
    return reaped


def _owner_alive(staging: PrivateDir, name: str) -> bool:
    st = staging.lstat(name)
    if st is None or not stat.S_ISDIR(st.st_mode):
        # Not a session directory: a stray file or link is removed as well.
        return False
    try:
        session = staging.subdir(name, create=False)
    except paths.UnsafePathError:
        return True  # leave anything we cannot verify alone
    with session:
        try:
            fd = os.open(
                LEASE,
                os.O_RDWR | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                dir_fd=session.fd,
            )
        except FileNotFoundError:
            return False  # its creator died before taking the lease
        except OSError:
            return True
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)
    return False


class SessionLayout:
    """Every host path a session uses, for a real run or a dry-run plan.

    A real run holds the open ``SessionStaging`` and the project's state
    directory, and writes only through them. A dry-run plan has neither:
    it names the paths and creates nothing.
    """

    def __init__(
        self,
        *,
        staging_dir: Path,
        project_state: Path,
        roots: tuple[Path, ...],
        session: SessionStaging | None = None,
        state: PrivateDir | None = None,
    ) -> None:
        self.staging_dir = staging_dir
        self.project_state = project_state
        self.roots = roots
        self.session = session
        self.state = state

    @classmethod
    def for_session(
        cls,
        session: SessionStaging,
        state: PrivateDir,
        env: Mapping[str, str] | None = None,
    ) -> "SessionLayout":
        return cls(
            staging_dir=session.path,
            project_state=state.path,
            roots=_resolved_roots(env),
            session=session,
            state=state,
        )

    @classmethod
    def planned(
        cls, session_id: str, project: Path, env: Mapping[str, str] | None = None
    ) -> "SessionLayout":
        return cls(
            staging_dir=staging_path(session_id, env),
            project_state=paths.project_state_path(project, env),
            roots=(),
        )

    @property
    def dry_run(self) -> bool:
        return self.session is None

    def __getattr__(self, name: str) -> Path:
        if name in SUBDIRS:
            return self.staging_dir / name
        raise AttributeError(name)

    @property
    def history_shell(self) -> Path:
        return self.project_state / "history" / "shell"

    @property
    def history_claude(self) -> Path:
        return self.project_state / "history" / "claude_projects"

    def ensure_history(self) -> tuple[Path, Path]:
        """Create the private history directories (and bash_history)."""
        assert self.state is not None
        with self.state.subdir("history") as history:
            with history.subdir("shell") as shell:
                if shell.lstat("bash_history") is None:
                    shell.write("bash_history", "", mode=0o600)
            history.subdir("claude_projects").close()
        return self.history_shell, self.history_claude

    def validate_mount_sources(self, sources: list[Path]) -> None:
        """Refuse any implicit mount source outside the private state roots."""
        for source in sources:
            paths.validate_mount_source(source, self.roots)


def _resolved_roots(env: Mapping[str, str] | None) -> tuple[Path, ...]:
    return tuple(
        Path(os.path.realpath(root))
        for root in (paths.runtime_root_path(env), paths.state_root_path(env))
    )
