import hashlib
import os
import subprocess
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import host_locks
from .workspace_lease import WorkspaceLease

# VM-backed container runtimes (every runtime on macOS) reach the host
# filesystem through a share whose metadata cache serves stale directory
# entries for about a second. That matters here because git deletes
# ``.git/worktrees`` itself once the repo's last worktree is removed: the next
# ``git worktree add`` recreates the directory with a new inode, and a
# container started inside the cache window mounts the *deleted* one. The new
# worktree's gitdir is then invisible in the container, and because a ``.git``
# file pointing at a missing gitdir is fatal to git, every in-container git
# command — including the entrypoint's ``git config --global`` provisioning —
# dies with "fatal: not a git repository: <gitdir>" before the agent starts.
# Measured staleness is ~1.0s and expires on time alone (re-reading does not
# refresh it), so wait it out with a little margin.
METADATA_CACHE_WINDOW = 1.5


class GitIndexLockedError(RuntimeError):
    """An external index transaction needs manual inspection before reuse."""


@dataclass(slots=True)
class Worktree:
    path: Path
    branch: str
    # Monotonic timestamp of the moment this process created the repo's
    # ``.git/worktrees`` directory, or None when the directory already existed
    # (nothing was recreated, so no cache entry can be stale). Consumed by
    # wait_for_metadata_visibility() before a container mounts the metadata.
    metadata_created_at: float | None = None
    lease: WorkspaceLease | None = field(default=None, repr=False, compare=False)


def setup(
    repo: Path,
    branch: str,
    start_at: str | None = None,
    worktree_dir: Path | None = None,
) -> Worktree:
    repo = repo.resolve()
    lease = WorkspaceLease(
        "git", repo, branch, path_for(repo, branch, worktree_dir=worktree_dir)
    )
    try:
        with _workspace_lock(repo):
            wt = _setup_locked(repo, branch, start_at, worktree_dir)
            wt.lease = lease
            return wt
    except GitIndexLockedError as exc:
        lease.close()
        raise SystemExit(str(exc)) from exc
    except BaseException:
        lease.close()
        raise


def _setup_locked(
    repo: Path, branch: str, start_at: str | None, worktree_dir: Path | None
) -> Worktree:
    wt_path = path_for(repo, branch, worktree_dir=worktree_dir)

    # --branch-start-at pins the starting point for a NEW branch only; reusing an
    # existing branch with an explicit start point is ambiguous, so reject it.
    if start_at is not None and _branch_exists(repo, branch):
        raise SystemExit(
            f"branch '{branch}' already exists; delete or merge it first, or omit "
            f"--branch-start-at to reuse it."
        )

    if wt_path.exists():
        _git(repo, ["worktree", "prune"])
        existing = _list_worktrees(repo)
        # git reports resolved absolute paths; wt_path may still contain symlinks
        # (e.g. /tmp -> /private/tmp on macOS), so compare both forms.
        if str(wt_path) in existing or str(wt_path.resolve()) in existing:
            _check_index_lock(wt_path)
            return Worktree(path=wt_path, branch=branch)
        raise SystemExit(
            f"worktree directory already exists but is not registered: {wt_path}\n"
            f"  Remove or rename it, then retry."
        )

    # git removes .git/worktrees when the last worktree goes away, so the add
    # below may recreate it; see METADATA_CACHE_WINDOW.
    metadata_dir_existed = _metadata_dir(repo).is_dir()

    if _branch_exists(repo, branch):
        _git(repo, ["worktree", "add", str(wt_path), branch])
    else:
        base_ref = start_at or "HEAD"
        _git(repo, ["worktree", "add", "-b", branch, str(wt_path), base_ref])

    return Worktree(
        path=wt_path,
        branch=branch,
        metadata_created_at=None if metadata_dir_existed else time.monotonic(),
    )


def wait_for_metadata_visibility(wt: Worktree) -> float:
    """Block until a VM-backed runtime can see this worktree's gitdir.

    Returns the seconds actually spent waiting, which is 0.0 whenever nothing
    was recreated or enough time has already passed — the timestamp is taken
    when the directory is created, so image builds and other setup work count
    against the window rather than adding to it. Callers on a native
    filesystem (Linux containers, chroot) should skip this entirely.
    """
    if wt.metadata_created_at is None:
        return 0.0
    remaining = METADATA_CACHE_WINDOW - (time.monotonic() - wt.metadata_created_at)
    if remaining <= 0:
        return 0.0
    time.sleep(remaining)
    return remaining


def _metadata_dir(repo: Path) -> Path:
    common_dir = _git(
        repo, ["rev-parse", "--path-format=absolute", "--git-common-dir"], capture=True
    ).strip()
    return Path(common_dir) / "worktrees"


def _branch_exists(repo: Path, branch: str) -> bool:
    branches = _git(repo, ["branch", "--list", branch], capture=True)
    return branch.strip() in branches


def path_for(repo: Path, branch: str, worktree_dir: Path | None = None) -> Path:
    repo = repo.resolve()
    wt_root = worktree_dir or (repo.parent / f"{repo.name}-worktrees")
    safe = branch.replace("/", "-")
    if "/" in branch:
        suffix = hashlib.sha256(branch.encode()).hexdigest()[:6]
        safe = f"{safe}-{suffix}"
    return wt_root / safe


@contextmanager
def _workspace_lock(repo: Path) -> Generator[None, None, None]:
    """Serialize host setup and finalization mutations for one repository."""
    # Keep the old lock name to coordinate with older running finalizers too.
    with host_locks.path_lock("git-teardown", repo):
        yield


def finalize(
    repo: Path,
    wt: Worktree,
    *,
    keep_workspace: bool,
    session_failed: bool,
    message: str,
) -> None:
    """Capture the session's work on the branch, then remove the worktree.

    The single after-session action never integrates into the main checkout: it
    commits any uncommitted work onto ``wt.branch`` (so ``worktree remove
    --force`` cannot discard it) and, unless the session failed or the caller
    asked to keep it, removes the worktree. The branch retains the commits for
    the user to merge or open a PR from manually.

    Runs from ``main()``'s ``finally`` block, so any git failure is caught and
    reported rather than propagated (which would mask the session's exit code):
    the worktree is left in place so no work is lost.
    """
    if wt.lease is None or not wt.lease.active:
        print(f"workspace lease is not active — worktree left in place at {wt.path}")
        return
    with wt.lease, _workspace_lock(repo):
        try:
            _check_index_lock(wt.path)
            if _is_dirty(wt):
                _git(wt.path, ["add", "-A"])
                _git(wt.path, ["commit", "-m", message])
        except GitIndexLockedError as exc:
            print(str(exc))
            return
        except subprocess.CalledProcessError:
            print(
                f"could not commit session changes — worktree left in place at {wt.path}"
            )
            return

        if session_failed:
            print(f"session failed — worktree left in place at {wt.path}")
            return
        if keep_workspace:
            print(f"worktree kept at {wt.path} (branch '{wt.branch}')")
            return

        try:
            _check_index_lock(wt.path)
            _git(repo, ["worktree", "remove", "--force", str(wt.path)])
        except GitIndexLockedError as exc:
            print(str(exc))
        except subprocess.CalledProcessError:
            print(
                f"could not remove worktree at {wt.path}; remove it manually with "
                f"`git worktree remove --force`."
            )


def _is_dirty(wt: Worktree) -> bool:
    return bool(_git(wt.path, ["status", "--porcelain"], capture=True).strip())


def _git(repo: Path, args: list[str], capture: bool = False) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo)] + args,
        capture_output=capture,
        text=True,
        check=True,
    )
    return result.stdout if capture else ""


_WORKTREE_PREFIX = "worktree "


def _list_worktrees(repo: Path) -> list[str]:
    out = _git(repo, ["worktree", "list", "--porcelain"], capture=True)
    # Porcelain lines look like "worktree <path>"; the path may contain spaces, so
    # strip the fixed prefix rather than splitting on whitespace.
    return [
        line[len(_WORKTREE_PREFIX) :]
        for line in out.splitlines()
        if line.startswith(_WORKTREE_PREFIX)
    ]


def _check_index_lock(path: Path) -> None:
    # Worktree metadata names can differ from directory basenames. Ask Git for
    # its actual index path, including relocated metadata and custom indexes.
    index = _git(
        path,
        ["rev-parse", "--path-format=absolute", "--git-path", "index"],
        capture=True,
    ).strip()
    lock = Path(f"{index}.lock")
    if os.path.lexists(lock):
        raise GitIndexLockedError(
            f"Git index lock exists at {lock}; worktree left in place at {path}.\n"
            "  Stop any Git process, editor, or container using this worktree. "
            "Only after verifying the lock is stale, remove it manually and retry."
        )
