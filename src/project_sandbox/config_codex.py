from pathlib import Path

from jinja2 import Environment, PackageLoader


def render(project_sandbox_dir: Path) -> Path:
    out_dir = project_sandbox_dir / "codex"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "config.toml"
    env = Environment(loader=PackageLoader("project_sandbox", "templates"))
    tmpl = env.get_template("codex-config.toml.j2")
    out.write_text(tmpl.render() + "\n", encoding="utf-8")
    return out
