from pathlib import Path

from . import generated_files, templating


def render(context_dir: Path) -> Path:
    out = context_dir / "chroot-run.sh"
    generated_files.write_text(
        out, templating.get_template("chroot-run.sh.j2").render() + "\n", mode=0o700
    )
    return out
