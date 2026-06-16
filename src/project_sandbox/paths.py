from pathlib import Path


def resolve_strict(path: str | Path) -> Path:
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"Path does not exist: {p}")
    return p.resolve()


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_history_paths(project: Path, *, create: bool = True) -> tuple[Path, Path]:
    """Return the persistent history paths for ``project`` and (optionally)
    create them on the host so bind mounts have something to bind to.

    Returns ``(bash_history_file, claude_projects_dir)`` under
    ``.project-sandbox/history/``.
    """
    history_dir = project / ".project-sandbox" / "history"
    bash_history = history_dir / "bash_history"
    claude_projects = history_dir / "claude_projects"
    if create:
        ensure_dir(history_dir)
        if not bash_history.exists():
            bash_history.touch()
        ensure_dir(claude_projects)
    return bash_history, claude_projects
