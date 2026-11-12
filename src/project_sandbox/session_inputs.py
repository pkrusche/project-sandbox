"""Private, exclusively owned input trees for CLI sessions."""

import os
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from . import generated_files, mount_storage


def storage_root() -> Path:
    return mount_storage.storage_root().parent / "session-inputs"


def preview_path() -> Path:
    return storage_root() / "preview"


@contextmanager
def allocate() -> Generator[Path, None, None]:
    root = storage_root()
    name = uuid.uuid4().hex
    with mount_storage.directory(root, root=root, create=True) as parent:
        # Exclusive creation: never reuse another invocation's staging tree.
        os.mkdir(name, mode=0o700, dir_fd=parent)
    path = root / name
    try:
        yield path
    finally:
        generated_files.remove(path, recursive=True)
