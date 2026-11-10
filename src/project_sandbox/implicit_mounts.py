"""Connect private mount storage to CLI and devcontainer mount planning."""

import hashlib
import os
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

from . import mount_options, mount_storage


def validate_sources(
    project: Path,
    *,
    workspace: Path | None = None,
    build_context: Path | None = None,
    extra_mounts: Sequence[str] = (),
    sources: Sequence[Path] = (),
) -> list[Path]:
    exposed = [project, workspace or project, *sources]
    if build_context is not None:
        exposed.append(build_context)
    for value in extra_mounts:
        # Preserve source aliases for validation when devcontainers reopen.
        exposed.append(mount_options.bind_source(value))
    mount_storage.check_exposure(mount_storage.storage_root(), exposed)
    return exposed


def initialize_command(
    project: Path, *, sources: Sequence[Path], credential_dirs: Sequence[Path]
) -> list[str]:
    """Install a standalone helper outside all exposed paths and return its argv."""
    root = mount_storage.storage_root()
    python = Path(sys.executable).resolve()
    for trusted in (python, Path(sys.base_prefix).resolve()):
        mount_storage.check_exposure(trusted, sources)
    source = Path(mount_storage.__file__).read_bytes()
    name = f"initialize-{hashlib.sha256(source).hexdigest()}.py"
    with mount_storage.directory(root / "helpers", root=root, create=True) as parent:
        temporary = f".{uuid.uuid4().hex}.tmp"
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(source)
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
    command = [
        str(python),
        "-I",
        "-S",
        str(root / "helpers" / name),
        "--root",
        str(root),
        "--project",
        str(project.resolve()),
        "--workspace",
        "${localWorkspaceFolder}",
    ]
    for path in sources:
        command.extend(("--source", str(path)))
    for path in credential_dirs:
        command.extend(("--credential-dir", str(path)))
    return command
