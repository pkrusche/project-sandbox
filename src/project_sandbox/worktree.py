import fcntl
import hashlib
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Worktree:
    path: Path
    branch: str


def setup(repo: Path, branch: str, start_at: str | None = None, worktree_dir: Path | None = None) -> Worktree:
    repo = repo.resolve()
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
            return Worktree(path=wt_path, branch=branch)
        raise SystemExit(
            f"worktree directory already exists but is not registered: {wt_path}\n"
            f"  Remove or rename it, then retry."
        )

    if _branch_exists(repo, branch):
        _git(repo, ["worktree", "add", str(wt_path), branch])
        return Worktree(path=wt_path, branch=branch)

    base_ref = start_at or "HEAD"
    _git(repo, ["worktree", "add", "-b", branch, str(wt_path), base_ref])
    return Worktree(path=wt_path, branch=branch)


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
def _teardown_lock(repo: Path) -> Iterator[None]:
    """Serialize teardown across concurrent host project-sandbox processes.

    Several agents can share one repo through separate worktrees, and each
    agent's host-side teardown mutates that shared repo — merging or rebasing
    branches into the main checkout's HEAD, pushing, and removing worktrees. An
    exclusive file lock keyed by the repo path keeps those teardowns from
    interleaving. (Concurrent writes from *inside* the containers are a
    separate, still-open problem — see the clone-per-subagent item in TODO.md.)
    """
    key = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:16]
    lock_path = Path(tempfile.gettempdir()) / f"project-sandbox-git-teardown-{key}.lock"
    with open(lock_path, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


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
    with _teardown_lock(repo):
        _clear_stale_index_lock(repo, wt)
        try:
            if _is_dirty(wt):
                _git(wt.path, ["add", "-A"])
                _git(wt.path, ["commit", "-m", message])
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
            _git(repo, ["worktree", "remove", "--force", str(wt.path)])
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
        line[len(_WORKTREE_PREFIX):]
        for line in out.splitlines()
        if line.startswith(_WORKTREE_PREFIX)
    ]


def _clear_stale_index_lock(repo: Path, wt: Worktree) -> None:
    # A container crash mid-commit may leave index.lock in the worktree metadata.
    # Remove it so the host-side merge/rebase can proceed.
    git_dir = repo.resolve() / ".git"
    wt_name = wt.path.name
    lock = git_dir / "worktrees" / wt_name / "index.lock"
    if lock.exists():
        lock.unlink()
