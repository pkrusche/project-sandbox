"""One-time migration of a project's legacy ``.project-sandbox/`` state.

Older versions kept everything in ``<project>/.project-sandbox/``. On the first
launch of this version, under the project's build lock:

1. shell and Claude history and session logs are moved into the project's
   private state (regular files only; links are skipped);
2. every generated file and directory this tool used to write there is deleted,
   including stale secrets (``api-keys.env``, agent-proxy configs, staged
   credentials);
3. anything else is left in place and named;
4. ``projects/<key>/migrated`` is written, so this runs once.

Everything goes through descriptors opened without following links, so a link
planted in ``.project-sandbox`` is removed (or skipped), never followed. The
migration is postponed while a session of the project is still running: a
session started by an older version has these files mounted.
"""

import hashlib
import json
import re
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import build_cache, observability, paths
from .paths import PrivateDir

LEGACY_DIR = ".project-sandbox"
MARKER = "migrated"

# Top-level files older versions generated.
_GENERATED_FILE_RE = re.compile(
    r"(Dockerfile(\.(devcontainer|python-uv|rust-cargo|node-npm))?(\.dockerignore)?"
    r"|entrypoint\.sh|init-firewall\.sh|project-sandbox-init"
    r"|project-sandbox-devcontainer-init|chroot-run\.sh|\.gitignore"
    r"|\.build-state\.json|\.dockerfile-checksums\.json|api-keys\.env"
    r"|project-sandbox-(build-)?ca-[0-9a-f]{64}\.crt)"
)
# Top-level directories older versions generated; removed as a whole.
_GENERATED_DIRS = frozenset(
    (
        "claude",
        "codex",
        "pi",
        "opencode",
        "copilot",
        "claude-devcontainer",
        "codex-devcontainer",
        "prompt",
        "prompts",
        "workspace-mask",
        "chroot-root",
    )
)
# Files that held secrets: reported by name when removed.
_SECRET_FILES = frozenset(
    (
        "api-keys.env",
        "pi/models.json",
        "pi/auth.json",
        "opencode/opencode.json",
        "claude/.credentials.json",
        "claude/.claude.json",
        "codex/auth.json",
    )
)


@dataclass
class Plan:
    """What a migration of ``project`` would do (computed without changes)."""

    project: Path
    present: bool = False
    linked: bool = False
    running: list[str] = field(default_factory=list)
    moves: list[str] = field(default_factory=list)
    removals: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    legacy_credentials: Path | None = None

    @property
    def legacy(self) -> Path:
        return self.project / LEGACY_DIR

    def describe(self) -> list[str]:
        lines = []
        if self.legacy_credentials is not None:
            lines.append(
                f"remove credentials staged by an earlier version: {self.legacy_credentials}"
            )
        if self.linked:
            return [*lines, f"{self.legacy} is a symlink; it would be left alone"]
        if not self.present:
            return lines
        if self.running:
            lines.append(
                "postponed while session(s) "
                + ", ".join(self.running)
                + " of this project are running"
            )
        if self.moves:
            lines.append(f"move {len(self.moves)} history/log file(s) to private state")
        if self.secrets:
            lines.append("remove stale secrets: " + ", ".join(self.secrets))
        if self.removals:
            lines.append(f"remove {len(self.removals)} generated file(s) and dir(s)")
        if self.unknown:
            lines.append(
                f"keep unknown files in {self.legacy}: " + ", ".join(self.unknown)
            )
        return lines


def _running_sessions(project: Path) -> list[str]:
    resolved = str(project.resolve())
    running = []
    for record in observability.list_records():
        if record.get("status") != "running":
            continue
        path = record.get("project_path") or record.get("workspace_path")
        if path and str(Path(path).resolve()) == resolved:
            running.append(str(record.get("session_id") or "?"))
    return running


LEGACY_CREDENTIALS_PARENT = Path("/tmp")


def legacy_credentials_dir(project: Path) -> Path:
    """Where versions before private-session-state staged this project's
    credentials: ``/tmp/project-sandbox-<uid>/<digest of .project-sandbox>``."""
    key = str((project / LEGACY_DIR).resolve(strict=False))
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return LEGACY_CREDENTIALS_PARENT / f"project-sandbox-{paths._uid()}" / digest


def _legacy_credentials_present(project: Path) -> Path | None:
    path = legacy_credentials_dir(project)
    try:
        st = path.lstat()
    except OSError:
        return None
    return path if stat.S_ISDIR(st.st_mode) and st.st_uid == paths._uid() else None


def plan(project: Path) -> Plan:
    """Scan ``<project>/.project-sandbox`` without changing anything."""
    result = Plan(project=project)
    result.legacy_credentials = _legacy_credentials_present(project)
    legacy = project / LEGACY_DIR
    if legacy.is_symlink():
        result.linked = True
        return result
    if not legacy.is_dir():
        return result
    result.present = True
    result.running = _running_sessions(project)
    try:
        directory = paths.open_in_project(project, LEGACY_DIR, create=False)
    except paths.UnsafePathError:
        result.linked = True
        return result
    with directory:
        for name in directory.listdir():
            st = directory.lstat(name)
            if st is None:
                continue
            is_dir = stat.S_ISDIR(st.st_mode)
            if name in ("history", "sessions") and is_dir:
                with directory.subdir(name, create=False, private=False) as sub:
                    for rel, kind in paths.walk_files(sub, f"{name}/"):
                        if kind == "file" and _movable(rel):
                            result.moves.append(rel)
                result.removals.append(name + "/")
            elif name in _GENERATED_DIRS and is_dir:
                result.removals.append(name + "/")
                with directory.subdir(name, create=False, private=False) as sub:
                    for rel, kind in paths.walk_files(sub, f"{name}/"):
                        if kind == "file" and rel in _SECRET_FILES:
                            result.secrets.append(rel)
            elif _GENERATED_FILE_RE.fullmatch(name) or (
                stat.S_ISLNK(st.st_mode)
                and (name in _GENERATED_DIRS or name in ("history", "sessions"))
            ):
                # A link is removed itself; its target is never touched.
                result.removals.append(name)
                if name in _SECRET_FILES and stat.S_ISREG(st.st_mode):
                    result.secrets.append(name)
            else:
                result.unknown.append(name)
    return result


def _movable(rel: str) -> bool:
    return rel == "history/shell/bash_history" or rel.startswith(
        ("history/claude_projects/", "sessions/")
    )


def run(project: Path, state: PrivateDir, *, quiet: bool = False) -> Plan | None:
    """Migrate once; return the executed plan, or None when nothing ran."""
    if state.lstat(MARKER) is not None:
        return None
    with build_cache.build_lock(project):
        if state.lstat(MARKER) is not None:
            return None
        current = plan(project)
        if current.running:
            print(
                f"[i] Postponing migration of {current.legacy}: session(s) "
                f"{', '.join(current.running)} of this project are still running."
            )
            return None
        if current.present and not current.linked:
            _execute(project, state, current)
        elif current.linked:
            print(f"[W] {current.legacy} is a symlink; left it alone.")
        if current.legacy_credentials is not None:
            _remove_legacy_credentials(current.legacy_credentials)
        state.write(
            MARKER,
            json.dumps(
                {
                    "migrated_at": datetime.now(UTC).isoformat(),
                    "moved": len(current.moves),
                    "removed": current.removals,
                    "unknown": current.unknown,
                },
                sort_keys=True,
            )
            + "\n",
            mode=0o600,
        )
    if not quiet and current.present and not current.linked:
        _report(current, state.path)
    if not quiet and current.legacy_credentials is not None:
        print(
            "[i] Removed credentials staged by an earlier version: "
            f"{current.legacy_credentials}"
        )
    return current


def _remove_legacy_credentials(path: Path) -> None:
    """Delete an old per-project credential staging tree, no-follow.

    Its parent must be a private directory owned by the caller; anything else
    is left alone.
    """
    try:
        with paths.PrivateDir.open(
            path.parent, anchor=LEGACY_CREDENTIALS_PARENT, create=False
        ) as parent:
            parent.remove(path.name)
    except (OSError, paths.UnsafePathError) as exc:
        print(f"[W] Could not remove old credential staging {path}: {exc}")


def _execute(project: Path, state: PrivateDir, current: Plan) -> None:
    with paths.open_in_project(project, LEGACY_DIR, create=False) as legacy:
        for rel in current.moves:
            _move(legacy, state, rel)
        for name in current.removals:
            legacy.remove(name.rstrip("/"))
        legacy_empty = not legacy.listdir()
    if legacy_empty:
        try:
            (project / LEGACY_DIR).rmdir()
        except OSError:
            pass


def _move(legacy: PrivateDir, state: PrivateDir, rel: str) -> None:
    """Copy one regular file to the same relative path in private state."""
    parts = rel.split("/")
    data = _read_no_follow(legacy, parts)
    if data is None:
        return
    directory = state
    opened: list[PrivateDir] = []
    try:
        for part in parts[:-1]:
            directory = directory.subdir(part)
            opened.append(directory)
        name = parts[-1]
        existing = directory.read_bytes(name)
        if existing is not None:
            if rel != "history/shell/bash_history":
                return  # keep what this version already wrote
            data = (
                data + (b"" if data.endswith(b"\n") or not data else b"\n") + existing
            )
        directory.write(name, data, mode=0o600)
    finally:
        for item in reversed(opened):
            item.close()


def _read_no_follow(legacy: PrivateDir, parts: list[str]) -> bytes | None:
    directory = legacy
    opened: list[PrivateDir] = []
    try:
        for part in parts[:-1]:
            try:
                directory = directory.subdir(part, create=False, private=False)
            except paths.UnsafePathError:
                return None
            opened.append(directory)
        return directory.read_bytes(parts[-1])
    finally:
        for item in reversed(opened):
            item.close()


def _report(current: Plan, state_path: Path) -> None:
    print(f"[i] Migrated {current.legacy} to private state in {state_path}")
    if current.moves:
        print(f"    moved {len(current.moves)} history/log file(s)")
    if current.secrets:
        print("    removed stale secrets: " + ", ".join(current.secrets))
    if current.removals:
        print("    removed generated: " + ", ".join(current.removals))
    if current.unknown:
        print(
            f"[i] Kept files project-sandbox did not generate in {current.legacy}: "
            + ", ".join(current.unknown)
        )
