"""Input fingerprinting so a matching image can be reused without rebuilding.

The CLI rebuilds the container image on every run by default. When the build
inputs have not changed and the image still exists, that work is wasted. This
module computes a deterministic fingerprint of the build inputs and records it,
alongside the image tag, in a sidecar JSON file under the generated
``.project-sandbox`` directory. The caller compares the current fingerprint with
the recorded one (and confirms the image still exists) to decide whether the
build can be skipped.

Skipping is correctness-safe: it only happens on an exact fingerprint+tag match,
and any read/parse problem degrades to "not cached" so the build runs.
"""

import hashlib
import json
from pathlib import Path

# Top-level files in the generated context that fully determine the CLI image.
# The CLI always builds context_dir/"Dockerfile"; entrypoint.sh, init-firewall.sh
# and project-sandbox-devcontainer-init are COPY'd into it.
_BUILD_INPUT_FILES = (
    "Dockerfile",
    "entrypoint.sh",
    "init-firewall.sh",
    "project-sandbox-devcontainer-init",
)

_STATE_FILENAME = ".build-state.json"


def state_path(context_dir: Path) -> Path:
    return context_dir / _STATE_FILENAME


def compute_fingerprint(context_dir: Path, *, extra: dict[str, str]) -> str:
    """Return a SHA256 over the build-input files plus the ``extra`` mapping.

    ``extra`` carries inputs that are not files in ``context_dir`` (the resolved
    base image and the image tag). Missing input files are simply skipped, so
    the fingerprint still reflects which files are present.
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


def read_state(context_dir: Path) -> dict | None:
    try:
        data = json.loads(state_path(context_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_state(context_dir: Path, *, image_tag: str, fingerprint: str) -> None:
    state_path(context_dir).write_text(
        json.dumps({"image_tag": image_tag, "fingerprint": fingerprint}) + "\n",
        encoding="utf-8",
    )


def is_cache_valid(context_dir: Path, *, image_tag: str, fingerprint: str) -> bool:
    state = read_state(context_dir)
    return (
        state is not None
        and state.get("image_tag") == image_tag
        and state.get("fingerprint") == fingerprint
    )
