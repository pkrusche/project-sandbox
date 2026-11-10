"""Runs inside the container through --agent bash, after entrypoint initialization."""

import os
import re
import stat
import subprocess
from pathlib import Path

SCRIPT = "/usr/local/bin/project-sandbox-init-firewall"
GUARD = "/run/project-sandbox-firewall-init"
SUDOERS = "/etc/sudoers.d/agent-firewall"


def check_initialization() -> None:
    if os.geteuid() == 0:
        raise RuntimeError("audit must run as the unprivileged agent")
    for path in (SCRIPT, GUARD):
        info = Path(path).stat()
        if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
            raise RuntimeError(
                f"expected root-owned, mode 0700 initialization artifact: {path}"
            )
    # stat must report absence, not just an unreadable sudoers directory.
    try:
        Path(SUDOERS).lstat()
    except FileNotFoundError:
        pass
    else:
        raise RuntimeError("firewall sudoers entry survived initialization")


def check_sudo(workspace: Path) -> None:
    env = {**os.environ, "LC_ALL": "C"}
    for name, args in (
        ("sudo-policy", ["sudo", "-n", "-l", SCRIPT]),
        ("sudo-attempt", ["sudo", "-n", SCRIPT]),
    ):
        result = subprocess.run(
            args, capture_output=True, text=True, env=env, check=False
        )
        output = result.stdout + result.stderr
        (workspace / f"{name}.log").write_text(output)
        if result.returncode == 0:
            raise RuntimeError(f"{name}: firewall sudo permission remains")
        if "Firewall initialization already attempted" in output:
            raise RuntimeError(
                "sudo executed the script; only its reinitialization guard blocked it"
            )
        if name == "sudo-attempt" and not re.search(
            r"password is required|not allowed|not in the sudoers|not permitted", output
        ):
            raise RuntimeError(f"sudo failed for an unexpected reason: {output}")


def main() -> None:
    workspace = Path("/workspace")
    check_initialization()
    check_sudo(workspace)
    (workspace / "firewall.ok").write_text("initialized; sudo revoked\n")


if __name__ == "__main__":
    main()
