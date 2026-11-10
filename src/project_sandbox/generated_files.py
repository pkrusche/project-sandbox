"""Descriptor-relative I/O for generated files in untrusted repositories.

Never resolve a managed path before passing it here. Each directory component
is opened with O_NOFOLLOW; all mutations use the resulting directory descriptor.
Output leaves are replaced, never truncated, so symlinks and hard links cannot
redirect writes or chmods to another file.
"""

import os
import shutil
import stat
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TextIO


@contextmanager
def directory(path: Path, *, create: bool = False) -> Iterator[int]:
    """Open a directory without following links, optionally creating parents."""
    path = path.absolute()
    # macOS provides these system-owned aliases. Canonicalize only this fixed
    # prefix, never a repository-controlled component below it.
    if sys.platform == "darwin" and path.parts[1:2] in (("tmp",), ("var",), ("etc",)):
        path = Path("/private").joinpath(*path.parts[1:])
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            if create:
                try:
                    os.mkdir(component, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def ensure_dir(path: Path) -> Path:
    with directory(path, create=True):
        pass
    return path


def validate_dir(path: Path) -> None:
    """Check existing components without creating missing directories."""
    try:
        with directory(path):
            pass
    except FileNotFoundError:
        pass


@contextmanager
def _temporary(parent: int, *, mode: int) -> Iterator[tuple[int, str]]:
    name = f".project-sandbox-{uuid.uuid4().hex}.tmp"
    fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        mode,
        dir_fd=parent,
    )
    try:
        yield fd, name
    finally:
        os.close(fd)
        try:
            os.unlink(name, dir_fd=parent)
        except FileNotFoundError:
            pass


def write_bytes(path: Path, data: bytes, *, mode: int = 0o644) -> None:
    with directory(path.parent) as parent, _temporary(parent, mode=0o600) as (fd, name):
        with os.fdopen(os.dup(fd), "wb") as handle:
            handle.write(data)
        os.fchmod(fd, mode)
        os.replace(name, path.name, src_dir_fd=parent, dst_dir_fd=parent)


def write_text(
    path: Path, text: str, *, encoding: str = "utf-8", mode: int = 0o644
) -> None:
    write_bytes(path, text.encode(encoding), mode=mode)


@contextmanager
def open_log(path: Path) -> Iterator[TextIO]:
    """Publish a fresh private log, then stream through its open descriptor."""
    with directory(path.parent) as parent, _temporary(parent, mode=0o600) as (fd, name):
        os.replace(name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        with os.fdopen(os.dup(fd), "w", encoding="utf-8") as handle:
            yield handle


def read_text(path: Path, *, encoding: str = "utf-8", errors: str = "strict") -> str:
    with directory(path.parent) as parent:
        fd = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        with os.fdopen(fd, "r", encoding=encoding, errors=errors) as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError(f"Expected a regular generated file: {path}")
            return handle.read()


def remove(path: Path, *, recursive: bool = False) -> None:
    """Remove an entry without traversing links, including during tree cleanup."""
    try:
        with directory(path.parent) as parent:
            info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if recursive and stat.S_ISDIR(info.st_mode):
                if not shutil.rmtree.avoids_symlink_attacks:
                    raise RuntimeError(
                        "Safe directory cleanup is unavailable on this platform"
                    )
                shutil.rmtree(path.name, dir_fd=parent)
            else:
                os.unlink(path.name, dir_fd=parent)
    except FileNotFoundError:
        pass


def remove_matching(directory_path: Path, pattern: str, *, keep: set[str]) -> None:
    """Remove stale generated files using names from the verified directory."""
    with directory(directory_path) as parent:
        for name in os.listdir(parent):
            if fnmatchcase(name, pattern) and name not in keep:
                try:
                    os.unlink(name, dir_fd=parent)
                except FileNotFoundError:
                    pass


def symlink(path: Path, target: Path) -> None:
    """Atomically install an intentional generated symlink without traversal."""
    with directory(path.parent) as parent:
        name = f".project-sandbox-{uuid.uuid4().hex}.tmp"
        os.symlink(target, name, dir_fd=parent)
        try:
            os.replace(name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            try:
                os.unlink(name, dir_fd=parent)
            except FileNotFoundError:
                pass
