"""Host paths, and the verified private directories host state lives in.

Generated state (build inputs, agent configs, staged secrets, history, logs,
masks, locks) lives outside every project, in two per-user roots:

* the state root, ``$XDG_STATE_HOME/project-sandbox/`` (persistent);
* the runtime root, for per-session staging (ephemeral; see
  ``runtime_root_path``).

Every write goes through ``PrivateDir``: a directory descriptor reached by a
no-follow walk from a trusted anchor and verified with ``fstat``. Files are
created next to their final name with ``O_EXCL|O_NOFOLLOW`` and renamed into
place relative to that descriptor, so a planted or swapped-in symlink is
replaced rather than followed, and a secret is ``0600`` from its first open.
"""

import contextlib
import errno
import hashlib
import os
import re
import secrets
import shutil
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Self

STATE_DIR_NAME = "project-sandbox"
RUNTIME_DIR_ENV = "PROJECT_SANDBOX_RUNTIME_DIR"


def resolve_strict(path: str | Path) -> Path:
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"Path does not exist: {p}")
    return p.resolve()


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


# Container-side targets for the persisted history bind mounts. apple/container
# can only bind-mount *directories* — a single-file source is rejected with
# "path '<file>' is not a directory" — so bash history is persisted by mounting
# a directory and pointing HISTFILE at a file inside it, rather than mounting
# ~/.bash_history directly. The Claude projects mount is already a directory.
HISTORY_SHELL_TARGET = "/home/agent/.bash_history.d"
HISTORY_HISTFILE = HISTORY_SHELL_TARGET + "/bash_history"
HISTORY_CLAUDE_PROJECTS_TARGET = "/home/agent/.claude/projects"
WORKSPACE_SANDBOX_TARGET = "/workspace/.project-sandbox"
WORKSPACE_DEVCONTAINER_TARGET = "/workspace/.devcontainer"
WORKSPACE_CARGO_TARGET = "/workspace/target"
WORKSPACE_NODE_MODULES_TARGET = "/workspace/node_modules"


class UnsafePathError(SystemExit):
    """A host path failed verification; the message names it."""


def _uid() -> int:
    return os.geteuid()


def _fstat(fd: int) -> os.stat_result:
    # Indirection so tests can simulate a foreign owner.
    return os.fstat(fd)


def _write_all(fd: int, data: bytes) -> None:
    # Indirection so tests can observe the file's mode before any byte lands.
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _before_replace(dir_fd: int, tmp_name: str, name: str) -> None:
    """Test hook: runs after the temp file is written, before the rename."""


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def open_lock_file(directory: "PrivateDir", name: str) -> int:
    """Open (creating ``0600``) the lock file ``name``, never following a link.

    macOS can fail a concurrent ``O_CREAT`` of one new name with ENOENT while
    another process or thread creates it; the file exists by the retry.
    """
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    flags |= getattr(os, "O_CLOEXEC", 0)
    for attempt in range(10):
        try:
            return os.open(name, flags, 0o600, dir_fd=directory.fd)
        except FileNotFoundError:
            if attempt == 9:
                raise
    raise AssertionError("unreachable")


class PrivateDir:
    """An open, verified directory; every operation is relative to its fd.

    ``private`` directories must be owned by the caller and not writable by
    group or others. Non-private ones (inside a project, such as
    ``.devcontainer``) are only required to be real directories, reached
    without following a symlink.
    """

    def __init__(self, fd: int, path: Path, *, private: bool) -> None:
        self.fd = fd
        self.path = path
        self.private = private

    # -- opening -----------------------------------------------------------

    @classmethod
    def open(
        cls,
        path: Path,
        *,
        anchor: Path,
        create: bool = True,
        private: bool = True,
    ) -> "PrivateDir":
        """Walk from the trusted ``anchor`` down to ``path``, never following a link.

        ``anchor`` is opened as given (it is the caller's trusted starting
        point: a configured root's parent, or a project directory). Every
        component below it is opened with ``O_NOFOLLOW``, created ``0700`` when
        missing and ``create`` is set, and verified.
        """
        try:
            relative = Path(path).relative_to(anchor)
        except ValueError:
            raise ValueError(f"{path} is not below {anchor}") from None
        # The anchor is trusted, so canonicalize it: every path derived from it
        # is then symlink-free (macOS /tmp and /var are links) and can be
        # checked as a mount source.
        anchor = Path(os.path.realpath(anchor))
        try:
            fd = os.open(
                anchor, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
            )
        except OSError as exc:
            raise UnsafePathError(
                f"Cannot open directory {anchor}: {exc.strerror}"
            ) from None
        current = cls(fd, anchor, private=private)
        try:
            for part in relative.parts:
                child = current._open_child(part, create=create, private=private)
                current.close()
                current = child
        except BaseException:
            current.close()
            raise
        return current

    def subdir(
        self, name: str, *, create: bool = True, private: bool | None = None
    ) -> "PrivateDir":
        """Open (or create ``0700``) the child directory ``name``."""
        _check_name(name)
        return self._open_child(
            name, create=create, private=self.private if private is None else private
        )

    def _open_child(self, name: str, *, create: bool, private: bool) -> "PrivateDir":
        path = self.path / name
        created = False
        while True:
            try:
                fd = os.open(name, _DIR_FLAGS, dir_fd=self.fd)
                break
            except FileNotFoundError:
                if not create:
                    raise UnsafePathError(f"Directory does not exist: {path}") from None
                try:
                    os.mkdir(name, 0o700, dir_fd=self.fd)
                    created = True
                except FileExistsError:
                    pass  # raced with another creator; open what is there
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
                    raise UnsafePathError(
                        f"Refusing {path}: it is a symlink or not a directory"
                    ) from None
                raise UnsafePathError(f"Cannot open {path}: {exc.strerror}") from None
        child = PrivateDir(fd, path, private=private)
        try:
            if created:
                os.fchmod(fd, 0o700)
            child.verify()
        except BaseException:
            child.close()
            raise
        return child

    def verify(self) -> None:
        st = _fstat(self.fd)
        if not stat.S_ISDIR(st.st_mode):
            raise UnsafePathError(f"Refusing {self.path}: not a directory")
        if not self.private:
            return
        if st.st_uid != _uid():
            raise UnsafePathError(
                f"Refusing {self.path}: owned by uid {st.st_uid}, expected uid "
                f"{_uid()} with mode 0700"
            )
        if st.st_mode & 0o022:
            raise UnsafePathError(
                f"Refusing {self.path}: mode {stat.S_IMODE(st.st_mode):04o} is "
                "writable by group or others; expected 0700"
            )

    # -- files -------------------------------------------------------------

    def write(self, name: str, data: str | bytes, *, mode: int = 0o644) -> Path:
        """Atomically replace ``name`` with ``data``; a link at ``name`` is replaced."""
        _check_name(name)
        payload = data.encode("utf-8") if isinstance(data, str) else data
        tmp = f".{name}.{secrets.token_hex(6)}.tmp"
        fd = os.open(
            tmp,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0),
            mode,
            dir_fd=self.fd,
        )
        try:
            try:
                # The umask may only narrow the mode; make it exact (0755 scripts).
                os.fchmod(fd, mode)
                _write_all(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            _before_replace(self.fd, tmp, name)
            os.replace(tmp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=self.fd)
            except OSError:
                pass
            raise
        return self.path / name

    def write_secret(self, name: str, data: str | bytes) -> Path:
        return self.write(name, data, mode=0o600)

    def copy_from(self, source: Path, name: str, *, mode: int = 0o600) -> Path:
        """Copy the regular file ``source`` (not a link) to ``name``."""
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise UnsafePathError(f"Refusing {source}: not a regular file")
            data = handle.read()
        return self.write(name, data, mode=mode)

    def read_bytes(self, name: str) -> bytes | None:
        """Return a regular file's content, or None (missing, link, not a file)."""
        _check_name(name)
        try:
            fd = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                dir_fd=self.fd,
            )
        except OSError:
            return None
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return None
            return handle.read()

    def lstat(self, name: str) -> os.stat_result | None:
        _check_name(name)
        try:
            return os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def listdir(self) -> list[str]:
        return sorted(os.listdir(self.fd))

    def unlink(self, name: str, *, missing_ok: bool = True) -> bool:
        """Remove the file or link ``name`` (never its target)."""
        _check_name(name)
        try:
            os.unlink(name, dir_fd=self.fd)
        except FileNotFoundError:
            if not missing_ok:
                raise
            return False
        return True

    def remove(self, name: str) -> bool:
        """Remove ``name`` whatever it is; a directory tree is removed no-follow."""
        st = self.lstat(name)
        if st is None:
            return False
        if stat.S_ISDIR(st.st_mode):
            shutil.rmtree(name, dir_fd=self.fd)
        else:
            os.unlink(name, dir_fd=self.fd)
        return True

    def rmtree(self, name: str) -> bool:
        """Remove the directory tree ``name`` without following links."""
        return self.remove(name)

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - safety net
        with contextlib.suppress(OSError):
            self.close()


def _check_name(name: str) -> None:
    if not name or name in (".", "..") or "/" in name or "\0" in name:
        raise ValueError(f"Invalid file name: {name!r}")


Target = Path | PrivateDir


@contextmanager
def target_dir(target: Target) -> Iterator[PrivateDir]:
    """Yield a ``PrivateDir`` for a renderer's output directory.

    An open ``PrivateDir`` is used as is (the CLI passes session directories).
    A plain path is opened, created when missing, without following a link at
    its last component; its parent is the trusted anchor.
    """
    if isinstance(target, PrivateDir):
        yield target
        return
    path = Path(os.path.abspath(target))
    with PrivateDir.open(path, anchor=path.parent, private=False) as directory:
        yield directory


def target_path(target: Target, name: str) -> Path:
    """The path of ``name`` in ``target``, spelled the way the caller gave it."""
    base = target.path if isinstance(target, PrivateDir) else Path(target)
    return base / name


def open_in_project(project: Path, name: str, *, create: bool = True) -> PrivateDir:
    """Open ``<project>/<name>`` (e.g. ``.devcontainer``), refusing a symlink."""
    return PrivateDir.open(project / name, anchor=project, create=create, private=False)


# -- roots -------------------------------------------------------------------


def state_home(env: Mapping[str, str] | None = None) -> Path:
    """``$XDG_STATE_HOME``; a relative or empty value is ignored (XDG spec)."""
    env = os.environ if env is None else env
    configured = env.get("XDG_STATE_HOME", "")
    if configured.startswith("/"):
        return Path(configured)
    return Path.home() / ".local" / "state"


def state_root_path(env: Mapping[str, str] | None = None) -> Path:
    return state_home(env) / STATE_DIR_NAME


def open_state_root(env: Mapping[str, str] | None = None) -> PrivateDir:
    """Open (creating) ``$XDG_STATE_HOME/project-sandbox``, verified private."""
    home = state_home(env)
    home.mkdir(parents=True, exist_ok=True)
    return PrivateDir.open(home / STATE_DIR_NAME, anchor=home)


def _is_per_user_dir(value: str) -> bool:
    try:
        st = os.stat(value)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == _uid() and not st.st_mode & 0o077


def runtime_root_path(env: Mapping[str, str] | None = None) -> Path:
    """Where per-session staging lives.

    In order: ``$PROJECT_SANDBOX_RUNTIME_DIR`` (absolute; used as is), then
    ``$XDG_RUNTIME_DIR/project-sandbox`` and ``$TMPDIR/project-sandbox`` when
    that directory is per-user (owned by the caller, no group or other
    access), then ``/tmp/project-sandbox-<uid>``.
    """
    env = os.environ if env is None else env
    override = env.get(RUNTIME_DIR_ENV, "")
    if override:
        if not override.startswith("/"):
            raise UnsafePathError(
                f"{RUNTIME_DIR_ENV} must be an absolute path, got {override!r}"
            )
        return Path(override)
    for name in ("XDG_RUNTIME_DIR", "TMPDIR"):
        value = env.get(name, "")
        if value.startswith("/") and _is_per_user_dir(value):
            return Path(value) / STATE_DIR_NAME
    return Path("/tmp") / f"{STATE_DIR_NAME}-{_uid()}"


def open_runtime_root(env: Mapping[str, str] | None = None) -> PrivateDir:
    root = runtime_root_path(env)
    if root.parent == root:
        raise UnsafePathError(f"Refusing {root} as the runtime root")
    return PrivateDir.open(root, anchor=root.parent)


_KEY_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def project_key(project: Path) -> str:
    """``<name>-<hash16>`` for the resolved project path."""
    resolved = Path(project).resolve(strict=False)
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]
    name = _KEY_NAME_RE.sub("-", resolved.name).strip(".-")[:40] or "project"
    return f"{name}-{digest}"


def project_state_path(project: Path, env: Mapping[str, str] | None = None) -> Path:
    return state_root_path(env) / "projects" / project_key(project)


def open_project_state(
    project: Path, env: Mapping[str, str] | None = None
) -> PrivateDir:
    with open_state_root(env) as root, root.subdir("projects") as projects:
        return projects.subdir(project_key(project))


def walk_files(directory: PrivateDir, prefix: str = "") -> Iterator[tuple[str, str]]:
    """Yield ``(relative_path, kind)`` for entries below ``directory``, no-follow.

    ``kind`` is ``file``, ``dir``, ``link`` or ``other``.
    """
    for name in directory.listdir():
        st = directory.lstat(name)
        if st is None:
            continue
        rel = f"{prefix}{name}"
        if stat.S_ISLNK(st.st_mode):
            yield rel, "link"
        elif stat.S_ISDIR(st.st_mode):
            yield rel, "dir"
            with directory.subdir(name, create=False, private=False) as child:
                yield from walk_files(child, rel + "/")
        elif stat.S_ISREG(st.st_mode):
            yield rel, "file"
        else:
            yield rel, "other"


def validate_mount_source(source: Path, roots: tuple[Path, ...]) -> None:
    """Refuse an implicit mount source that is not a private path under a root.

    The source must be absolute, not itself a symlink, resolve under one of
    ``roots`` (resolved state roots), and be owned by the caller. (Directories
    below a root are private to the caller, so only the leaf can be a link
    someone else planted; system links above a root, such as macOS ``/var``,
    are fine.)
    """
    text = str(source)
    if not os.path.isabs(text):
        raise UnsafePathError(f"Refusing mount source {source}: not absolute")
    if os.path.islink(text):
        raise UnsafePathError(f"Refusing mount source {source}: it is a symlink")
    real = os.path.realpath(text)
    if not any(_is_below(real, str(root)) for root in roots):
        raise UnsafePathError(
            f"Refusing mount source {source}: not under a project-sandbox state root"
        )
    try:
        st = os.lstat(real)
    except OSError as exc:
        raise UnsafePathError(
            f"Refusing mount source {source}: {exc.strerror}"
        ) from None
    if st.st_uid != _uid():
        raise UnsafePathError(
            f"Refusing mount source {source}: owned by uid {st.st_uid}, not {_uid()}"
        )


def _is_below(path: str, root: str) -> bool:
    root = os.path.realpath(root)
    return path == root or path.startswith(root.rstrip("/") + "/")
