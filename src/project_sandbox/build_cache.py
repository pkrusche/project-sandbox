"""Input fingerprinting so a matching image can be reused without rebuilding.

The CLI rebuilds the container image on every run by default. When the build
inputs have not changed and the image still exists, that work is wasted. This
module computes a deterministic fingerprint of the build inputs and records it,
alongside the image tag, in ``build-state.json`` in the project's private host
state (``projects/<key>/`` under the state root). The caller compares the current fingerprint with
the recorded one (and confirms the image still exists) to decide whether the
build can be skipped.

Skipping is correctness-safe: it only happens on an exact fingerprint+tag match,
and any read/parse problem degrades to "not cached" so the build runs.
"""

import hashlib
import json
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from . import host_locks, paths

# Top-level files in the generated context that fully determine the CLI image.
# The CLI always builds build/"Dockerfile"; entrypoint.sh, init-firewall.sh,
# project-sandbox-init and project-sandbox-devcontainer-init are COPY'd into it.
_BUILD_INPUT_FILES = (
    "Dockerfile",
    "entrypoint.sh",
    "init-firewall.sh",
    "project-sandbox-init",
    "project-sandbox-devcontainer-init",
)

_STATE_FILENAME = "build-state.json"


@contextmanager
def build_lock(project: Path) -> Generator[None, None, None]:
    """Serialize cache checks and image builds for one project.

    Every session renders its own build context, but all of a project's
    sessions share its image tag and build state, so the lock is keyed by the
    project. It lives in private state, outside every build context and
    sandbox. Callers must hold it across the complete cache-check/build/
    state-write sequence; locking only ``write_state`` would still allow two
    first-time callers to build the same image.
    """
    with host_locks.path_lock("build", project):
        yield


def state_path(state_dir: "paths.Target") -> Path:
    return paths.target_path(state_dir, _STATE_FILENAME)


def compute_fingerprint(context_dir: Path, *, extra: dict[str, str]) -> str:
    """Return a SHA256 over the build-input files plus the ``extra`` mapping.

    ``context_dir`` is the session's rendered build directory. ``extra``
    carries inputs that are not files there (the resolved base image and the
    image tag). Missing input files are simply skipped, so the fingerprint
    still reflects which files are present.
    """
    h = hashlib.sha256()
    for name in sorted(_BUILD_INPUT_FILES):
        path = context_dir / name
        if not path.is_file():
            continue
        h.update(name.encode("utf-8"))
        h.update(b"\0")
        h.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii"))
        h.update(b"\0")
    h.update(b"extra\0")
    h.update(json.dumps(extra, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return h.hexdigest()


def read_state(state_dir: "paths.Target") -> dict | None:
    try:
        data = json.loads(state_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_state(state_dir: "paths.Target", *, image_tag: str, fingerprint: str) -> None:
    with paths.target_dir(state_dir) as directory:
        directory.write(
            _STATE_FILENAME,
            json.dumps({"image_tag": image_tag, "fingerprint": fingerprint}) + "\n",
        )


def is_cache_valid(
    state_dir: "paths.Target", *, image_tag: str, fingerprint: str
) -> bool:
    state = read_state(state_dir)
    return (
        state is not None
        and state.get("image_tag") == image_tag
        and state.get("fingerprint") == fingerprint
    )
