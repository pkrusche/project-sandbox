from dataclasses import dataclass
import subprocess


@dataclass(slots=True)
class GitIdentity:
    name: str | None
    email: str | None


def _get_global(key: str) -> str | None:
    cmd = ["git", "config", "--global", "--get", key]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    value = result.stdout.strip()
    return value or None


def read() -> GitIdentity:
    return GitIdentity(name=_get_global("user.name"), email=_get_global("user.email"))
