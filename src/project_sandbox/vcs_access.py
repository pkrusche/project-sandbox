"""Plan runtime protection of existing Git and Jujutsu metadata.

No VCS commands are used to discover paths. Tracked files such as
.gitattributes deliberately remain outside this policy.
"""

import os
import posixpath
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .container_cli import APPLE_CONTAINER, MountSpec, Runtime, parse_mount

MODES = ("full", "read-only", "hidden")
NAMES = (".git", ".jj")


def _walk_error(error: OSError) -> None:
    raise error


@dataclass(frozen=True)
class Layout:
    workspace: Path
    roots: tuple[Path, ...]
    mounts: tuple[MountSpec, ...]
    files: frozenset[Path] = frozenset()

    def check_roots(self) -> None:
        """Reject newly introduced metadata before host-side finalization."""
        expected = set(self.roots)
        for name in NAMES:
            path = self.workspace / name
            if os.path.lexists(path) != (path in expected) or path.is_symlink():
                raise ValueError(
                    f"VCS metadata layout changed at {path}; leaving the workspace "
                    "for manual inspection without automatic capture."
                )
            if path in expected and (
                path.is_file() != (path in self.files)
                or not (path.is_file() or path.is_dir())
            ):
                raise ValueError(
                    f"VCS metadata type changed at {path}; leaving the workspace for manual inspection"
                )


def inspect(workspace: Path) -> Layout:
    workspace = workspace.resolve()
    roots = []
    mounts: dict[str, MountSpec] = {}

    def add(source: Path, target: str) -> None:
        if source.is_symlink():
            raise ValueError(
                f"Restricted VCS access does not support symlinked metadata: {source}"
            )
        source = source.resolve(strict=True)
        if source.is_file() and source.stat().st_nlink > 1:
            raise ValueError(
                f"Restricted VCS access does not support hard-linked metadata: {source}"
            )
        if source == workspace or source in workspace.parents:
            raise ValueError(f"Metadata points at a workspace ancestor: {source}")
        target = posixpath.normpath(target)
        if not target.startswith("/") or target == "/":
            raise ValueError(f"Invalid VCS metadata mount target: {target}")
        if target == "/workspace" or (
            target.startswith("/workspace/")
            and not any(
                target == f"/workspace/{name}"
                or target.startswith(f"/workspace/{name}/")
                for name in NAMES
            )
        ):
            raise ValueError(f"Metadata mount would cover source files: {target}")
        previous = mounts.get(target)
        if previous is not None and previous.source != source:
            raise ValueError(f"Conflicting VCS metadata mounts at {target}")
        mounts[target] = MountSpec(source, target, True)

    def pointer(path: Path, guest_parent: str, *, gitfile=False) -> tuple[Path, str]:
        if path.is_symlink():
            raise ValueError(f"Symlinked VCS pointer is unsupported: {path}")
        raw = path.read_text(encoding="utf-8").strip()
        if gitfile:
            if not raw.startswith("gitdir: "):
                raise ValueError(f"Invalid Git worktree pointer: {path}")
            raw = raw.removeprefix("gitdir: ")
        if not raw or any(c in raw for c in "\n\r\0"):
            raise ValueError(f"Invalid VCS pointer: {path}")
        source = (path.parent / raw).resolve(strict=True)
        target = posixpath.normpath(posixpath.join(guest_parent, raw))
        return source, target

    def git_store(source: Path, target: str) -> None:
        add(source, target)
        common = source / "commondir"
        if common.exists():
            common_source, common_target = pointer(common, target)
            add(common_source, common_target)

    for name in NAMES:
        path = workspace / name
        if not os.path.lexists(path):
            continue
        roots.append(path)
        add(path, f"/workspace/{name}")
        if name == ".git":
            if path.is_file():
                source, target = pointer(path, "/workspace", gitfile=True)
                git_store(source, target)
            elif path.is_dir():
                git_store(path, "/workspace/.git")
            else:
                raise ValueError(f"Unsupported Git metadata type: {path}")
        else:
            if not path.is_dir():
                raise ValueError(f"Unsupported jj metadata type: {path}")
            repo = path / "repo"
            repo_target = "/workspace/.jj/repo"
            if repo.is_file():
                repo, repo_target = pointer(repo, "/workspace/.jj")
                add(repo, repo_target)
            elif repo.is_symlink():
                raise ValueError(f"Symlinked jj repository is unsupported: {repo}")
            backend = repo / "store" / "git_target"
            if backend.exists():
                source, target = pointer(backend, repo_target + "/store")
                git_store(source, target)

    # Nested mountpoints need ancestor protection too. Reject these layouts
    # until we can protect them against renaming/replacing their parent dirs.
    for parent, dirs, files in os.walk(
        workspace, followlinks=False, onerror=_walk_error
    ):
        if Path(parent) != workspace:
            for name in NAMES:
                if name in dirs or name in files:
                    raise ValueError(
                        f"Restricted VCS access does not support nested repositories: {Path(parent) / name}"
                    )
        dirs[:] = [
            name
            for name in dirs
            if name not in NAMES
            and not (
                Path(parent) == workspace
                and name in (".project-sandbox", ".devcontainer")
            )
        ]
    # A read-only directory mount does not protect a symlink's target. Reject
    # metadata trees that could redirect config/index/object writes back into
    # the writable checkout or another mount. Explicit Git/jj pointer files
    # above are handled separately with their own read-only mounts.
    sources = {m.source for m in mounts.values() if m.source.is_dir()}
    for source in sources:
        if any(parent in sources for parent in source.parents):
            continue
        for parent, dirs, files in os.walk(
            source, followlinks=False, onerror=_walk_error
        ):
            for name in dirs + files:
                path = Path(parent) / name
                if path.is_symlink():
                    raise ValueError(
                        f"Restricted VCS access does not support symlinks inside metadata: {path}"
                    )
                if path.is_file() and path.stat().st_nlink > 1:
                    raise ValueError(
                        f"Restricted VCS access does not support hard-linked metadata: {path}"
                    )
    # Do not let a runtime create a missing root metadata mountpoint on the
    # writable host checkout while mounting an external store at that path.
    for mount in mounts.values():
        if mount.target.startswith("/workspace/"):
            root_name = mount.target.split("/")[2]
            if workspace / root_name not in roots:
                raise ValueError(
                    f"External metadata collides with an absent workspace path: {mount.target}"
                )
    return Layout(
        workspace,
        tuple(roots),
        tuple(mounts.values()),
        frozenset(p for p in roots if p.is_file()),
    )


def validate(
    layout: Layout, mode: str, runtime: Runtime, extra_mounts: list[str]
) -> None:
    if mode not in MODES:
        raise ValueError(f"Unknown VCS access mode: {mode}")
    if mode == "full":
        return
    if not runtime.is_container:
        raise ValueError(
            "Restricted VCS access requires a container runtime; chroot is unsupported"
        )
    if runtime == APPLE_CONTAINER and layout.files:
        raise ValueError(
            "Restricted VCS access for a .git pointer file requires Docker or Podman; "
            "Apple container does not support the required file bind mount"
        )
    protected = [layout.workspace, *(m.source for m in layout.mounts)]
    for value in extra_mounts:
        mount = parse_mount(value)
        if mount.raw is not None:
            raise ValueError(
                "Restricted VCS access requires fully understood bind --mount options"
            )
        target = Path(mount.target)
        if (
            target == Path("/")
            or target == Path("/workspace")
            or Path("/workspace") in target.parents
        ):
            raise ValueError(
                "Restricted VCS access rejects extra mounts at or inside /workspace"
            )
        if any(
            mount.source == p or mount.source in p.parents or p in mount.source.parents
            for p in protected
        ):
            raise ValueError(
                f"Extra mount exposes protected workspace/VCS paths: {mount.source}"
            )
        if any(
            target == Path(m.target)
            or target in Path(m.target).parents
            or Path(m.target) in target.parents
            for m in layout.mounts
        ):
            raise ValueError(
                f"Extra mount overlaps protected VCS target: {mount.target}"
            )
    for mount in layout.mounts:
        if any(c in str(mount.source) + mount.target for c in ",\n\r\0"):
            raise ValueError(
                "Restricted VCS mounts cannot encode commas or control characters"
            )


def new_mask_dir() -> Path:
    """Private, non-repository-controlled sources for hidden metadata mounts."""
    path = Path(tempfile.mkdtemp(prefix="project-sandbox-vcs-"))
    (path / "directory").mkdir(mode=0o755)
    (path / "file").touch(mode=0o644)
    return path


def mounts(layout: Layout, mode: str, mask_dir: Path | None = None) -> list[MountSpec]:
    if mode == "full":
        return []
    if mode == "read-only":
        return list(layout.mounts)
    if mode != "hidden" or mask_dir is None:
        raise ValueError("Hidden VCS access requires private mask sources")
    return [
        MountSpec(
            mask_dir / ("file" if path in layout.files else "directory"),
            f"/workspace/{path.name}",
            True,
        )
        for path in layout.roots
    ]
