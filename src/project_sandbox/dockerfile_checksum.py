"""Tamper-evidence for the project Dockerfile.

project-sandbox records a SHA256 of the project Dockerfile(s) it builds from in a
sidecar JSON file under the generated ``.project-sandbox`` directory. That
directory is masked (mounted empty and read-only) inside every sandbox, so an
agent running in the container can neither read nor rewrite the recorded
checksums to cover its tracks.

On a later run the CLI recomputes the checksum of the current project Dockerfile
and warns when it differs from the recorded one. This surfaces an edit made to a
Dockerfile that lives in the writable workspace — for example a ``--dockerfile``
the agent modified during a previous session — before that Dockerfile is built
again.

Only Dockerfiles that live *outside* ``.project-sandbox`` are tracked: the
generated Dockerfiles inside the masked directory cannot be reached by the agent,
so there is nothing to detect there. Any read/parse problem degrades to "no
recorded checksum", so an inconclusive check never raises a false alarm.
"""

import hashlib
import json
from pathlib import Path

_STATE_FILENAME = ".dockerfile-checksums.json"


def state_path(context_dir: Path) -> Path:
    return context_dir / _STATE_FILENAME


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _key(path: Path) -> str:
    return path.resolve(strict=False).as_posix()


def _read(context_dir: Path) -> dict[str, str]:
    try:
        data = json.loads(state_path(context_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def changed_warnings(context_dir: Path, dockerfiles: list[Path]) -> list[str]:
    """Return a warning for each Dockerfile whose content differs from the
    checksum recorded by a previous build.

    A Dockerfile with no recorded checksum (first run, or never tracked before)
    produces no warning — only an actual change relative to a recorded baseline
    is reported.
    """
    recorded = _read(context_dir)
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


def record(context_dir: Path, dockerfiles: list[Path]) -> None:
    """Persist the current checksum of each tracked Dockerfile as the trusted
    baseline. Unreadable files are skipped rather than recorded."""
    data: dict[str, str] = {}
    for path in dockerfiles:
        try:
            data[_key(path)] = checksum(path)
        except OSError:
            continue
    state_path(context_dir).write_text(
        json.dumps(data, sort_keys=True) + "\n", encoding="utf-8"
    )
