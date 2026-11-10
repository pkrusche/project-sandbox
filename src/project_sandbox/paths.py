from pathlib import Path

from . import generated_files, mount_storage


def resolve_strict(path: str | Path) -> Path:
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"Path does not exist: {p}")
    return p.resolve()


def ensure_dir(path: Path) -> Path:
    return generated_files.ensure_dir(path)


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


def ensure_history_paths(project: Path, *, create: bool = True) -> tuple[Path, Path]:
    """Return persistent history roots in private host storage outside the repo."""
    if create:
        return mount_storage.prepare_history(project)
    return mount_storage.history_paths(project)


def ensure_workspace_sandbox_mask(
    project: Path, *, create: bool = True, token: str = "devcontainer"
) -> Path:
    """Return an empty private mask; CLI callers supply a unique session token."""
    if create:
        return mount_storage.prepare_mask(project, token)
    return mount_storage.mask_path(project, token)
