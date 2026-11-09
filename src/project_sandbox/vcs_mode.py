"""Which version-control mode a session runs in, and what that mode mounts.

* ``isolated`` (the default in jj projects): the agent works in a separate jj
  workspace with a private throwaway repository; the host records the result
  as one commit on the session's bookmark (see ``jj_workspace``).
* ``shared`` (``--vcs shared``): the project is mounted in place with its real,
  writable ``.git`` and ``.jj``.
* ``none``: a directory without a repository is mounted in place.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import jj_workspace as jj_workspace_mod

ISOLATED = "isolated"
SHARED = "shared"
NONE = "none"
MODES = (ISOLATED, SHARED)
GENERATED_BOOKMARK_PREFIX = "sandbox/"

SHARED_WARNING = (
    "[!] Shared VCS mode: the agent can change this repository's .git and .jj, "
    "including hooks and config that your host git and jj run later. Review "
    "them after the session, or use isolated mode (the default)."
)

# Paths a --share can't be, lie inside or contain: repository metadata, the
# generated sandbox files, the devcontainer, and config that host tools run.
_PROTECTED_SHARE_PATHS = (
    ".git",
    ".jj",
    ".devcontainer",
    ".project-sandbox",
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".mcp.json",
    ".codex",
    ".pi",
    ".opencode",
    "opencode.json",
    "opencode.jsonc",
    ".vscode/tasks.json",
    ".vscode/settings.json",
    ".vscode/launch.json",
    ".idea/runConfigurations",
    ".run",
)


@dataclass(frozen=True)
class Share:
    """A project directory mounted at the same place under ``/workspace``."""

    rel: str
    writable: bool
    source: Path | None = None

    @property
    def target(self) -> str:
        return f"/workspace/{self.rel}"


@dataclass(frozen=True)
class VcsPlan:
    mode: str
    # What is mounted at /workspace: the session directory or the project.
    workspace: Path
    ws: jj_workspace_mod.JjWorkspace | None = None
    generated_bookmark: bool = False
    start: str | None = None
    shares: tuple[Share, ...] = field(default=())

    @property
    def bookmark(self) -> str | None:
        return self.ws.bookmark if self.ws is not None else None

    @property
    def writable_shares(self) -> tuple[Share, ...]:
        return tuple(share for share in self.shares if share.writable)


def parse_share(value: str) -> Share:
    """Parse ``PATH[:rw|:ro]`` into a normalized project-relative share."""
    path, writable = value, False
    head, sep, mode = value.rpartition(":")
    if sep and mode in ("rw", "ro"):
        path, writable = head, mode == "rw"
    if not path:
        raise SystemExit(f"--share {value}: missing path")
    if any(ord(ch) < 32 or ch in ",\"'" for ch in path):
        raise SystemExit(
            f"--share {value}: the path contains a comma, quote or control character"
        )
    pure = PurePosixPath(path)
    if pure.is_absolute() or path.startswith("~"):
        raise SystemExit(f"--share {value}: the path must be relative to the project")
    if ".." in pure.parts:
        raise SystemExit(f"--share {value}: the path must not contain '..'")
    rel = pure.as_posix().strip("/")
    if rel in ("", "."):
        raise SystemExit(f"--share {value}: the project itself can't be shared")
    _reject_protected(rel, value)
    return Share(rel=rel, writable=writable)


def _reject_protected(rel: str, value: str) -> None:
    parts = PurePosixPath(rel).parts
    for protected in _PROTECTED_SHARE_PATHS:
        p_parts = PurePosixPath(protected).parts
        shorter = min(len(parts), len(p_parts))
        if parts[:shorter] == p_parts[:shorter]:
            raise SystemExit(
                f"--share {value}: {protected} can't be shared (repository "
                f"metadata, generated sandbox files, the devcontainer and "
                f"host-executed config are excluded)"
            )


def validate_args(args, run_agent: str | None) -> None:
    """Checks that need no project directory."""
    for flag, value in (
        ("--vcs", args.vcs),
        ("--branch", args.branch),
        ("--share", args.shares),
    ):
        if value and args.build_only:
            raise SystemExit(f"--build-only cannot be combined with {flag}")
        if value and run_agent is None:
            raise SystemExit(f"{flag} requires --agent, --prompt, or --prompt-text")
    if args.vcs == SHARED and args.branch:
        raise SystemExit(
            "--branch names an isolated session's bookmark; it can't be combined "
            "with --vcs shared"
        )
    if args.branch_start_at and not args.branch:
        raise SystemExit("--branch-start-at requires --branch")
    if args.keep_workspace and args.vcs == SHARED:
        raise SystemExit("--keep-workspace requires an isolated session")
    for value in args.shares:
        parse_share(value)


def plan(
    args, project: Path, *, session_id: str, describe_start: bool = False
) -> VcsPlan:
    """Choose the mode per design Decision 4 and lay out the session.

    Non-mutating. ``describe_start`` additionally asks jj (read-only) where a
    new session would start, for ``--dry-run``.
    """
    has_jj = (project / ".jj").is_dir()
    has_git = os.path.lexists(project / ".git")
    shares = tuple(parse_share(value) for value in args.shares)

    if not has_jj and not has_git:
        if args.vcs or args.branch:
            flag = "--vcs" if args.vcs else "--branch"
            raise SystemExit(
                f"{flag} needs a repository at the project root, but {project} "
                f"has neither .jj nor .git."
            )
        if args.keep_workspace:
            raise SystemExit("--keep-workspace requires an isolated session")
        return VcsPlan(mode=NONE, workspace=project, shares=shares)

    if args.vcs == SHARED:
        return VcsPlan(mode=SHARED, workspace=project, shares=shares)

    if not has_jj:
        if args.vcs == ISOLATED or args.branch:
            raise SystemExit(
                f"Isolated sessions need a jj repository. {project} is a Git "
                f"repository without jj; add jj with:\n"
                f"    jj git init --colocate {project}"
            )
        raise SystemExit(
            f"{project} is a Git repository without jj. Sessions run isolated "
            f"by default, which needs jj. Either add jj:\n"
            f"    jj git init --colocate {project}\n"
            f"or mount the project in place, with its .git writable by the agent:\n"
            f"    --vcs shared"
        )

    generated = not args.branch
    bookmark = args.branch or f"{GENERATED_BOOKMARK_PREFIX}{session_id}"
    workspace_dir = Path(args.worktree_dir) if args.worktree_dir else None
    ws = jj_workspace_mod.plan(project, bookmark, workspace_dir)
    start = (
        jj_workspace_mod.describe_start(project, bookmark, args.branch_start_at)
        if describe_start
        else None
    )
    return VcsPlan(
        mode=ISOLATED,
        workspace=ws.path,
        ws=ws,
        generated_bookmark=generated,
        start=start,
        shares=validate_shares(project, shares),
    )


def validate_shares(project: Path, shares: Sequence[Share]) -> tuple[Share, ...]:
    """Check each share against the project before anything is created."""
    root = project.resolve()
    checked: list[Share] = []
    for share in shares:
        label = share.rel + (":rw" if share.writable else "")
        candidate = project / share.rel
        if not os.path.lexists(candidate):
            raise SystemExit(f"--share {label}: {candidate} does not exist")
        resolved = candidate.resolve()
        if resolved != root and root not in resolved.parents:
            raise SystemExit(
                f"--share {label}: {candidate} resolves outside the project "
                f"({resolved})"
            )
        if not resolved.is_dir():
            raise SystemExit(
                f"--share {label}: only directories can be shared; share the "
                f"parent directory instead"
            )
        rel_resolved = resolved.relative_to(root).as_posix()
        if rel_resolved in ("", "."):
            raise SystemExit(f"--share {label}: the project itself can't be shared")
        _reject_protected(rel_resolved, label)
        if any(other.rel == share.rel for other in checked):
            raise SystemExit(f"--share {label}: given more than once")
        checked.append(Share(rel=share.rel, writable=share.writable, source=resolved))
    for share in checked:
        for other in checked:
            if share is not other and PurePosixPath(other.rel).is_relative_to(
                PurePosixPath(share.rel)
            ):
                raise SystemExit(
                    f"--share {other.rel} lies inside --share {share.rel}; share "
                    f"only one of them"
                )
    return tuple(checked)


def mounts(plan: VcsPlan) -> list[str]:
    """The metadata and share mounts of an isolated session, in mount order."""
    if plan.mode != ISOLATED or plan.ws is None:
        return []
    values = [
        f"type=bind,source={source},target={target}"
        for source, target in jj_workspace_mod.metadata_mounts(plan.ws)
    ]
    for share in plan.shares:
        assert share.source is not None
        values.append(
            f"type=bind,source={share.source},target={share.target}"
            + ("" if share.writable else ",readonly")
        )
    return values


def protected_host_paths(plan: VcsPlan, project: Path) -> list[Path]:
    """Host paths no extra --mount may expose in this plan."""
    if plan.mode != ISOLATED or plan.ws is None:
        return []
    return [
        (project / ".jj").resolve(),
        (project / ".git").resolve(),
        plan.ws.path / ".jj",
        plan.ws.private.parent,
    ]


def protected_targets(plan: VcsPlan) -> list[PurePosixPath]:
    """Container paths no extra --mount may cover in this plan."""
    if plan.mode != ISOLATED:
        return []
    return [
        PurePosixPath(jj_workspace_mod.JJ_TARGET),
        PurePosixPath(jj_workspace_mod.GIT_TARGET),
    ]
