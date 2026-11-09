"""Isolated jj sessions: a host-owned workspace plus an agent-private repository.

An isolated session runs in a jj workspace ``W`` of the main repository. Before
the container starts, the host moves ``W/.jj`` aside, initializes a second,
colocated repository over the same files with one "session base" commit, moves
that repository's ``.jj`` and ``.git`` into the private directory ``P`` and
puts the host ``.jj`` back. The container mounts ``W`` at ``/workspace`` and
``P/jj`` and ``P/git`` over ``/workspace/.jj`` and ``/workspace/.git``, so the
agent never sees the host's metadata and the main repository's ``.jj`` and
``.git`` are not mounted at all.

At the end the host snapshots ``W`` with its own metadata: everything the agent
changed becomes one commit on the session's bookmark. The private repository,
and with it the agent's own commit series, is discarded.
"""

from __future__ import annotations

import functools
import hashlib
import os
import re
import secrets
import shlex
import shutil
import subprocess
import time
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import host_locks

# Private repositories live beside the session directories, in W's parent:
# outside W (so the /workspace mount can't reach them) and outside the project.
#
# Each private repository gets a name no earlier session used
# (``<W name>.<12 hex>``), and neither W's parent nor PRIVATE_DIR_NAME is ever
# removed once created. Docker Desktop on macOS keeps a stale entry for a
# private repository directory that was mounted, removed and recreated at the
# same path, for longer than a few seconds, and the next bind from it fails
# with "bind source path does not exist". A fresh name is always visible
# (measured with Docker Desktop 29.9.3 on 2026-11-10). The same happened after
# removing and recreating PRIVATE_DIR_NAME itself.
PRIVATE_DIR_NAME = ".session-vcs"
JJ_TARGET = "/workspace/.jj"
GIT_TARGET = "/workspace/.git"
BASE_MESSAGE = "session base"
# The private repository's base commit is created by the host with the user's
# identity; it is thrown away with the repository, so it is never signed.
_PRIVATE_REPO_CONFIG = ("--config", "signing.behavior=drop")
_REFUSED_HEADER = "Refused to snapshot some files:"
# VM-backed container runtimes (every runtime on macOS) reach the host
# filesystem through a share whose metadata cache serves stale directory
# entries for about a second. A session directory or private repository
# created (or recreated at the same path, as when a bookmark is reused) just
# before the container starts can be missing from the runtime's view, and the
# launch then fails with "bind source path does not exist". Measured staleness
# is ~1.0s and expires on time alone, so wait it out with a little margin.
METADATA_CACHE_WINDOW = 1.5
_REFUSED_LINE = re.compile(r"^  (?P<path>.+): [0-9.]+\S* \([0-9]+ bytes\);")


@dataclass(slots=True)
class JjWorkspace:
    path: Path
    bookmark: str
    private: Path
    # Whether *this* session created the bookmark / workspace, as opposed to
    # reusing pre-existing ones. The build-failure cleanup (``remove``) uses these
    # to avoid destroying a prior session's work: a reused bookmark or a kept
    # workspace must survive a failed build.
    created_bookmark: bool = False
    created_workspace: bool = False
    # Monotonic time at which setup last created a directory that will be
    # mounted, or None; see wait_for_new_directories().
    created_at: float | None = None

    @property
    def private_jj(self) -> Path:
        return self.private / "jj"

    @property
    def private_git(self) -> Path:
        return self.private / "git"


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


def private_root_for(ws_path: Path) -> Path:
    """The directory holding the private repositories next to ``W``."""
    return ws_path.parent / PRIVATE_DIR_NAME


def find_private(ws_path: Path) -> Path | None:
    """Return the existing private repository directory ``P`` of ``W``.

    The private root is host-owned and never mounted, so listing it is safe.
    More than one candidate is an inconsistent leftover and is refused.
    """
    root = private_root_for(ws_path)
    pattern = re.compile(rf"^{re.escape(ws_path.name)}\.[0-9a-f]{{12}}$")
    try:
        names = sorted(name for name in os.listdir(root) if pattern.match(name))
    except FileNotFoundError:
        return None
    if len(names) > 1:
        listed = "\n".join(f"    {root / name}" for name in names)
        raise SystemExit(
            f"several private repositories exist for session directory "
            f"{ws_path}:\n{listed}\n  Remove all but the current one."
        )
    return root / names[0] if names else None


def new_private_path(ws_path: Path) -> Path:
    """A private repository path for ``W`` that no earlier session used."""
    return private_root_for(ws_path) / f"{ws_path.name}.{secrets.token_hex(6)}"


def plan(repo: Path, bookmark: str, workspace_dir: Path | None = None) -> JjWorkspace:
    """Return the session's layout without creating anything.

    A kept session's private repository is reused; otherwise a fresh path is
    chosen.
    """
    ws_path = path_for(repo, bookmark, workspace_dir=workspace_dir)
    private = find_private(ws_path) or new_private_path(ws_path)
    return JjWorkspace(path=ws_path, bookmark=bookmark, private=private)


def describe_start(repo: Path, bookmark: str, start_at: str | None) -> str:
    """Describe where a session for ``bookmark`` would start, for ``--dry-run``.

    Read-only: every jj call skips the working-copy snapshot, so the main
    checkout's uncommitted edits are judged as of its last snapshot.
    """
    if start_at:
        return f"{start_at} (--branch-start-at)"
    try:
        if _bookmark_exists(repo, bookmark, ignore_working_copy=True):
            return f"bookmark {bookmark}"
        if _is_empty(repo, "@", ignore_working_copy=True):
            return "@- (parent of the main working copy, which has no changes)"
    except (OSError, subprocess.CalledProcessError):
        return "unknown (jj could not read the repository)"
    return "@ (the main working copy, with its uncommitted changes)"


def metadata_mounts(ws: JjWorkspace) -> list[tuple[Path, str]]:
    """The two private-repository mounts of an isolated session."""
    return [(ws.private_jj, JJ_TARGET), (ws.private_git, GIT_TARGET)]


def setup(
    repo: Path,
    bookmark: str,
    start_at: str | None = None,
    workspace_dir: Path | None = None,
    shares: Sequence[str] = (),
) -> JjWorkspace:
    """Create (or resume) the isolated session directory and private repository.

    ``shares`` are project-relative directories that will be mounted into the
    session; each gets an empty placeholder in ``W`` and an exclude entry in the
    private repository. A share that the checkout already contains is refused.
    """
    repo = repo.resolve()
    # `jj workspace add` mutates shared repository state and concurrent calls
    # can corrupt the invoking working copy (jj#9314). Keep all observations
    # used to decide how to add the workspace under the same repository lock.
    with _workspace_lock(repo):
        ws = plan(repo, bookmark, workspace_dir)
        bookmark_exists = _bookmark_exists(repo, bookmark)

        # --branch-start-at pins the starting point for a NEW bookmark only;
        # reusing an existing bookmark with an explicit start point is
        # ambiguous, so reject.
        if start_at is not None and bookmark_exists:
            raise SystemExit(
                f"bookmark '{bookmark}' already exists; delete or merge it first, or "
                f"omit --branch-start-at to reuse it."
            )

        if os.path.lexists(ws.path) or os.path.lexists(ws.private):
            _check_resumable(repo, ws)
            if _prepare_shares(ws, shares):
                ws.created_at = time.monotonic()
            return ws

        try:
            _create(repo, ws, start_at=start_at, bookmark_exists=bookmark_exists)
            _prepare_shares(ws, shares)
        except BaseException:
            _discard(repo, ws)
            raise
        ws.created_at = time.monotonic()
        return ws


def wait_for_new_directories(ws: JjWorkspace) -> float:
    """Block until a VM-backed runtime can see directories setup just created.

    Returns the seconds actually spent waiting: 0.0 when setup created nothing
    or enough time has already passed (image builds and other work count
    against the window). Callers on a native filesystem (Linux containers,
    chroot) skip this entirely.
    """
    if ws.created_at is None:
        return 0.0
    remaining = METADATA_CACHE_WINDOW - (time.monotonic() - ws.created_at)
    if remaining <= 0:
        return 0.0
    time.sleep(remaining)
    return remaining


def _create(
    repo: Path, ws: JjWorkspace, *, start_at: str | None, bookmark_exists: bool
) -> None:
    ws.path.parent.mkdir(parents=True, exist_ok=True)
    add_args = ["workspace", "add"]
    if start_at:
        add_args += ["-r", start_at]
    elif bookmark_exists:
        # Reuse: start the workspace at the bookmark's commit so the agent
        # continues from where the last session left off (the bookmark is then
        # advanced at teardown), rather than off the default workspace's @.
        add_args += ["-r", ws.bookmark]
    elif not _is_empty(repo):
        add_args += ["-r", "@"]
    if _supports_no_colocate():
        # jj >= 0.46 gives a workspace added from a colocated repo its own Git
        # worktree. The session directory must have no .git of its own: the
        # private repository's .git is mounted there instead.
        add_args.append("--no-colocate")
    add_args.append(str(ws.path))
    # Set first: W didn't exist before, so a partially created one is ours too.
    ws.created_workspace = True
    _jj(repo, add_args)
    if _is_colocated(ws.path):
        raise SystemExit(
            f"jj created a colocated workspace at {ws.path}; upgrade jj to one "
            f"that supports `jj workspace add --no-colocate`."
        )

    # Point the bookmark at this workspace's working-copy commit. When reusing an
    # existing bookmark, @ was created on top of it (above), so leave it where it
    # is until teardown advances it.
    if not bookmark_exists:
        _jj(ws.path, ["bookmark", "create", ws.bookmark])
        ws.created_bookmark = True

    _build_private_repository(ws)


def _build_private_repository(ws: JjWorkspace) -> None:
    """Set up the agent's repository over ``W``, before any agent exists.

    Every command here reads only files the host itself just checked out.
    """
    parked = _parked_host_jj(ws)
    ws.private.parent.mkdir(parents=True, exist_ok=True)
    os.rename(ws.path / ".jj", parked)
    _jj_bare(["git", "init", "--colocate", str(ws.path)])
    _jj(ws.path, [*_PRIVATE_REPO_CONFIG, "commit", "-m", BASE_MESSAGE])
    ws.private.mkdir(mode=0o700)
    os.rename(ws.path / ".jj", ws.private_jj)
    os.rename(ws.path / ".git", ws.private_git)
    os.rename(parked, ws.path / ".jj")
    # The bind-mount target for the private .git (Apple `container` refuses a
    # bind onto a missing target). jj doesn't record empty directories.
    (ws.path / ".git").mkdir()


def _parked_host_jj(ws: JjWorkspace) -> Path:
    return ws.private.with_name(f".{ws.private.name}.host-jj")


def _check_resumable(repo: Path, ws: JjWorkspace) -> None:
    """Accept a kept session directory with its private repository, or explain."""
    ws_q = shlex.quote(str(ws.path))
    private_q = shlex.quote(str(ws.private))
    if not os.path.lexists(ws.path):
        raise SystemExit(
            f"a private session repository exists without its session directory: "
            f"{ws.private}\n"
            f"  It holds only the agent's own metadata. Remove it, then retry:\n"
            f"    rm -rf {private_q}"
        )
    existing = _list_workspaces(repo)
    if str(ws.path) not in existing and str(ws.path.resolve()) not in existing:
        raise SystemExit(
            f"workspace directory already exists but is not registered: {ws.path}\n"
            f"  Remove or rename it, then retry."
        )
    recover = (
        f"  Keep its work on the bookmark, then recreate it:\n"
        f"    jj -R {ws_q} bookmark set {shlex.quote(ws.bookmark)} -r @\n"
        f"    jj workspace forget {shlex.quote(ws.path.name)}\n"
        f"    rm -rf {ws_q}"
    )
    placeholder = ws.path / ".git"
    placeholder_ok = (
        placeholder.is_dir()
        and not placeholder.is_symlink()
        and not any(placeholder.iterdir())
    )
    private_ok = ws.private_jj.is_dir() and ws.private_git.is_dir()
    if placeholder_ok and private_ok:
        return
    if not os.path.lexists(ws.private) and not placeholder_ok:
        raise SystemExit(
            f"workspace {ws.path} was kept by an older project-sandbox version "
            f"and has no private repository, so it can't run in isolated mode.\n"
            f"{recover}"
        )
    if not os.path.lexists(ws.private):
        raise SystemExit(
            f"session directory {ws.path} has no private repository in "
            f"{private_root_for(ws.path)}.\n{recover}"
        )
    if not placeholder_ok:
        raise SystemExit(
            f"{placeholder} is no longer the empty placeholder the private "
            f"repository is mounted over.\n{recover}\n    rm -rf {private_q}"
        )
    raise SystemExit(
        f"the private repository of session directory {ws.path} is incomplete: "
        f"{ws.private}\n{recover}\n    rm -rf {private_q}"
    )


def _prepare_shares(ws: JjWorkspace, shares: Sequence[str]) -> bool:
    """Create each share's placeholder in ``W`` and hide it from the agent's VCS.

    Returns whether any directory was created.
    """
    created = False
    for rel in shares:
        target = ws.path
        for part in Path(rel).parts:
            target = target / part
            if target.is_symlink():
                raise SystemExit(
                    f"--share {rel}: {target} in the session's checkout is a "
                    f"symbolic link; tracked paths are already in the session."
                )
        if os.path.lexists(target):
            if not target.is_dir() or any(target.iterdir()):
                raise SystemExit(
                    f"--share {rel}: the session's checkout already contains "
                    f"{rel}. Tracked paths are already in the session; only "
                    f"directories the checkout doesn't contain can be shared."
                )
        else:
            current = ws.path
            for part in Path(rel).parts:
                current = current / part
                if not os.path.lexists(current):
                    current.mkdir()
                    created = True
                elif not current.is_dir():
                    raise SystemExit(
                        f"--share {rel}: {current} in the session's checkout is "
                        f"not a directory."
                    )
        _add_private_exclude(ws, f"/{Path(rel).as_posix()}/")
    return created


def _add_private_exclude(ws: JjWorkspace, pattern: str) -> None:
    exclude = ws.private_git / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    if pattern in existing.splitlines():
        return
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    with exclude.open("a", encoding="utf-8") as handle:
        handle.write(f"{prefix}{pattern}\n")


@contextmanager
def _workspace_lock(repo: Path) -> Generator[None, None, None]:
    """Serialize all host-side workspace mutations for one jj repository.

    Creation, finalization, and build-failure cleanup all update the shared jj
    store.  They must use the same lock: separate setup/teardown locks still
    permit ``workspace add`` to race with bookmark deletion or
    ``workspace forget``.
    """
    with host_locks.path_lock("jj-workspace", repo):
        yield


def change_id(repo: Path, bookmark: str) -> str | None:
    """Return the bookmark tip's stable jj change ID, if it can be resolved."""
    # --ignore-working-copy keeps this a pure read: it runs outside the setup/
    # teardown locks, so it must not snapshot the caller's working copy while a
    # concurrent session mutates the shared store.
    try:
        value = _jj(
            repo,
            [
                "log",
                "--ignore-working-copy",
                "--no-graph",
                "-r",
                bookmark,
                "-T",
                "change_id",
            ],
            capture=True,
        )
    except subprocess.CalledProcessError:
        return None
    return value.strip() or None


def finalize(
    repo: Path,
    ws: JjWorkspace,
    *,
    keep_workspace: bool,
    session_failed: bool,
    message: str,
) -> None:
    """Record the session's files as one commit on the bookmark, then clean up.

    Only host-owned metadata is used: ``W/.jj`` was never mounted, and the
    private repository is never read. The snapshot records the agent's edits
    to the working copy; the bookmark points at ``@`` when it holds changes
    (described with ``message`` if it has none), otherwise at ``@-``, so a
    session that changed nothing leaves the bookmark at its start.

    Unless the session failed, ``--keep-workspace`` was given, or jj refused
    to record some new files, the workspace is forgotten and ``W`` and ``P``
    are removed.

    Runs from ``main()``'s ``finally`` block, so any jj failure is caught and
    reported rather than propagated (which would mask the session's exit code):
    the workspace is left in place so no work is lost.
    """
    with _workspace_lock(repo):
        try:
            # A no-op jj command forces a working-copy snapshot of the session's
            # edits, then point the bookmark at the session tip.
            status = subprocess.run(
                ["jj", "-R", str(ws.path), "status"],
                capture_output=True,
                text=True,
                check=True,
            )
            refused = refused_paths(status.stderr)
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

        print(f"Session result: bookmark '{ws.bookmark}'")
        if refused:
            print(
                "[W] jj did not record these new files, because they exceed "
                "snapshot.max-new-file-size:"
            )
            for path in refused:
                print(f"  {path}")
            print(f"session directory kept at {ws.path}")
            return
        if session_failed:
            print(f"session failed — workspace left in place at {ws.path}")
            return
        if keep_workspace:
            print(f"workspace kept at {ws.path} (bookmark '{ws.bookmark}')")
            return

        try:
            _jj(repo, ["workspace", "forget", ws.path.name])
        except subprocess.CalledProcessError:
            print(
                f"could not forget workspace {ws.path.name}; workspace left in "
                f"place at {ws.path}"
            )
            return
        shutil.rmtree(ws.path, ignore_errors=True)
        # The parent directories stay; see PRIVATE_DIR_NAME.
        shutil.rmtree(ws.private, ignore_errors=True)


def refused_paths(stderr: str) -> list[str]:
    """Return the paths jj refused to snapshot, from its warning on stderr."""
    paths: list[str] = []
    in_block = False
    for line in stderr.splitlines():
        if _REFUSED_HEADER in line:
            in_block = True
            continue
        if not in_block:
            continue
        match = _REFUSED_LINE.match(line)
        if match is None:
            in_block = False
            continue
        if match.group("path") not in paths:
            paths.append(match.group("path"))
    return paths


def remove(repo: Path, ws: JjWorkspace) -> None:
    """Drop artifacts this session created, for the build-failure path (agent
    never ran). Only freshly-created artifacts are removed: a reused bookmark or
    a kept, reused workspace is left in place so a prior session's work is never
    destroyed by a failed build.
    """
    with _workspace_lock(repo):
        _discard(repo, ws)


def _discard(repo: Path, ws: JjWorkspace) -> None:
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
        shutil.rmtree(ws.private, ignore_errors=True)
        shutil.rmtree(_parked_host_jj(ws), ignore_errors=True)


@functools.cache
def _supports_no_colocate() -> bool:
    """Whether the host jj accepts ``workspace add --no-colocate`` (jj >= 0.46)."""
    try:
        result = subprocess.run(
            ["jj", "workspace", "add", "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    return "--no-colocate" in result.stdout


def _is_colocated(ws_path: Path) -> bool:
    """Whether the workspace has its own Git worktree (a ``.git`` file or dir)."""
    return os.path.lexists(ws_path / ".git")


def _bookmark_exists(
    repo: Path, bookmark: str, *, ignore_working_copy: bool = False
) -> bool:
    args = ["bookmark", "list", "--template", 'name ++ "\n"']
    if ignore_working_copy:
        args.insert(0, "--ignore-working-copy")
    try:
        out = _jj(repo, args, capture=True)
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


def _jj_bare(args: list[str]) -> None:
    subprocess.run(["jj", *args], capture_output=True, text=True, check=True)


def _is_empty(repo: Path, rev: str = "@", *, ignore_working_copy: bool = False) -> bool:
    args = ["log", "-r", rev, "--no-graph", "--template", 'empty ++ "\n"']
    if ignore_working_copy:
        args.insert(0, "--ignore-working-copy")
    out = _jj(repo, args, capture=True)
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
