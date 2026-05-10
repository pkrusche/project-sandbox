from pathlib import Path

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
