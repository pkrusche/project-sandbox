"""Validate literal values inserted into generated build instructions."""

import re
import unicodedata
from pathlib import PurePosixPath


def validate_image(image: str) -> None:
    # Accept literal registry/repository, tag, and digest tokens. The runtime
    # validates reference semantics and availability; whitespace, substitutions,
    # flags, and instruction delimiters must never reach a generated FROM.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]*", image):
        raise ValueError(
            f"Invalid base image {image!r}: expected a literal image reference"
        )


def validate_version(version: str, *, option: str) -> None:
    # Each generated tag appends '-slim' (five characters) to this value.
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,122}", version):
        raise ValueError(
            f"Invalid {option} version {version!r}: expected a literal image tag"
        )


def cargo_path(value: str) -> str:
    """Validate a relative member path or glob before traversal/rendering."""
    if (
        not isinstance(value, str)
        or not value
        or any(unicodedata.category(c) in {"Cc", "Zl", "Zp"} for c in value)
    ):
        raise ValueError(
            f"Invalid Cargo workspace path {value!r}: empty or control characters"
        )
    if "\\" in value:
        raise ValueError(
            f"Invalid Cargo workspace path {value!r}: backslashes are unsupported"
        )
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(
            f"Invalid Cargo workspace path {value!r}: must stay inside the project"
        )
    return path.as_posix()
