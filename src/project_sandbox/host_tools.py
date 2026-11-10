"""Find and run host tools without trusting the project or the caller's cwd.

A launch runs several tools on the host: ``jj``, ``git``, the container runtime,
agent CLIs for token refresh, ``pass``, ``gh`` and ``security``. A repository
an agent can write must not choose which program runs, nor what it reads from
its working directory:

- :func:`resolve` looks a tool up only through absolute ``PATH`` entries that
  lie outside the project and every session directory (the *untrusted roots*).
  An empty or relative entry would make the caller's directory, usually the
  project, a lookup location. A tool found only through a skipped entry is
  missing.
- :func:`run` executes the resolved path from :func:`neutral_dir`, a private
  empty directory, so a tool that loads per-directory settings (Claude's
  ``.claude/settings.json``, Codex's ``.codex/``) finds none.
- :func:`restricted_path` narrows the process ``PATH`` to the trusted entries
  for the duration of a launch, for the call sites that run the container
  runtime by name.
"""

from __future__ import annotations

import atexit
import contextlib
import errno
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

_untrusted_roots: tuple[Path, ...] = ()
_restricting = False
_neutral: Path | None = None


def _real(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path.absolute()


def _is_within(path: Path, roots: Sequence[Path]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def untrusted_roots() -> tuple[Path, ...]:
    return _untrusted_roots


def add_untrusted_roots(*roots: Path) -> None:
    """Never find tools under these directories (the project, session dirs).

    When :func:`restricted_path` is active, the process ``PATH`` is narrowed
    again to drop entries under the new roots.
    """
    global _untrusted_roots
    added = tuple(_real(Path(root)) for root in roots if root is not None)
    _untrusted_roots = tuple(dict.fromkeys(_untrusted_roots + added))
    if _restricting:
        os.environ["PATH"] = os.pathsep.join(trusted_path_entries())


def reset() -> None:
    """Forget the untrusted roots (tests, new launches)."""
    global _untrusted_roots
    _untrusted_roots = ()


def _entry_skip_reason(entry: str) -> str | None:
    if not entry:
        return "empty"
    if not os.path.isabs(entry):
        return "relative"
    if _is_within(_real(Path(entry)), _untrusted_roots):
        return "inside the project or a session directory"
    return None


def trusted_path_entries(path: str | None = None) -> list[str]:
    """The ``PATH`` entries a host tool may be found through, in order."""
    raw = os.environ.get("PATH", os.defpath) if path is None else path
    return [
        entry for entry in raw.split(os.pathsep) if _entry_skip_reason(entry) is None
    ]


def resolve(name: str) -> str | None:
    """Absolute path of host tool ``name``, or None when it is missing.

    An absolute ``name`` is accepted as given unless it lies under an
    untrusted root. A candidate whose real path (after symlinks) lies under an
    untrusted root is skipped too.
    """
    path = os.environ.get("PATH", os.defpath)
    found: str | None = None
    if os.path.isabs(name):
        if os.access(name, os.X_OK) and not _is_within(
            _real(Path(name)), _untrusted_roots
        ):
            found = name
    elif os.sep not in name:
        for entry in trusted_path_entries(path):
            candidate = shutil.which(name, path=entry)
            if candidate is None:
                continue
            if _is_within(_real(Path(candidate)), _untrusted_roots):
                continue
            found = candidate
            break
    return found


def skipped_matches(name: str) -> list[str]:
    """``PATH`` entries holding ``name`` that :func:`resolve` ignored, with why.

    Used to explain a missing tool, e.g. ``". (relative)"``.
    """
    reasons = []
    for entry in os.environ.get("PATH", os.defpath).split(os.pathsep):
        reason = _entry_skip_reason(entry)
        if reason is None:
            continue
        if shutil.which(name, path=entry or os.curdir) is not None:
            reasons.append(f"{entry or '(empty)'} ({reason})")
    return reasons


def missing_message(name: str) -> str:
    """``"<name> not found on PATH"``, naming any ignored entry that holds it."""
    skipped = skipped_matches(name)
    if not skipped:
        return f"{name} not found on PATH"
    return f"{name} not found on PATH (ignored: {', '.join(skipped)})"


def neutral_dir() -> Path:
    """A private (0700), empty directory outside every project: the cwd for tools.

    Created once per process and removed at exit.
    """
    global _neutral
    if _neutral is None or not _neutral.is_dir():
        _neutral = Path(tempfile.mkdtemp(prefix="project-sandbox-cwd-"))
        atexit.register(shutil.rmtree, _neutral, ignore_errors=True)
    return _neutral


def argv_for(argv: Sequence[str]) -> list[str]:
    """``argv`` with its program resolved; FileNotFoundError when it is missing."""
    program = resolve(argv[0])
    if program is None:
        raise FileNotFoundError(errno.ENOENT, missing_message(argv[0]), argv[0])
    return [program, *argv[1:]]


def run(
    argv: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess:
    """``subprocess.run`` with argv[0] resolved and cwd defaulting to :func:`neutral_dir`.

    Raises FileNotFoundError, like ``subprocess.run``, when the tool is missing.
    """
    kwargs.setdefault("check", False)
    return subprocess.run(  # noqa: PLW1510 - check is set above
        argv_for(argv), cwd=neutral_dir() if cwd is None else cwd, **kwargs
    )


@contextlib.contextmanager
def restricted_path() -> Iterator[None]:
    """Narrow ``os.environ["PATH"]`` to trusted entries; restore it afterwards.

    Covers the subprocesses that run tools by name (the container runtime and
    what it spawns). Roots added inside the block narrow it again.
    """
    global _restricting
    previous = os.environ.get("PATH")
    previous_restricting = _restricting
    _restricting = True
    os.environ["PATH"] = os.pathsep.join(trusted_path_entries())
    try:
        yield
    finally:
        _restricting = previous_restricting
        if previous is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = previous


@contextlib.contextmanager
def launch_scope() -> Iterator[None]:
    """One launch: fresh untrusted roots and a restricted ``PATH``, undone after."""
    reset()
    try:
        with restricted_path():
            yield
    finally:
        reset()
