import getpass
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Claude profiles: a profile's settings.json defaultMode IS its .claude.json
# permission posture, so both the rendered settings file and the staged config
# state derive from a single value per profile. This is the one place that
# encodes Claude's permission posture.
CLAUDE_PROFILES = {
    "claude": "bypassPermissions",
    "claude-devcontainer": "auto",
}

_CLAUDE_CONFIG_BASE = {
    "autoUpdaterStatus": "disabled",
    "autoUpdates": False,
    "hasCompletedOnboarding": True,
    "installMethod": "npm",
    "projects": {"/workspace": {"hasTrustDialogAccepted": True}},
}


def _claude_config_state(permission_mode: str) -> dict:
    """Build the .claude.json config state for a profile's permission mode."""
    state = dict(_CLAUDE_CONFIG_BASE)
    if permission_mode == "bypassPermissions":
        state["bypassPermissionsModeAccepted"] = True
        state["permissions"] = {
            "defaultMode": "bypassPermissions",
            "skipDangerousModePermissionPrompt": True,
        }
    else:
        state["permissions"] = {"defaultMode": permission_mode}
    return state


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

_CONFIGURED_AGENTS = ("claude", "codex", "opencode")


def _agent_host_paths(home: Path) -> dict[str, Path]:
    return {
        "claude": home / ".claude",
        "codex": home / ".codex",
        "opencode": home / ".config" / "opencode",
    }


def available_agents(home: Path | None = None) -> tuple[str, ...]:
    """Return agents present on this host. Always includes 'bash'."""
    _home = home or Path.home()
    present = tuple(
        a for a in _CONFIGURED_AGENTS if _agent_host_paths(_home)[a].exists()
    )
    return (*present, "bash")


def render(context_dir: Path) -> dict[str, Path]:
    """Render all agent config files and return a dict of written paths."""
    paths: dict[str, Path] = {}
    for key, permission_mode in CLAUDE_PROFILES.items():
        out_dir = _ensure_project_subdir(context_dir, key)
        out = out_dir / "settings.json"
        out.write_text(_claude_settings_json(permission_mode), encoding="utf-8")
        paths[key] = out
    for key, approval_policy in (
        ("codex", "never"),
        ("codex-devcontainer", "on-request"),
    ):
        out_dir = _ensure_project_subdir(context_dir, key)
        out = out_dir / "config.toml"
        out.write_text(_codex_config_toml(approval_policy), encoding="utf-8")
        paths[key] = out
    return paths


def sync_credentials(
    project_sandbox_dir: Path,
    *,
    home: Path | None = None,
) -> dict[str, Path]:
    """Stage credentials for all agents present on this host.

    Returns a dict keyed by agent name:
      "claude", "claude-devcontainer" — always present
      "codex", "opencode"             — present only if the agent is installed
    """
    _home = home or Path.home()
    host_paths = _agent_host_paths(_home)
    result: dict[str, Path] = {}
    for profile, permission_mode in CLAUDE_PROFILES.items():
        result[profile] = _sync_claude_credentials(
            project_sandbox_dir,
            agent=profile,
            config_state=_claude_config_state(permission_mode),
            home=home,
        )
    if host_paths["codex"].exists():
        result["codex"] = _sync_generic_credentials(
            project_sandbox_dir,
            "codex",
            host_paths["codex"],
            include_files=("auth.json",),
        )
    if host_paths["opencode"].exists():
        result["opencode"] = _sync_opencode_credentials(
            project_sandbox_dir,
            home=_home,
        )
    return result


def credentials_dir(project_sandbox_dir: Path, agent: str = "claude") -> Path:
    if not all(c.isalnum() or c in "._-" for c in agent):
        raise ValueError(f"Invalid credential agent name: {agent}")
    key = str(project_sandbox_dir.resolve(strict=False))
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    uid = os.getuid() if hasattr(os, "getuid") else "user"
    return CREDENTIALS_ROOT / f"project-sandbox-{uid}" / digest / agent


def _claude_settings_json(permission_mode: str) -> str:
    settings = {
        "$schema": "https://json.schemastore.org/claude-code-settings.json",
        "permissions": {
            "defaultMode": permission_mode,
            "allow": [],
            "deny": [],
            "ask": [],
        },
        "sandbox": {"enabled": False},
        "env": {
            "IS_SANDBOX": "1",
            "CLAUDE_TELEMETRY_DISABLED": "1",
        },
        "theme": "auto",
        "autoUpdaterStatus": "disabled",
        "includeCoAuthoredBy": False,
    }
    return json.dumps(settings, indent=2) + "\n"


def _codex_config_toml(approval_policy: str) -> str:
    return (
        f'approval_policy = "{approval_policy}"\n'
        'sandbox_mode = "danger-full-access"\n'
        "disable_update_check = true\n"
        "\n"
        "[sandbox_workspace_write]\n"
        "network_access = true\n"
        "\n"
        '[projects."/workspace"]\n'
        'trust_level = "trusted"\n'
        "\n"
        "[shell_environment_policy]\n"
        'inherit = "core"\n'
        "\n"
        "[analytics]\n"
        "enabled = false\n"
        "\n"
        "[feedback]\n"
        "enabled = false\n"
    )


def _sync_claude_credentials(
    project_sandbox_dir: Path,
    *,
    agent: str,
    config_state: dict,
    home: Path | None,
) -> Path:
    use_host_keychain = home is None
    home = home or Path.home()
    out_dir = credentials_dir(project_sandbox_dir, agent)
    _ensure_private_dir(out_dir)
    if agent == "claude":
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
        config_state=config_state,
    )
    return out_dir


def _sync_generic_credentials(
    project_sandbox_dir: Path,
    agent: str,
    source_dir: Path,
    *,
    include_files: tuple[str, ...] | None = None,
) -> Path:
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


def _sync_opencode_credentials(
    project_sandbox_dir: Path,
    *,
    home: Path,
) -> Path:
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


def _reject_symlinked_project_path(path: Path) -> None:
    """Refuse a managed .project-sandbox path reached through a symlink.

    A hostile repository can pre-place an in-repo `.project-sandbox/<agent>`
    path as a symlink to e.g. ~/.claude, so that rendering writes outside the
    project and stale-cleanup unlinks host credentials. Mirror the
    `_ensure_private_dir` guard: lstat each managed component and reject any
    symlink before writing or unlinking.
    """
    for component in (path.parent, path):
        if component.is_symlink():
            raise RuntimeError(
                f"Refusing to use symlinked project config path: {component}"
            )


def _ensure_project_subdir(context_dir: Path, name: str) -> Path:
    """Create `context_dir / name`, refusing symlinked components."""
    out_dir = context_dir / name
    _reject_symlinked_project_path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _ensure_private_dir(path: Path) -> None:
    for directory in (path.parent.parent, path.parent, path):
        if directory.is_symlink():
            raise RuntimeError(
                f"Refusing to use symlinked credential directory: {directory}"
            )
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)


def purge_staged_credentials(project_sandbox_dir: Path) -> None:
    """Delete this project's staged credential tree under /tmp.

    Used with --no-forward-credentials: no host tokens are staged, and any left
    over from a previous forwarding run are removed so nothing lingers on disk or
    can be mounted. The per-project digest directory holds only this project's
    staged agent credentials, so removing it is safe.
    """
    digest_dir = credentials_dir(project_sandbox_dir, "claude").parent
    _remove_path_if_exists(digest_dir)


def _remove_stale_project_credentials(project_sandbox_dir: Path) -> None:
    project_claude_dir = project_sandbox_dir / "claude"
    _reject_symlinked_project_path(project_claude_dir)
    for name in (".credentials.json", ".claude.json"):
        _remove_if_exists(project_claude_dir / name)


def _remove_stale_project_agent_credentials(
    project_sandbox_dir: Path,
    agent: str,
    include_files: tuple[str, ...] | None,
) -> None:
    project_agent_dir = project_sandbox_dir / agent
    _reject_symlinked_project_path(project_agent_dir)
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


def _stage_config_state(
    sources: tuple[Path, ...], target: Path, *, config_state: dict
) -> None:
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
    state.update(config_state)
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
    username = os.environ.get("USER")
    if not username:
        try:
            username = getpass.getuser()
        except (OSError, KeyError, ImportError):
            username = None
    if username and all(c.isalnum() or c in "._-" for c in username):
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
