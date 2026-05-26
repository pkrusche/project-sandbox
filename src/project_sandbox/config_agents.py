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
    "hasCompletedOnboarding": True,
    "installMethod": "npm",
    "projects": {"/workspace": {"hasTrustDialogAccepted": True}},
    "permissions": {
        "defaultMode": "bypassPermissions",
        "skipDangerousModePermissionPrompt": True,
    },
}

CLAUDE_CREDENTIAL_STATE_KEYS = frozenset(
    (
        "claudeAiOauth",
        "lastOnboardingVersion",
        "oauthAccount",
        "token",
        "userID",
    )
)

CREDENTIALS_ROOT = Path("/tmp")


def render_claude(
    project_sandbox_dir: Path,
) -> Path:
    out_dir = project_sandbox_dir / "claude"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "settings.json"
    env = Environment(loader=PackageLoader("project_sandbox", "templates"))
    tmpl = env.get_template("claude-settings.json.j2")
    out.write_text(
        tmpl.render() + "\n",
        encoding="utf-8",
    )
    return out


def render_codex(project_sandbox_dir: Path) -> Path:
    out_dir = project_sandbox_dir / "codex"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "config.toml"
    env = Environment(loader=PackageLoader("project_sandbox", "templates"))
    tmpl = env.get_template("codex-config.toml.j2")
    out.write_text(tmpl.render() + "\n", encoding="utf-8")
    return out


def sync_credentials(project_sandbox_dir: Path, *, home: Path | None = None) -> Path:
    """Stage Claude auth files outside the generated project directory."""
    use_host_keychain = home is None
    home = home or Path.home()
    out_dir = credentials_dir(project_sandbox_dir)
    _ensure_private_dir(out_dir)
    _remove_stale_project_credentials(project_sandbox_dir)
    if not (use_host_keychain and _stage_macos_keychain_credentials(out_dir)):
        copied = _copy_if_file(
            home / ".claude" / ".credentials.json",
            out_dir / ".credentials.json",
        )
        if not copied:
            _remove_if_exists(out_dir / ".credentials.json")
    _stage_config_state(
        (
            home / ".claude.json",
            home / ".claude" / ".claude.json",
        ),
        out_dir / ".claude.json",
    )
    return out_dir


def sync_agent_credentials(
    project_sandbox_dir: Path,
    agent: str,
    source_dir: Path,
    *,
    include_files: tuple[str, ...] | None = None,
) -> Path:
    """Stage non-Claude agent credentials outside the generated project directory."""
    out_dir = credentials_dir(project_sandbox_dir, agent)
    _ensure_private_dir(out_dir)
    _remove_stale_project_agent_credentials(project_sandbox_dir, agent, include_files)
    _clear_dir(out_dir)
    if not source_dir.is_dir():
        return out_dir
    if include_files is None:
        for child in source_dir.iterdir():
            _copy_path(child, out_dir / child.name)
        return out_dir
    for name in include_files:
        _copy_path(source_dir / name, out_dir / name)
    return out_dir


def sync_opencode_credentials(
    project_sandbox_dir: Path,
    *,
    home: Path | None = None,
) -> Path:
    """Stage OpenCode config and state without copying package installs."""
    home = home or Path.home()
    out_dir = credentials_dir(project_sandbox_dir, "opencode")
    _ensure_private_dir(out_dir)
    _remove_stale_project_agent_credentials(project_sandbox_dir, "opencode", None)
    _clear_dir(out_dir)
    source_config = home / ".config" / "opencode"
    target_config = out_dir / ".config" / "opencode"
    for name in ("opencode.json", "opencode.jsonc"):
        _copy_path(source_config / name, target_config / name)
    _copy_dir_contents(
        home / ".local" / "share" / "opencode",
        out_dir / ".local" / "share" / "opencode",
    )
    _copy_dir_contents(
        home / ".local" / "state" / "opencode",
        out_dir / ".local" / "state" / "opencode",
    )
    return out_dir


def credentials_dir(project_sandbox_dir: Path, agent: str = "claude") -> Path:
    if not all(c.isalnum() or c in "._-" for c in agent):
        raise ValueError(f"Invalid credential agent name: {agent}")
    key = str(project_sandbox_dir.resolve(strict=False))
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    uid = os.getuid() if hasattr(os, "getuid") else "user"
    return CREDENTIALS_ROOT / f"project-sandbox-{uid}" / digest / agent


def _ensure_private_dir(path: Path) -> None:
    for directory in (path.parent.parent, path.parent, path):
        if directory.is_symlink():
            raise RuntimeError(
                f"Refusing to use symlinked credential directory: {directory}"
            )
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)


def _remove_stale_project_credentials(project_sandbox_dir: Path) -> None:
    project_claude_dir = project_sandbox_dir / "claude"
    for name in (".credentials.json", ".claude.json"):
        _remove_if_exists(project_claude_dir / name)


def _remove_stale_project_agent_credentials(
    project_sandbox_dir: Path,
    agent: str,
    include_files: tuple[str, ...] | None,
) -> None:
    project_agent_dir = project_sandbox_dir / agent
    if include_files is None:
        _remove_path_if_exists(project_agent_dir)
        return
    for name in include_files:
        _remove_path_if_exists(project_agent_dir / name)


def _remove_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _remove_path_if_exists(path: Path) -> None:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    except FileNotFoundError:
        pass


def _clear_dir(path: Path) -> None:
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def _copy_path(source: Path, target: Path) -> None:
    if not source.exists():
        return
    if source.is_symlink():
        raise RuntimeError(f"Refusing to stage symlinked credential path: {source}")
    if source.is_dir():
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        for child in source.iterdir():
            _copy_path(child, target / child.name)
        return
    if source.is_file() and source.stat().st_size > 0:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o600)


def _copy_dir_contents(source: Path, target: Path) -> None:
    if not source.is_dir():
        return
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    for child in source.iterdir():
        _copy_path(child, target / child.name)


def _copy_if_file(source: Path, target: Path) -> bool:
    if not source.is_file() or source.stat().st_size == 0:
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
            state.update(_credential_state(existing))
        break
    state.update(CONTAINER_CONFIG_STATE)
    _write_secure_text(target, json.dumps(state, indent=2, sort_keys=True))


def _credential_state(existing: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in existing.items()
        if key in CLAUDE_CREDENTIAL_STATE_KEYS
    }


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
