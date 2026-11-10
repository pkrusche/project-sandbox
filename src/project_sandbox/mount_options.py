"""Parse the supported --mount grammar and identify bounded host bind sources.

Docker uses CSV fields, not a comma split (docker/cli, opts/mount.go). Keep the
source/target interpretation shared between security checks and argv rendering.
Unknown runtime forms may be preserved by command builders, but security checks
must call bind_source() and fail closed when their exposure is not understood.
"""

import csv
import io
import re
from pathlib import Path

_ALIASES = {"src": "source", "dst": "target", "destination": "target", "ro": "readonly"}
_BOOLEAN_KEYS = {"readonly", "bind-nonrecursive"}
_SIMPLE_KEYS = {"type", "source", "target", "readonly"}
_BIND_ENUMS = {
    "bind-propagation": {"private", "rprivate", "shared", "rshared", "slave", "rslave"},
    "bind-recursive": {"enabled", "disabled", "writable", "readonly"},
    "consistency": {"consistent", "cached", "delegated", "default"},
}
# Go's encoding/csv rejects bare quotes in unquoted fields. Python's csv reader
# doesn't, even with strict=True, so first enforce the same quoting structure.
_CSV_FIELD = r'(?:[^",\r\n]*|"(?:[^"\r\n]|"")*")'
_CSV_RECORD = re.compile(rf"{_CSV_FIELD}(?:,{_CSV_FIELD})*")


def parse(value: str) -> dict[str, str]:
    value = value.strip()
    if not value or "\0" in value or not _CSV_RECORD.fullmatch(value):
        raise ValueError("Invalid CSV quoting in --mount")
    try:
        parts = next(csv.reader([value], strict=True))
    except csv.Error as exc:
        raise ValueError(f"Invalid --mount: {exc}") from exc
    fields: dict[str, str] = {}
    for part in parts:
        key, separator, item = part.partition("=")
        if not key or key.strip() != key:
            raise ValueError("Empty or whitespace-padded --mount option")
        key = _ALIASES.get(key.lower(), key.lower())
        if key in fields:
            raise ValueError(f"Ambiguous duplicate --mount option: {key}")
        if separator and (not item or item.strip() != item):
            raise ValueError(f"Empty or whitespace-padded --mount value: {key}")
        if key in _BOOLEAN_KEYS:
            if not separator or item.lower() in ("true", "t", "1"):
                item = "true"
            elif item.lower() in ("false", "f", "0"):
                item = "false"
            else:
                raise ValueError(f"Invalid boolean --mount value: {key}")
        elif not separator:
            raise ValueError(f"--mount option requires a value: {key}")
        if key == "type" or key in _BIND_ENUMS:
            item = item.lower()
        fields[key] = item
    return fields


def simple_bind(fields: dict[str, str]) -> bool:
    # Retain project-sandbox's shorthand: without type, a fully modeled mount
    # is rendered explicitly as type=bind. Never apply this to raw passthrough.
    return (
        fields.keys() <= _SIMPLE_KEYS
        and fields.get("type", "bind") == "bind"
        and bool(fields.get("source"))
        and bool(fields.get("target"))
    )


def validate_target(target: str) -> None:
    path = Path(target)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Bind mount target must be an absolute jail path: {target}")


def bind_source(value: str) -> Path:
    """Return the lexical source, rejecting indirect or ambiguous host exposure."""
    fields = parse(value)
    if not simple_bind(fields):
        if fields.get("type") != "bind":
            raise ValueError(
                "Isolation requires a bind --mount with a known host source; volume and other indirect mount types are unsupported"
            )
        if fields.keys() - (_SIMPLE_KEYS | _BOOLEAN_KEYS | _BIND_ENUMS.keys()):
            raise ValueError(
                "Unsupported --mount options: host filesystem exposure cannot be verified"
            )
    for key, allowed in _BIND_ENUMS.items():
        if key in fields and fields[key] not in allowed:
            raise ValueError(f"Unsupported --mount value for {key}: {fields[key]}")
    if not fields.get("source") or not fields.get("target"):
        raise ValueError("Isolation requires both --mount source and target")
    validate_target(fields["target"])
    return Path(fields["source"]).absolute()


def encode(fields: list[str]) -> str:
    stream = io.StringIO()
    csv.writer(stream, lineterminator="").writerow(fields)
    return stream.getvalue()
