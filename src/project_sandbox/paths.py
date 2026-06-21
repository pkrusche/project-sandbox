from pathlib import Path


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


def ensure_history_paths(project: Path, *, create: bool = True) -> tuple[Path, Path]:
    """Return the persistent history *directories* for ``project`` and
    (optionally) create them on the host so the bind mounts have something to
    bind to.

    Returns ``(shell_history_dir, claude_projects_dir)`` under
    ``.project-sandbox/history/``. Both are directories so the mounts work on
    apple/container; bash history lives in ``shell/bash_history`` (see
    ``HISTORY_HISTFILE``).
    """
    history_dir = project / ".project-sandbox" / "history"
    shell_dir = history_dir / "shell"
    claude_projects = history_dir / "claude_projects"
    if create:
        ensure_dir(shell_dir)
        ensure_dir(claude_projects)
        histfile = shell_dir / "bash_history"
        if not histfile.exists():
            histfile.touch()
    return shell_dir, claude_projects


def ensure_workspace_sandbox_mask(project: Path, *, create: bool = True) -> Path:
    """Return the empty host directory mounted over /workspace/.project-sandbox.

    Generated sandbox files must exist on the host for image/devcontainer builds,
    but exposing them through the writable workspace mount lets an agent tamper
    with files that might be copied into a later image. Mount this empty
    directory read-only over the in-workspace generated directory instead.
    """
    mask_dir = project / ".project-sandbox" / "workspace-mask"
    if create:
        ensure_dir(mask_dir)
    return mask_dir
