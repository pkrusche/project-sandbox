from __future__ import annotations
import fcntl
import hashlib
import os
import posixpath
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class JjWorkspace:
    path: Path
    bookmark: str


def setup(
    repo: Path,
    bookmark: str,
    base: str | None = None,
    workspace_dir: Path | None = None,
) -> JjWorkspace:
    repo = repo.resolve()
    ws_path = path_for(repo, bookmark, workspace_dir=workspace_dir)

    if ws_path.exists():
        existing = _list_workspaces(repo)
        if str(ws_path) in existing or str(ws_path.resolve()) in existing:
            return JjWorkspace(path=ws_path, bookmark=bookmark)
        raise SystemExit(
            f"workspace directory already exists but is not registered: {ws_path}\n"
            f"  Remove or rename it, then retry."
        )

    ws_path.parent.mkdir(parents=True, exist_ok=True)
    add_args = ["workspace", "add"]
    if base:
        add_args += ["-r", base]
    elif not _current_revision_is_empty(repo):
        add_args += ["-r", "@"]
    add_args.append(str(ws_path))
    _jj(repo, add_args)

    # Create or move the bookmark to this workspace's working-copy commit.
    # After a rebase/merge teardown the bookmark survives but the workspace does
    # not, so a fresh workspace must set rather than create.
    if _bookmark_exists(ws_path, bookmark):
        _jj(ws_path, ["bookmark", "set", "--allow-backwards", bookmark, "-r", "@"])
    else:
        _jj(ws_path, ["bookmark", "create", bookmark])

    return JjWorkspace(path=ws_path, bookmark=bookmark)


def path_for(
    repo: Path,
    bookmark: str,
    workspace_dir: Path | None = None,
) -> Path:
    repo = repo.resolve()
    ws_root = workspace_dir or (repo.parent / f"{repo.name}-workspaces")
    safe = bookmark.replace("/", "-")
    if "/" in bookmark:
        suffix = hashlib.sha256(bookmark.encode()).hexdigest()[:6]
        safe = f"{safe}-{suffix}"
    return ws_root / safe


def repo_store_mount(repo: Path, ws_path: Path) -> tuple[Path, str]:
    """Return the shared jj repo store source and its container target path."""
    source = (repo.resolve() / ".jj" / "repo").resolve()
    pointer = ws_path / ".jj" / "repo"
    if pointer.is_file():
        raw_target = pointer.read_text(encoding="utf-8").strip()
    else:
        raw_target = os.path.relpath(source, start=(ws_path.resolve() / ".jj"))

    if raw_target.startswith("/"):
        target = raw_target
    else:
        target = posixpath.normpath(
            posixpath.join("/workspace", ".jj", raw_target.replace(os.sep, "/"))
        )
    return source, target


def git_backend_mount(repo: Path, ws_path: Path) -> tuple[Path, str] | None:
    """Return the git backend source and its container target for a jj session.

    A jj repo's git backend lives outside ``.jj/repo`` — the store records its
    location in ``.jj/repo/store/git_target`` (typically ``../../../.git``).
    When the agent runs in the *default* workspace the backend is reachable via
    the ``/workspace`` mount, but an additional workspace's store points back at
    the *main* repo's git dir, which is not otherwise mounted; without it every
    in-container ``jj`` command fails to open the repository.

    Returns ``None`` when the store has no git backend or its target cannot be
    located, so callers can simply skip the extra mount.
    """
    store_source = (repo.resolve() / ".jj" / "repo" / "store").resolve()
    git_target_file = store_source / "git_target"
    if not git_target_file.is_file():
        return None
    raw = git_target_file.read_text(encoding="utf-8").strip()
    if not raw:
        return None

    if posixpath.isabs(raw) or os.path.isabs(raw):
        source = Path(raw)
        target = raw
    else:
        source = (store_source / raw).resolve()
        # The git_target is relative to the store dir; mirror that against the
        # store's container path so the backend lands where jj will look for it.
        _, store_target = repo_store_mount(repo, ws_path)
        target = posixpath.normpath(
            posixpath.join(store_target, "store", raw.replace(os.sep, "/"))
        )

    if not source.exists():
        return None
    return source, target


@contextmanager
def _teardown_lock(repo: Path) -> Iterator[None]:
    """Serialize teardown across concurrent host project-sandbox processes.

    Several agents can share one repo's store through separate workspaces, and
    each agent's host-side teardown mutates that shared store — moving bookmarks
    and rebasing onto the default workspace's ``@``. An exclusive file lock keyed
    by the repo path keeps those teardowns from interleaving. (Concurrent writes
    from *inside* the containers are a separate, still-open problem — see the
    clone-per-subagent item in TODO.md.)
    """
    key = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:16]
    lock_path = Path(tempfile.gettempdir()) / f"project-sandbox-jj-teardown-{key}.lock"
    with open(lock_path, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def teardown(repo: Path, ws: JjWorkspace, *, after: str) -> None:
    if after == "ask":
        after = _prompt_user(ws)

    if after == "nothing":
        # Leave workspace and bookmark in place; nothing mutates the shared store.
        return

    with _teardown_lock(repo):
        ws_name = ws.path.name

        if after in ("rebase", "merge"):
            _snapshot_workspace(ws)
            # Rebase bookmark commits onto the default workspace's current @
            try:
                _jj(repo, ["rebase", "-b", ws.bookmark, "-d", "@"])
            except subprocess.CalledProcessError:
                print(f"rebase conflict — workspace left in place at {ws.path}; integrate manually")
                return
            _jj(repo, ["workspace", "forget", ws_name])
            shutil.rmtree(ws.path, ignore_errors=True)

        elif after == "pr":
            _snapshot_workspace(ws)
            try:
                _jj(repo, ["git", "push", "-b", ws.bookmark, "--allow-new"])
            except subprocess.CalledProcessError:
                print(
                    f"  Could not push '{ws.bookmark}' (is a git remote configured?). "
                    f"Workspace left in place at {ws.path}."
                )
                return
            try:
                subprocess.run(
                    ["gh", "pr", "create", "--head", ws.bookmark, "--fill"],
                    cwd=str(repo),
                    check=True,
                )
            except subprocess.CalledProcessError:
                print(
                    f"  'gh pr create' failed for bookmark '{ws.bookmark}'. "
                    f"Workspace left in place at {ws.path}; create the PR manually."
                )


def remove(repo: Path, ws: JjWorkspace) -> None:
    """Remove workspace and bookmark without integration (e.g. after a failed build)."""
    ws_name = ws.path.name
    try:
        _jj(repo, ["bookmark", "delete", ws.bookmark])
    except subprocess.CalledProcessError:
        pass
    try:
        _jj(repo, ["workspace", "forget", ws_name])
    except subprocess.CalledProcessError:
        pass
    shutil.rmtree(ws.path, ignore_errors=True)


def _bookmark_exists(repo: Path, bookmark: str) -> bool:
    try:
        out = _jj(repo, ["bookmark", "list", "--template", 'name ++ "\n"'], capture=True)
        return bookmark in out.splitlines()
    except subprocess.CalledProcessError:
        return False


def _jj(repo: Path, args: list[str], capture: bool = False) -> str:
    result = subprocess.run(
        ["jj", "-R", str(repo)] + args,
        capture_output=capture,
        text=True,
        check=True,
    )
    return result.stdout if capture else ""


def _current_revision_is_empty(repo: Path) -> bool:
    out = _jj(
        repo,
        ["log", "-r", "@", "--no-graph", "--template", 'empty ++ "\n"'],
        capture=True,
    )
    return out.strip() == "true"


def _snapshot_workspace(ws: JjWorkspace) -> None:
    _jj(ws.path, ["status"], capture=True)
    _jj(
        ws.path,
        ["bookmark", "set", "--allow-backwards", ws.bookmark, "-r", "@"],
        capture=True,
    )


def _list_workspaces(repo: Path) -> list[str]:
    try:
        out = _jj(
            repo,
            ["workspace", "list", "--template", 'root ++ "\n"'],
            capture=True,
        )
    except subprocess.CalledProcessError:
        out = _jj(repo, ["workspace", "list"], capture=True)
    paths = [line.strip() for line in out.splitlines() if line.strip()]
    if paths and all(":" not in line for line in paths):
        return paths

    # Older/default human output may include paths after "<name>: ". Fall back
    # to parsing that format if templating is unavailable.
    paths = []
    for line in out.splitlines():
        if ": " in line:
            _, _, rest = line.partition(": ")
            # Path may be followed by " (<change-id>)" — strip anything after " ("
            path = rest.split(" (")[0].strip()
            paths.append(path)
    return paths


def _prompt_user(ws: JjWorkspace) -> str:
    print(f"\n  Agent session ended. Bookmark: {ws.bookmark}")
    print(f"  Workspace: {ws.path}")
    choices = {"r": "rebase", "p": "pr", "n": "nothing"}
    while True:
        ans = input("  Integrate? [r]ebase / [p]r / [n]othing: ").strip().lower()
        if ans in choices:
            return choices[ans]
