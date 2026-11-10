from pathlib import Path

from . import paths, templating

SCRIPT_NAME = "chroot-run.sh"
JAIL_ROOT_NAME = "chroot-root"


def script_text() -> str:
    return templating.get_template("chroot-run.sh.j2").render() + "\n"


def render(context_dir: "paths.Target") -> Path:
    with paths.target_dir(context_dir) as directory:
        directory.write(SCRIPT_NAME, script_text(), mode=0o700)
    return paths.target_path(context_dir, SCRIPT_NAME)
