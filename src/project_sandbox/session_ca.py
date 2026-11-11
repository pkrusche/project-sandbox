"""The egress sidecar's CA, and per-session sandbox input directories.

Squid's ``ssl-bump`` port always needs a signing certificate, and it intercepts
``api.github.com`` for the Copilot token exchange. The global sidecar of each
runtime owns one CA (egress-sidecar-macos, design.md Decision 3):

- EC P-256, ``BasicConstraints(ca=True, path_length=0)``,
  ``KeyUsage(keyCertSign, cRLSign)``;
- critical ``NameConstraints`` permitting the intercepted hosts' registrable
  domain (``github.com``: Squid mimics the origin certificate's names), or only
  the reserved name ``invalid`` when nothing is intercepted, so a leaked key
  cannot mint a certificate any client accepts for another domain;
- a lifetime from ``[egress.ca] lifetime_days`` (default 7 days, at most
  :data:`MAX_LIFETIME`), reused across sidecar restarts while it has at least
  :data:`MIN_REMAINING` left and its constraints still match.

The CA lives in ``egress/<runtime>/sidecar/`` (directory 0700, key 0600), is
mounted only into the sidecar, and is deleted by ``project-sandbox-egress
down``.

:func:`session_directory` creates a per-session directory that holds an
``flock`` on ``.lock`` while the session runs (the sandbox's own inputs);
:func:`remove_stale` deletes directories whose lock is free, so a crashed
session's files do not outlive the next launch.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import os
import shutil
import time
from collections.abc import Callable, Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from . import egress_policy, mount_storage, observability

CERT_NAME = "ca.pem"
KEY_NAME = "ca-key.pem"
LOCK_NAME = ".lock"
# Reserved by RFC 6761: no real host can match, so an unused CA mints nothing
# that any client would accept.
UNUSED_CONSTRAINT = "invalid"
DEFAULT_LIFETIME = dt.timedelta(days=egress_policy.DEFAULT_CA_LIFETIME_DAYS)
MAX_LIFETIME = dt.timedelta(days=egress_policy.MAX_CA_LIFETIME_DAYS)
# A CA with less than this left is replaced by up/restart, and a launch that
# relies on it (interception active) warns.
MIN_REMAINING = dt.timedelta(days=2)
# Clock skew allowance between the host and the containers.
BACKDATE = dt.timedelta(minutes=5)
# A directory younger than this is never treated as stale: a launching session
# creates its directory a moment before it takes the lock.
STALE_GRACE_SECONDS = 120.0


class SessionCAError(RuntimeError):
    """The session CA could not be created."""


@dataclass(frozen=True)
class SessionCA:
    directory: Path
    cert_path: Path
    key_path: Path
    name_constraints: tuple[str, ...]
    not_before: dt.datetime
    not_after: dt.datetime

    @property
    def interception(self) -> bool:
        """True when the CA may sign for a real host (the sandbox must trust it)."""
        return self.name_constraints != (UNUSED_CONSTRAINT,)


def egress_root() -> Path:
    """``$XDG_STATE_HOME/project-sandbox/egress`` (next to the session records)."""
    return observability.state_dir().parent / "egress"


def name_constraints_for(bump_set: Iterable[str]) -> tuple[str, ...]:
    """See ``egress_policy.name_constraints``; ``invalid`` when nothing is bumped."""
    return egress_policy.name_constraints(bump_set) or (UNUSED_CONSTRAINT,)


def common_name(runtime_name: str) -> str:
    return f"project-sandbox egress {runtime_name}"[:64]


def generate(
    directory: Path,
    bump_set: Iterable[str],
    *,
    name: str,
    lifetime: dt.timedelta = DEFAULT_LIFETIME,
    now: dt.datetime | None = None,
) -> SessionCA:
    """Create the CA key and certificate in ``directory`` (which must exist).

    ``name`` is the certificate's common name (:func:`common_name`).
    ``lifetime`` is capped at :data:`MAX_LIFETIME`. Existing files are never
    overwritten.
    """
    if lifetime <= dt.timedelta(0):
        raise SessionCAError("CA lifetime must be positive")
    lifetime = min(lifetime, MAX_LIFETIME)
    now = now or dt.datetime.now(dt.UTC)
    not_before = now - BACKDATE
    not_after = now + lifetime
    constraints = name_constraints_for(bump_set)

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "project-sandbox"),
            x509.NameAttribute(NameOID.COMMON_NAME, name[:64]),
        ]
    )
    public = key.public_key()
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(public)
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.NameConstraints(
                permitted_subtrees=[x509.DNSName(host) for host in constraints],
                excluded_subtrees=None,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(public), critical=False
        )
        .sign(key, hashes.SHA256())
    )

    key_path = directory / KEY_NAME
    cert_path = directory / CERT_NAME
    _write_new(
        key_path,
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        0o600,
    )
    _write_new(cert_path, cert.public_bytes(serialization.Encoding.PEM), 0o644)
    return SessionCA(
        directory=directory,
        cert_path=cert_path,
        key_path=key_path,
        name_constraints=constraints,
        not_before=not_before,
        not_after=not_after,
    )


def load(directory: Path) -> SessionCA | None:
    """The CA in ``directory``, or ``None`` when it is missing or unreadable."""
    cert_path, key_path = directory / CERT_NAME, directory / KEY_NAME
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        if not key_path.is_file() or key_path.is_symlink():
            return None
        constraints = cert.extensions.get_extension_for_class(
            x509.NameConstraints
        ).value
    except (OSError, ValueError, x509.ExtensionNotFound):
        return None
    names = tuple(
        sorted(
            str(subtree.value)
            for subtree in constraints.permitted_subtrees or ()
            if isinstance(subtree, x509.DNSName)
        )
    )
    return SessionCA(
        directory=directory,
        cert_path=cert_path,
        key_path=key_path,
        name_constraints=names,
        not_before=cert.not_valid_before_utc,
        not_after=cert.not_valid_after_utc,
    )


def remaining(ca: SessionCA, *, now: dt.datetime | None = None) -> dt.timedelta:
    return ca.not_after - (now or dt.datetime.now(dt.UTC))


def reusable(
    ca: SessionCA | None,
    bump_set: Iterable[str],
    *,
    now: dt.datetime | None = None,
) -> bool:
    """Keep ``ca`` when it has :data:`MIN_REMAINING` left and constraints match."""
    return (
        ca is not None
        and remaining(ca, now=now) >= MIN_REMAINING
        and ca.name_constraints == tuple(sorted(name_constraints_for(bump_set)))
    )


def remove(directory: Path) -> None:
    """Delete the CA key and certificate in ``directory`` (missing files are fine)."""
    for name in (KEY_NAME, CERT_NAME):
        (directory / name).unlink(missing_ok=True)


def _write_new(path: Path, data: bytes, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, mode)
    except FileExistsError as exc:
        raise SessionCAError(f"refusing to overwrite {path}") from exc
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.chmod(path, mode)  # independent of the umask


# --------------------------------------------------------------------------
# Session directory lifecycle
# --------------------------------------------------------------------------


@contextmanager
def session_directory(
    session_id: str, *, root: Path | None = None
) -> Generator[Path, None, None]:
    """Create ``<root>/<session_id>`` (0700), hold its lock, delete it on exit.

    The directory and everything in it (including the CA key) is removed when
    the block exits, normally or by exception.
    """
    if not session_id or "/" in session_id or session_id in (".", ".."):
        raise SessionCAError(f"invalid session id: {session_id!r}")
    root = root or egress_root()
    mount_storage.ensure_private_dirs(root)
    directory = root / session_id
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise SessionCAError(
            f"egress session directory already exists: {directory}"
        ) from exc
    os.chmod(directory, 0o700)
    try:
        fd = os.open(
            directory / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield directory
        finally:
            os.close(fd)  # releases the lock
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def is_live(directory: Path) -> bool:
    """True while a session holds ``directory``'s lock."""
    return lock_held(directory / LOCK_NAME)


LOCK_PROBE_TOLERANCE_SECONDS = 0.5


def acquire_exclusive(
    fd: int, *, tolerance: float = LOCK_PROBE_TOLERANCE_SECONDS
) -> bool:
    """Take an exclusive ``flock`` on ``fd``; ``False`` if another holder keeps it.

    ``lock_held`` probes by taking the lock for an instant, so one failed
    non-blocking attempt can mean a probe, not an owner. Retrying for
    ``tolerance`` seconds rides out probes while still failing fast on a real
    owner, which holds the lock for its whole life.
    """
    deadline = time.monotonic() + tolerance
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)


def lock_held(path: Path) -> bool:
    """True while some process holds an ``flock`` on ``path``.

    Used instead of a pid check, which could match a reused pid. The probe
    takes the lock for an instant, so an owner must acquire with
    ``acquire_exclusive`` rather than a single non-blocking attempt.
    """
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    except OSError:
        return True  # unexpected (symlink, permissions): leave it alone
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def remove_stale(
    root: Path | None = None,
    *,
    grace_seconds: float = STALE_GRACE_SECONDS,
    now: float | None = None,
    match: Callable[[str], bool] | None = None,
) -> list[Path]:
    """Delete session directories no live session holds; return what was removed.

    Directories modified within ``grace_seconds`` are kept, so a session that
    has created its directory but not yet taken the lock is never raced.
    Symlinks and non-directories under ``root`` are ignored, not followed.
    """
    root = root or egress_root()
    if not root.is_dir():
        return []
    now = time.time() if now is None else now
    removed = []
    for entry in sorted(root.iterdir()):
        if entry.is_symlink() or not entry.is_dir():
            continue
        if match is not None and not match(entry.name):
            continue
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            continue
        if age < grace_seconds or is_live(entry):
            continue
        shutil.rmtree(entry, ignore_errors=True)
        removed.append(entry)
    return removed
