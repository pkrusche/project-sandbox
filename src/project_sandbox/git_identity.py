from dataclasses import dataclass

from . import host_tools


@dataclass(slots=True)
class GitIdentity:
    name: str | None
    email: str | None


def _get_global(key: str) -> str | None:
    # --global only, from a neutral directory: the project's own .git/config
    # (agent-writable in a shared session) is never read.
    try:
        result = host_tools.run(
            ["git", "config", "--global", "--get", key],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    value = result.stdout.strip()
    return value or None


def read() -> GitIdentity:
    return GitIdentity(name=_get_global("user.name"), email=_get_global("user.email"))
