from pathlib import Path
import shutil

from jinja2 import Environment, PackageLoader


def render(project_sandbox_dir: Path, *, refresh: bool = False) -> Path:
    out_dir = project_sandbox_dir / "claude"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "settings.json"
    if out.exists() and not refresh:
        return out
    env = Environment(loader=PackageLoader("project_sandbox", "templates"))
    tmpl = env.get_template("claude-settings.json.j2")
    out.write_text(tmpl.render() + "\n", encoding="utf-8")
    return out


def sync_credentials(project_sandbox_dir: Path, *, home: Path | None = None) -> None:
    """Stage Claude auth files for directory-only container mounts."""
    home = home or Path.home()
    out_dir = project_sandbox_dir / "claude"
    out_dir.mkdir(parents=True, exist_ok=True)
    for source, target_name in (
        (home / ".claude" / ".credentials.json", ".credentials.json"),
        (home / ".claude.json", ".claude.json"),
    ):
        if source.is_file():
            target = out_dir / target_name
            shutil.copyfile(source, target)
            target.chmod(0o600)
