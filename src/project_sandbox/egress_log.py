"""Read the global egress sidecar's access log (design.md Decision 11).

The sidecar logs one ``SBX``-prefixed line per transaction (``squid_config``'s
``logformat sbx``), ending with the client IP of the sandbox that made the
request. Denials are attributed to the local policy (our ``sbx_cause`` labels,
SNI termination, failed peeks) or to the upstream parent proxy (a CONNECT it
refused, or any 407), so the user knows where to change policy.

At session end the CLI counts the denials whose timestamp falls inside the
session's window, measured on the sidecar's clock, across every sandbox that
shares the sidecar. ``project-sandbox-egress log`` lists them.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from . import squid_config

LOCAL = "local policy"
UPSTREAM = "upstream proxy"
# Plain-HTTP sentinel the readiness probe requests (egress_sidecar); its
# denials are not the user's.
READINESS_HOST = "sidecar-readiness.invalid"

_LINE_RE = re.compile(
    rf"^{squid_config.LOG_PREFIX} (?P<ts>\d+\.\d+) (?P<status>\S+)/(?P<code>\d+) "
    r"(?P<method>\S+) (?P<url>\S+) sni=(?P<sni>\S*) bump=(?P<bump>\S*) "
    r"err=(?P<err>\S*) cause=(?P<cause>\S*) peer=(?P<peer>\S*)"
    r"(?: client=(?P<client>\S*))?$"
)


@dataclass(frozen=True)
class Denial:
    target: str  # host, or host + path prefix for intercepted path denials
    cause: str
    attribution: str
    # Not part of identity: denials are counted per (target, cause, attribution).
    ts: float | None = field(default=None, compare=False)
    client: str | None = field(default=None, compare=False)


def _target(method: str, url: str, *, with_path: bool) -> str | None:
    if method == "CONNECT":
        host, _, _port = url.rpartition(":")
        return host.strip("[]").lower() or None
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host:
        return None
    if with_path:
        segments = [s for s in (parts.path or "/").split("/") if s][:2]
        return host + "/" + "/".join(segments) + ("/…" if segments else "")
    return host


def classify(line: str, *, upstream: bool) -> Denial | None:
    """The denial this access-log line records, or ``None`` for allowed traffic."""
    match = _LINE_RE.match(line.strip())
    if not match:
        return None
    method, url, err = match["method"], match["url"], match["err"]
    code, cause, bump = int(match["code"]), match["cause"], match["bump"]
    ts = float(match["ts"])
    client = match["client"] if match["client"] not in (None, "", "-") else None
    local_denial = (
        match["status"].startswith("TCP_DENIED")
        or err == "TCP_RESET"
        or err.startswith("ERR_SBX_")
    )
    if local_denial and cause not in ("", "-"):
        label = cause
    elif bump == "terminate":
        label = "sni-mismatch"
    elif bump == "peek" and err == "ERR_PROTOCOL_UNKNOWN":
        label = "non-tls-tunnel"
    elif local_denial:
        label = "denied"
    elif upstream and (code == 407 or (method == "CONNECT" and code >= 400)):
        target = _target(method, url, with_path=False)
        if target is None:
            return None
        return Denial(target, f"HTTP {code}", UPSTREAM, ts, client)
    else:
        return None
    target = _target(method, url, with_path=label == "path-not-allowed")
    if target is None or target.startswith(READINESS_HOST):
        return None
    return Denial(target, label, LOCAL, ts, client)


def line_time(line: str) -> float | None:
    """The ``%ts`` of an access-log line, or ``None`` for any other line."""
    match = _LINE_RE.match(line.strip())
    return float(match["ts"]) if match else None


def iter_denials(lines: Iterable[str], *, upstream: bool = False):
    for line in lines:
        denial = classify(line, upstream=upstream)
        if denial is not None:
            yield denial


def denials(text: str, *, upstream: bool = False) -> Counter[Denial]:
    return Counter(iter_denials(text.splitlines(), upstream=upstream))


def count_in_window(
    text: str, *, start: float, end: float, upstream: bool = False
) -> int:
    """Denials logged at ``start <= %ts <= end`` (sidecar clock, epoch seconds)."""
    return sum(
        1
        for d in iter_denials(text.splitlines(), upstream=upstream)
        if d.ts is not None and start <= d.ts <= end
    )


def iso_local(epoch: float) -> str:
    """Seconds-precision local ISO 8601 time, as ``--since`` accepts it."""
    return (
        dt.datetime.fromtimestamp(int(epoch))
        .astimezone()
        .replace(tzinfo=None)
        .isoformat()
    )


def session_summary(count: int, *, since: float) -> list[str]:
    """The session-end denial count; nothing when there were no denials."""
    if count <= 0:
        return []
    noun = "request" if count == 1 else "requests"
    return [
        (
            f"Egress: the sidecar denied {count} {noun} during this session "
            "(all sandboxes on this sidecar)."
        ),
        f"  List them: project-sandbox-egress log --denied --since {iso_local(since)}",
    ]


def format_denial(denial: Denial) -> str:
    when = iso_local(denial.ts) if denial.ts is not None else "-"
    where = "" if denial.attribution == LOCAL else f" ({denial.attribution})"
    return f"{when}  {denial.client or '-':<15}  {denial.target}  {denial.cause}{where}"


_DURATION_RE = re.compile(r"^(?P<n>[0-9]+)(?P<unit>[smhd])$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_since(value: str, *, now: float) -> float:
    """``--since``: an ISO 8601 time (local unless it has an offset) or ``10m``."""
    text = value.strip()
    match = _DURATION_RE.fullmatch(text)
    if match:
        return now - int(match["n"]) * _UNITS[match["unit"]]
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(
            f"invalid --since {value!r}: use an ISO 8601 time such as "
            "2026-11-06T10:42:13, or a duration such as 30s, 10m, 2h or 1d"
        ) from None
    if moment.tzinfo is None:
        moment = moment.astimezone()  # local time
    return moment.timestamp()
