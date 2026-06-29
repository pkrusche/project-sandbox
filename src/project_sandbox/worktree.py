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


def setup(repo: Path, branch: str, base: str | None = None, worktree_dir: Path | None = None) -> Worktree:
    repo = repo.resolve()
    wt_path = path_for(repo, branch, worktree_dir=worktree_dir)

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

    branches = _git(repo, ["branch", "--list", branch], capture=True)
    branch_exists = branch.strip() in branches

    if branch_exists:
        _git(repo, ["worktree", "add", str(wt_path), branch])
        return Worktree(path=wt_path, branch=branch)

    base_ref = base or "HEAD"
    _git(repo, ["worktree", "add", "-b", branch, str(wt_path), base_ref])
    return Worktree(path=wt_path, branch=branch)


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


def teardown(repo: Path, wt: Worktree, *, after: str) -> None:
    if after == "ask":
        after = _prompt_user(wt)

    if after == "nothing":
        # Leave worktree and branch in place; nothing mutates the shared repo.
        return

    with _teardown_lock(repo):
        if after in ("merge", "rebase"):
            _clear_stale_index_lock(repo, wt)

        if after == "merge":
            try:
                _git(repo, ["merge", "--no-ff", wt.branch, "-m", f"Merge agent session: {wt.branch}"])
            except subprocess.CalledProcessError:
                subprocess.run(["git", "-C", str(repo), "merge", "--abort"], check=False, capture_output=True)
                print(f"merge conflict — worktree left in place at {wt.path}; integrate manually")
                return
        elif after == "rebase":
            current = _git(repo, ["rev-parse", "--abbrev-ref", "HEAD"], capture=True).strip()
            try:
                subprocess.run(
                    ["git", "-C", str(wt.path), "rebase", current],
                    check=True,
                )
                _git(repo, ["merge", "--ff-only", wt.branch])
            except subprocess.CalledProcessError:
                subprocess.run(["git", "-C", str(wt.path), "rebase", "--abort"], check=False, capture_output=True)
                print(f"merge conflict — worktree left in place at {wt.path}; integrate manually")
                return
        elif after == "pr":
            try:
                _git(repo, ["push", "-u", "origin", wt.branch])
            except subprocess.CalledProcessError:
                print(
                    f"  Could not push '{wt.branch}' to 'origin' (is a remote configured?). "
                    f"Worktree left in place at {wt.path}."
                )
                return
            try:
                subprocess.run(
                    ["gh", "pr", "create", "--head", wt.branch, "--fill"],
                    cwd=str(repo),
                    check=True,
                )
            except subprocess.CalledProcessError:
                print(
                    f"  'gh pr create' failed for branch '{wt.branch}'. "
                    f"Worktree left in place at {wt.path}; create the PR manually."
                )
                return

        if after in ("merge", "rebase"):
            _git(repo, ["worktree", "remove", "--force", str(wt.path)])


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


def _prompt_user(wt: Worktree) -> str:
    print(f"\n  Agent session ended. Branch: {wt.branch}")
    print(f"  Worktree: {wt.path}")
    choices = {"m": "merge", "r": "rebase", "p": "pr", "n": "nothing"}
    while True:
        ans = input("  Integrate? [m]erge / [r]ebase / [p]r / [n]othing: ").strip().lower()
        if ans in choices:
            return choices[ans]
