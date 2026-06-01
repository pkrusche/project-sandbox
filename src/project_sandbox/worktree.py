from dataclasses import dataclass
from pathlib import Path
import subprocess


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
    return wt_root / branch.replace("/", "-")


def teardown(repo: Path, wt: Worktree, *, after: str) -> None:
    if after == "ask":
        after = _prompt_user(wt)

    if after in ("merge", "rebase"):
        _clear_stale_index_lock(repo, wt)

    if after == "merge":
        _git(repo, ["merge", "--no-ff", wt.branch, "-m", f"Merge agent session: {wt.branch}"])
    elif after == "rebase":
        current = _git(repo, ["rev-parse", "--abbrev-ref", "HEAD"], capture=True).strip()
        subprocess.run(
            ["git", "-C", str(wt.path), "rebase", current],
            check=True,
        )
        _git(repo, ["merge", "--ff-only", wt.branch])
    elif after == "pr":
        _git(repo, ["push", "-u", "origin", wt.branch])
        subprocess.run(["gh", "pr", "create", "--head", wt.branch, "--fill"], check=False)

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
