from pathlib import Path


def resolve_strict(path: str | Path) -> Path:
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"Path does not exist: {p}")
    return p.resolve()


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
