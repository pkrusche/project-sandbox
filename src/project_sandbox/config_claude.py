import getpass
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from jinja2 import Environment, PackageLoader

CONTAINER_CONFIG_STATE = {
    "autoUpdaterStatus": "disabled",
    "autoUpdates": False,
    "bypassPermissionsModeAccepted": True,
    "installMethod": "npm",
    "theme": "auto",
    "projects": {"/workspace": {"hasTrustDialogAccepted": True}},
    "permissions": {
        "defaultMode": "bypassPermissions",
        "skipDangerousModePermissionPrompt": True,
    },
}


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
    use_host_keychain = home is None
    home = home or Path.home()
    out_dir = project_sandbox_dir / "claude"
    out_dir.mkdir(parents=True, exist_ok=True)
    if not (use_host_keychain and _stage_macos_keychain_credentials(out_dir)):
        _copy_if_file(
            home / ".claude" / ".credentials.json",
            out_dir / ".credentials.json",
        )
    _stage_config_state(
        (
            home / ".claude.json",
            home / ".claude" / ".claude.json",
        ),
        out_dir / ".claude.json",
    )


def _copy_if_file(source: Path, target: Path) -> bool:
    if not source.is_file():
        return False
    shutil.copyfile(source, target)
    target.chmod(0o600)
    return True


def _stage_config_state(sources: tuple[Path, ...], target: Path) -> None:
    state: dict[str, object] = {}
    for source in sources:
        if not source.is_file():
            continue
        try:
            existing = json.loads(source.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = None
        if isinstance(existing, dict):
            state.update(existing)
        break
    state.update(CONTAINER_CONFIG_STATE)
    _write_secure_text(target, json.dumps(state, indent=2, sort_keys=True))


def _stage_macos_keychain_credentials(out_dir: Path) -> bool:
    if sys.platform != "darwin" or shutil.which("security") is None:
        return False
    for service in _keychain_service_names():
        try:
            result = subprocess.run(
                [
                    "security",
                    "find-generic-password",
                    "-a",
                    _keychain_account(),
                    "-w",
                    "-s",
                    service,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode != 0:
            continue
        if not _is_claude_oauth_credentials(result.stdout):
            continue
        _write_secure_text(out_dir / ".credentials.json", result.stdout)
        return True
    return False


def _keychain_account() -> str:
    username = os.environ.get("USER") or getpass.getuser() or "claude-code-user"
    if all(c.isalnum() or c in "._-" for c in username):
        return username
    return "claude-code-user"


def _keychain_service_names() -> tuple[str, ...]:
    services = ["Claude Code-credentials"]
    config_dir = os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR")
    if config_dir is None and os.environ.get("CLAUDE_CONFIG_DIR"):
        config_dir = os.environ["CLAUDE_CONFIG_DIR"]
    if config_dir:
        digest = hashlib.sha256(config_dir.encode("utf-8")).hexdigest()[:8]
        services.insert(0, f"Claude Code-credentials-{digest}")
    return tuple(dict.fromkeys(services))


def _is_claude_oauth_credentials(raw: str) -> bool:
    try:
        credentials = json.loads(raw)
    except json.JSONDecodeError:
        return False
    oauth = credentials.get("claudeAiOauth")
    return isinstance(oauth, dict) and isinstance(oauth.get("accessToken"), str)


def _write_secure_text(target: Path, content: str) -> None:
    target.write_text(
        content if content.endswith("\n") else content + "\n",
        encoding="utf-8",
    )
    target.chmod(0o600)
