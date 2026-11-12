"""Private host storage for implicit mounts.

This module is also installed as a standalone devcontainer initialization helper.
Keep it standard-library-only: reopening a container must not import the project.
"""

import argparse
import hashlib
import os
import stat
import sys
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from pathlib import Path


def storage_root() -> Path:
    configured = os.environ.get("XDG_STATE_HOME", "")
    base = (
        Path(configured)
        if configured and Path(configured).is_absolute()
        else Path.home() / ".local/state"
    )
    return canonical_path(base / "project-sandbox/mounts")


def canonical_path(path: Path) -> Path:
    """Normalize trusted system aliases without resolving managed components."""
    path = path.absolute()
    if ".." in path.parts:
        raise ValueError(
            f"Parent traversal is not allowed in mount storage paths: {path}"
        )
    if sys.platform == "darwin" and path.parts[1:2] in (("tmp",), ("var",), ("etc",)):
        path = Path("/private").joinpath(*path.parts[1:])
    return path


def ensure_private_dirs(path: Path) -> Path:
    """``mkdir -p`` that creates every missing component ``0700``.

    ``Path.mkdir(parents=True, mode=0o700)`` applies ``mode`` to the leaf
    only; missing parents get the umask default, which is group-writable
    under the common ``umask 002``. Such an ancestor of private mount
    storage is then (rightly) refused by :func:`directory`. Existing
    directories are left untouched.
    """
    missing: list[Path] = []
    current = path.absolute()
    while not current.is_dir():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for component in reversed(missing):
        try:
            component.mkdir(mode=0o700)
        except FileExistsError:
            continue
        # A restrictive umask must not remove the owner's own access.
        os.chmod(component, 0o700)
    return path


def project_dir(project: Path, *, root: Path | None = None) -> Path:
    digest = hashlib.sha256(os.fsencode(project.resolve())).hexdigest()
    return (root if root is not None else storage_root()) / "projects" / digest


def history_paths(project: Path, *, root: Path | None = None) -> tuple[Path, Path]:
    history = project_dir(project, root=root) / "history"
    return history / "shell", history / "claude_projects"


def mask_path(project: Path, token: str, *, root: Path | None = None) -> Path:
    if not token or not all(c.isascii() and (c.isalnum() or c in "-_") for c in token):
        raise ValueError("Invalid private mask identifier")
    return project_dir(project, root=root) / "masks" / token / "empty"


def check_exposure(root: Path, sources: Sequence[Path]) -> None:
    """Refuse storage inside a build context or reachable through another mount."""
    root = canonical_path(root)
    # Check both spellings so a planted symlink cannot hide an overlap.
    for candidate in (root, root.resolve()):
        for source in sources:
            source = source.resolve()
            if (
                candidate == source
                or candidate in source.parents
                or source in candidate.parents
            ):
                raise ValueError(
                    f"Private mount storage {root} overlaps exposed host path {source}"
                )
    if any(c in str(root) for c in ",\n\r\0"):
        raise ValueError(
            f"Private mount storage path cannot be encoded in a bind mount: {root}"
        )


def _check_directory(fd: int, path: Path, *, private: bool, mask: bool = False) -> None:
    info = os.fstat(fd)
    mode = stat.S_IMODE(info.st_mode)
    if private:
        expected = 0o755 if mask else 0o700
        if info.st_uid != os.getuid() or mode != expected:
            raise ValueError(
                f"Unsafe private mount directory {path}: expected owner {os.getuid()} and mode {expected:04o}"
            )
    else:
        # A root-owned sticky temporary directory is a safe ancestor: other
        # users cannot rename our owned entry there. Never accept a user-owned
        # group/world-writable ancestor, even if it carries the sticky bit.
        sticky_root = info.st_uid == 0 and bool(mode & stat.S_ISVTX)
        if info.st_uid not in (0, os.getuid()) or (mode & 0o022 and not sticky_root):
            raise ValueError(f"Unsafe ancestor of private mount storage: {path}")


@contextmanager
def directory(
    path: Path, *, root: Path, create: bool = False, mask: bool = False
) -> Generator[int, None, None]:
    """Walk, validate and optionally create directories without following links."""
    path, root = canonical_path(path), canonical_path(root)
    if not path.is_relative_to(root):
        raise ValueError(f"Mount directory is outside private storage: {path}")
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    current = Path(path.anchor)
    try:
        _check_directory(fd, current, private=current == root)
        for component in path.parts[1:]:
            current /= component
            created = False
            mode = 0o755 if mask and current == path else 0o700
            if create:
                try:
                    os.mkdir(
                        component,
                        mode=mode,
                        dir_fd=fd,
                    )
                    created = True
                except FileExistsError:
                    pass
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = child
            # Set exact permissions only on our newly created directory. In
            # particular, a restrictive umask must not make a mask inaccessible
            # to the container's agent UID. Never repair a pre-existing entry.
            if created and os.fstat(fd).st_uid == os.getuid():
                os.fchmod(fd, mode)
            _check_directory(
                fd,
                current,
                private=current.is_relative_to(root),
                mask=mask and current == path,
            )
        if mask and os.listdir(fd):
            raise ValueError(f"Private workspace mask is not empty: {path}")
        yield fd
    finally:
        os.close(fd)


def prepare_history(project: Path, *, root: Path | None = None) -> tuple[Path, Path]:
    root = root if root is not None else storage_root()
    check_exposure(root, [project])
    paths = history_paths(project, root=root)
    for path in paths:
        with directory(path, root=root, create=True):
            pass
    # Do not inspect, rewrite, or chmod history contents. They are writable by
    # the agent and may contain links; the host only manages their parent roots.
    return paths


def prepare_mask(project: Path, token: str, *, root: Path | None = None) -> Path:
    root = root if root is not None else storage_root()
    check_exposure(root, [project])
    path = mask_path(project, token, root=root)
    with directory(path, root=root, create=True, mask=True):
        pass
    return path


def cleanup_mask(path: Path, *, root: Path) -> None:
    """Remove only an empty session mask; never recursively delete state."""
    try:
        with directory(path.parent, root=root) as parent:
            os.rmdir(path.name, dir_fd=parent)
        with directory(path.parent.parent, root=root) as parent:
            os.rmdir(path.parent.name, dir_fd=parent)
    except FileNotFoundError:
        pass


def _prepare_credential_dir(path: Path) -> None:
    # Preserve devcontainers' empty-credential-directory recovery. Its staging
    # root is separate from history storage; never read or copy credentials.
    path = canonical_path(path)
    with directory(path, root=path.parent.parent, create=True):
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate private devcontainer mount sources"
    )
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--source", action="append", default=[], type=Path)
    parser.add_argument("--credential-dir", action="append", default=[], type=Path)
    args = parser.parse_args(argv)
    try:
        if args.workspace.resolve() != canonical_path(args.project):
            raise ValueError(
                "Workspace moved or changed; regenerate its devcontainer configuration"
            )
        check_exposure(args.root, [args.project, *args.source])
        # CLI session inputs are a separate private sibling tree. Reopening a
        # persisted devcontainer must not expose it through a relocated mount.
        check_exposure(
            args.root.parent / "session-inputs", [args.project, *args.source]
        )
        for trusted in (
            Path(sys.executable).resolve(),
            Path(sys.base_prefix).resolve(),
        ):
            check_exposure(trusted, [args.project, *args.source])
        prepare_history(args.project, root=args.root)
        prepare_mask(args.project, "devcontainer", root=args.root)
        for path in args.credential_dir:
            _prepare_credential_dir(path)
    except (OSError, ValueError) as exc:
        parser.exit(
            1,
            f"Cannot prepare private container mounts: {exc}\nRegenerate the devcontainer with project-sandbox after correcting the path.\n",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
