"""Connect private mount storage to CLI and devcontainer mount planning."""

import hashlib
import os
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

from . import container_cli, mount_storage


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
        keys = [part.split("=", 1)[0] for part in value.split(",")]
        for aliases in (("source", "src"), ("target", "dst", "destination"), ("type",)):
            if sum(keys.count(key) for key in aliases) > 1:
                raise ValueError("Ambiguous duplicate fields in --mount")
        spec = container_cli.parse_mount(value)
        if spec.raw is not None:
            raise ValueError(
                "Private mount storage requires fully understood bind --mount options"
            )
        # Keep the original spelling for devcontainer reopen checks: a source
        # symlink can point somewhere else by the time initializeCommand runs.
        source = next(
            part.split("=", 1)[1]
            for part in value.split(",")
            if part.split("=", 1)[0] in ("source", "src")
        )
        if '"' in source or any(c in source for c in "\n\r\0"):
            raise ValueError(
                "Unsupported quoting or control characters in --mount source"
            )
        exposed.append(Path(source).absolute())
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
