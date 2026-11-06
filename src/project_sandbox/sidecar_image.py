"""Build context for the Squid egress sidecar image (design.md Decision 9).

The context is rendered from ``templates/egress-sidecar/`` and the image is
tagged with a hash of the rendered files, so a template or pin change produces a
new tag (and a build), while an unchanged context reuses the existing image.
The context lives under the user's state directory, never in a project.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from . import egress_policy, local_service_network, observability, squid_config
from .templating import get_template

# Pinned by digest; updated by scripts/update-pins.py (Alpine release + digest).
BASE_IMAGE = (
    "alpine:3.22.1"
    "@sha256:4bcff63911fcb4448bd4fdacec207030997caf25e9bea4045fa6c8c44de311d1"
)
# apk "~" pins the Squid release and accepts Alpine's security revisions of it
# (an exact -rN pin breaks once the branch repository drops that revision).
# Updated by scripts/update-pins.py from the branch's APKINDEX.
SQUID_PACKAGE = "squid~6.12"
IMAGE_REPOSITORY = "project-sandbox-egress-sidecar"
TEMPLATE_DIR = "egress-sidecar"

# Rendered context file -> (template name, file mode).
_FILES = {
    "Dockerfile": ("Dockerfile.j2", 0o644),
    "entrypoint.sh": ("entrypoint.sh.j2", 0o755),
    "sni-match": ("sni-match", 0o755),
    "sbx-reload": ("sbx-reload.j2", 0o755),
    "sbx-forwarders": ("sbx-forwarders.j2", 0o755),
}


def render_files() -> dict[str, str]:
    """The build context's files, rendered (name -> text)."""
    values = {
        "base_image": BASE_IMAGE,
        "squid_package": SQUID_PACKAGE,
        "forward_host": local_service_network.HOSTNAME,
        "input_dir": squid_config.INPUT_DIR,
        "state_dir": squid_config.STATE_DIR,
        "min_port": egress_policy.MIN_FORWARD_PORT,
        "proxy_port": squid_config.LISTEN_PORT,
        "error_directory": squid_config.ERROR_DIRECTORY,
        "error_page_sources": [
            f"errors/{page}" for page in sorted(squid_config.error_pages())
        ],
    }
    rendered = {}
    for name, (template, _mode) in _FILES.items():
        text = get_template(f"{TEMPLATE_DIR}/{template}").render(**values)
        rendered[name] = text if text.endswith("\n") else text + "\n"
    for page, html in squid_config.error_pages().items():
        rendered[f"errors/{page}"] = html
    return rendered


def image_tag(files: dict[str, str] | None = None) -> str:
    files = render_files() if files is None else files
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode() + b"\0" + files[name].encode() + b"\0")
    return f"{IMAGE_REPOSITORY}:{digest.hexdigest()[:16]}"


def default_context_root() -> Path:
    """``$XDG_STATE_HOME/project-sandbox/egress-sidecar-image``."""
    return observability.state_dir().parent / "egress-sidecar-image"


def render(root: Path | None = None) -> tuple[Path, str]:
    """Write the build context to ``<root>/<tag suffix>``; return (dir, tag).

    Writing is idempotent: an existing context for the same tag is rewritten
    with identical content.
    """
    files = render_files()
    tag = image_tag(files)
    context = (root or default_context_root()) / tag.rsplit(":", 1)[1]
    context.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        path = context / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
        os.chmod(path, _FILES[name][1] if name in _FILES else 0o644)
    return context, tag
