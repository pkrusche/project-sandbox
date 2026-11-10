"""Tamper-evidence for the project Dockerfile.

project-sandbox records a SHA256 of the project Dockerfile(s) it builds from in
the project's private host state (``projects/<key>/dockerfile-checksums.json``
under the state root). No sandbox mounts that directory, so an agent running in
the container can neither read nor rewrite the recorded checksums to cover its
tracks.

On a later run the CLI recomputes the checksum of the current project Dockerfile
and warns when it differs from the recorded one. This surfaces an edit made to a
Dockerfile that lives in the writable workspace — for example a ``--dockerfile``
the agent modified during a previous session — before that Dockerfile is built
again.

Only Dockerfiles that live *outside* the generated build directory are tracked:
the generated Dockerfiles are rendered fresh into private staging for every
session, so there is nothing to detect there. Any read/parse problem degrades to "no
recorded checksum", so an inconclusive check never raises a false alarm.
"""

import hashlib
import json
from pathlib import Path

from . import paths

_STATE_FILENAME = "dockerfile-checksums.json"


def state_path(state_dir: "paths.Target") -> Path:
    return paths.target_path(state_dir, _STATE_FILENAME)


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _key(path: Path) -> str:
    return path.resolve(strict=False).as_posix()


def _read(state_dir: "paths.Target") -> dict[str, str]:
    try:
        data = json.loads(state_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def changed_warnings(state_dir: "paths.Target", dockerfiles: list[Path]) -> list[str]:
    """Return a warning for each Dockerfile whose content differs from the
    checksum recorded by a previous build.

    A Dockerfile with no recorded checksum (first run, or never tracked before)
    produces no warning — only an actual change relative to a recorded baseline
    is reported.
    """
    recorded = _read(state_dir)
    warnings: list[str] = []
    for path in dockerfiles:
        try:
            current = checksum(path)
        except OSError:
            continue
        prior = recorded.get(_key(path))
        if prior is not None and prior != current:
            warnings.append(
                f"[W] Project Dockerfile changed since it was last built: {path}. "
                "If you did not change it yourself, an agent may have modified it; "
                "review it before rebuilding."
            )
    return warnings


def record(state_dir: "paths.Target", dockerfiles: list[Path]) -> None:
    """Persist the current checksum of each tracked Dockerfile as the trusted
    baseline. Unreadable files are skipped rather than recorded.

    Merges into any existing state file rather than overwriting it, so that
    baselines previously recorded for other Dockerfiles (for example a
    different ``--dockerfile`` used in an earlier run) are preserved."""
    data = _read(state_dir)
    for path in dockerfiles:
        try:
            data[_key(path)] = checksum(path)
        except OSError:
            continue
    with paths.target_dir(state_dir) as directory:
        directory.write(_STATE_FILENAME, json.dumps(data, sort_keys=True) + "\n")
