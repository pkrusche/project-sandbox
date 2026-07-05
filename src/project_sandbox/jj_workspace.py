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
    # Whether *this* session created the bookmark / workspace, as opposed to
    # reusing pre-existing ones. The build-failure cleanup (``remove``) uses these
    # to avoid destroying a prior session's work: a reused bookmark or a kept
    # workspace must survive a failed build, just as a git branch/worktree does.
    created_bookmark: bool = False
    created_workspace: bool = False


def setup(
    repo: Path,
    bookmark: str,
    start_at: str | None = None,
    workspace_dir: Path | None = None,
) -> JjWorkspace:
    repo = repo.resolve()
    ws_path = path_for(repo, bookmark, workspace_dir=workspace_dir)

    bookmark_exists = _bookmark_exists(repo, bookmark)

    # --branch-start-at pins the starting point for a NEW bookmark only; reusing
    # an existing bookmark with an explicit start point is ambiguous, so reject.
    if start_at is not None and bookmark_exists:
        raise SystemExit(
            f"bookmark '{bookmark}' already exists; delete or merge it first, or "
            f"omit --branch-start-at to reuse it."
        )

    if ws_path.exists():
        existing = _list_workspaces(repo)
        if str(ws_path) in existing or str(ws_path.resolve()) in existing:
            # Reusing a kept workspace and its bookmark; created nothing.
            return JjWorkspace(path=ws_path, bookmark=bookmark)
        raise SystemExit(
            f"workspace directory already exists but is not registered: {ws_path}\n"
            f"  Remove or rename it, then retry."
        )

    ws_path.parent.mkdir(parents=True, exist_ok=True)
    add_args = ["workspace", "add"]
    if start_at:
        add_args += ["-r", start_at]
    elif bookmark_exists:
        # Reuse: start the workspace at the bookmark's commit so the agent
        # continues from where the last session left off (the bookmark is then
        # advanced to @ at teardown), rather than off the default workspace's @.
        add_args += ["-r", bookmark]
    elif not _is_empty(repo):
        add_args += ["-r", "@"]
    add_args.append(str(ws_path))
    _jj(repo, add_args)

    # Point the bookmark at this workspace's working-copy commit. When reusing an
    # existing bookmark, @ was created on top of it (above), so leave it where it
    # is until teardown advances it.
    if not bookmark_exists:
        _jj(ws_path, ["bookmark", "create", bookmark])

    return JjWorkspace(
        path=ws_path,
        bookmark=bookmark,
        created_bookmark=not bookmark_exists,
        created_workspace=True,
    )


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


def finalize(
    repo: Path,
    ws: JjWorkspace,
    *,
    keep_workspace: bool,
    session_failed: bool,
    message: str,
) -> None:
    """Capture the session's work on the bookmark, then remove the workspace.

    The single after-session action never integrates into the default
    workspace: it snapshots the working copy and advances the bookmark to the
    session's tip. Unless the session failed or the caller asked to keep it, the
    workspace is then forgotten and removed. The bookmark retains the work for
    the user to rebase or push manually.

    The tip is @ when it holds changes (described with ``message`` if it has no
    message of its own). When @ is empty — the agent committed and left an empty
    working copy, or made no changes at all — the bookmark is advanced to @-
    instead, so any committed work is still captured without leaving an empty
    commit as the bookmark tip.

    Runs from ``main()``'s ``finally`` block, so any jj failure is caught and
    reported rather than propagated (which would mask the session's exit code):
    the workspace is left in place so no work is lost.
    """
    with _teardown_lock(repo):
        ws_name = ws.path.name

        try:
            # A no-op jj command forces a working-copy snapshot of the session's
            # edits, then point the bookmark at the session tip.
            _jj(ws.path, ["status"], capture=True)
            if _is_empty(ws.path, "@"):
                target = "@-"
            else:
                target = "@"
                if not _description(ws.path, target):
                    _jj(ws.path, ["describe", "-r", target, "-m", message])
            _jj(
                ws.path,
                ["bookmark", "set", "--allow-backwards", ws.bookmark, "-r", target],
            )
        except subprocess.CalledProcessError:
            print(
                f"could not capture session changes — workspace left in place at {ws.path}"
            )
            return

        if session_failed:
            print(f"session failed — workspace left in place at {ws.path}")
            return
        if keep_workspace:
            print(f"workspace kept at {ws.path} (bookmark '{ws.bookmark}')")
            return

        try:
            _jj(repo, ["workspace", "forget", ws_name])
        except subprocess.CalledProcessError:
            print(
                f"could not forget workspace {ws_name}; workspace left in place at {ws.path}"
            )
            return
        shutil.rmtree(ws.path, ignore_errors=True)


def remove(repo: Path, ws: JjWorkspace) -> None:
    """Drop artifacts this session created, for the build-failure path (agent
    never ran). Only freshly-created artifacts are removed: a reused bookmark or
    a kept, reused workspace is left in place so a prior session's work is never
    destroyed by a failed build — matching how a git branch/worktree survives.
    """
    if ws.created_bookmark:
        try:
            _jj(repo, ["bookmark", "delete", ws.bookmark])
        except subprocess.CalledProcessError:
            pass
    if ws.created_workspace:
        try:
            _jj(repo, ["workspace", "forget", ws.path.name])
        except subprocess.CalledProcessError:
            pass
        shutil.rmtree(ws.path, ignore_errors=True)


def _bookmark_exists(repo: Path, bookmark: str) -> bool:
    try:
        out = _jj(
            repo, ["bookmark", "list", "--template", 'name ++ "\n"'], capture=True
        )
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


def _is_empty(repo: Path, rev: str = "@") -> bool:
    out = _jj(
        repo,
        ["log", "-r", rev, "--no-graph", "--template", 'empty ++ "\n"'],
        capture=True,
    )
    return out.strip() == "true"


def _description(ws_path: Path, rev: str = "@") -> str:
    out = _jj(
        ws_path,
        ["log", "-r", rev, "--no-graph", "--template", "description"],
        capture=True,
    )
    return out.strip()


def _list_workspaces(repo: Path) -> list[str]:
    """Return the absolute root path of every workspace registered on ``repo``.

    ``--template 'root ++ "\\n"'`` renders exactly one line per workspace with
    nothing but the path, so a successful call is unambiguous: every line
    *is* a path, whatever characters (including ``:``) it happens to
    contain. Only when the template call itself fails outright — e.g. a jj
    version too old to know the ``root`` keyword for ``workspace list`` — do
    we fall back to parsing the human-readable ``<name>: ...`` format. Which
    branch ran is decided by whether the subprocess call raised, never by
    scanning the output for incidental characters like ``:``.
    """
    try:
        out = _jj(
            repo,
            ["workspace", "list", "--template", 'root ++ "\n"'],
            capture=True,
        )
    except subprocess.CalledProcessError:
        out = _jj(repo, ["workspace", "list"], capture=True)
        # Older/default human output may include paths after "<name>: ". Fall
        # back to parsing that format since templating is unavailable.
        paths = []
        for line in out.splitlines():
            if ": " in line:
                _, _, rest = line.partition(": ")
                # Path may be followed by " (<change-id>)" — strip anything after " ("
                path = rest.split(" (")[0].strip()
                paths.append(path)
        return paths

    return [line.strip() for line in out.splitlines() if line.strip()]
