from pathlib import Path

from . import templating


def render(context_dir: Path) -> Path:
    out = context_dir / "chroot-run.sh"
    out.write_text(
        templating.get_template("chroot-run.sh.j2").render() + "\n", encoding="utf-8"
    )
    out.chmod(0o700)
    return out
