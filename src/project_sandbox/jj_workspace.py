from __future__ import annotations
import hashlib
import shutil
import subprocess
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
    _jj(repo, ["workspace", "add", str(ws_path)])

    if base:
        _jj(ws_path, ["new", base])

    # Create bookmark at the workspace's working-copy commit
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


def teardown(repo: Path, ws: JjWorkspace, *, after: str) -> None:
    if after == "ask":
        after = _prompt_user(ws)

    ws_name = ws.path.name

    if after in ("rebase", "merge"):
        # Rebase bookmark commits onto the default workspace's current @
        try:
            _jj(repo, ["rebase", "-b", ws.bookmark, "-d", "@"])
        except subprocess.CalledProcessError:
            print(f"rebase conflict — workspace left in place at {ws.path}; integrate manually")
            return
        _jj(repo, ["workspace", "forget", ws_name])
        shutil.rmtree(ws.path, ignore_errors=True)

    elif after == "pr":
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

    # "nothing": leave workspace and bookmark in place


def _jj(repo: Path, args: list[str], capture: bool = False) -> str:
    result = subprocess.run(
        ["jj", "-R", str(repo)] + args,
        capture_output=capture,
        text=True,
        check=True,
    )
    return result.stdout if capture else ""


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
