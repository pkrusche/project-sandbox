"""Shared, leased bridge forwarders for host-loopback services on Docker Engine.

share-local-service-forwarders design.md Decisions 1, 2 and 4. Every
concurrent session that forwards the same host-loopback service shares one
detached forwarder per ``(gateway, port)``. The forwarder is the
``egress_relay`` TCP forwarder run with ``--lease``; its state lives in
``$XDG_STATE_HOME/project-sandbox/local-forward/<gateway>-<port>/`` (directory
``0700``, files ``0600``, every open ``O_NOFOLLOW``)::

    fwd.lock   forwarder: LOCK_EX for its whole life   -> liveness
    lease      every session: LOCK_SH for its whole life
    ctl.lock   LOCK_EX briefly: serializes attach (session) against exit
    upstream   "<loopback host>\\n<project-sandbox version>\\n", written by the
               forwarder before it reports ready
    fwd.pid, fwd.port, fwd.log   (as relay.pid / relay.port / relay.log; the
               port file is the readiness signal ``RelayControl`` waits for)

A session :func:`attach`\\ es under ``ctl.lock``: it reuses a live forwarder
with a matching upstream and version, or spawns one after a probe-bind, then
takes a shared lease. Every ``IDLE_POLL`` seconds the forwarder takes
``ctl.lock`` (non-blocking) and tries an exclusive lock on ``lease``; when that
succeeds no session uses it, and it exits while still holding ``ctl.lock``.
Correctness rests on kernel ``flock`` semantics only, so a crashed session or
forwarder needs no cleanup: its locks are released when it dies.
"""

from __future__ import annotations

import errno
import fcntl
import ipaddress
import os
import socket
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Self

from . import __version__, egress_relay, mount_storage, observability

FILES = egress_relay.StateFiles(
    lock="fwd.lock", pid="fwd.pid", port="fwd.port", log="fwd.log"
)
LEASE_NAME = "lease"
CTL_NAME = "ctl.lock"
UPSTREAM_NAME = "upstream"
STATE_NAMES = (
    FILES.lock,
    FILES.pid,
    FILES.port,
    FILES.log,
    LEASE_NAME,
    CTL_NAME,
    UPSTREAM_NAME,
)
# How often a forwarder checks whether any session still holds a lease.
IDLE_POLL = 2.0
# A session may wait on ctl.lock behind another session's spawn.
CTL_TIMEOUT = egress_relay.SPAWN_TIMEOUT + 5.0
# A live forwarder that has not written its port file yet is still starting.
READY_TIMEOUT = 5.0
_LOCK_RETRY = 0.02


class ForwarderError(SystemExit):
    """A user-facing failure to attach to or start a local-service forwarder."""


# --------------------------------------------------------------------------
# State layout
# --------------------------------------------------------------------------


def root() -> Path:
    """``$XDG_STATE_HOME/project-sandbox/local-forward``."""
    return observability.state_dir().parent / "local-forward"


def state_dir(gateway: str, port: int, *, root_dir: Path | None = None) -> Path:
    """The state directory for the forwarder on ``gateway:port``."""
    address = ipaddress.ip_address(gateway)  # never a name or a path fragment
    if not 1 <= port <= 65535:
        raise ValueError(f"invalid forwarder port: {port}")
    return (root_dir or root()) / f"{address}-{port}"


def _refuse_symlink(path: Path) -> bool:
    """``True`` if ``path`` exists; ``ForwarderError`` if it is a symlink."""
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(mode):
        raise ForwarderError(
            f"Refusing to use local-forward state path {path}: it is a symlink."
        )
    return True


def _check_state_files(directory: Path) -> None:
    for name in STATE_NAMES:
        _refuse_symlink(directory / name)


def ensure_state_dir(directory: Path) -> Path:
    """Create ``directory`` and its ``local-forward`` parent private (``0700``).

    Refuses (``ForwarderError``) a symlink at either directory or at any state
    file inside, naming the path.
    """
    for path in (directory.parent, directory):
        if not _refuse_symlink(path):
            mount_storage.ensure_private_dirs(path)
            _refuse_symlink(path)  # lost a race with a planted link
        if not path.is_dir():
            raise ForwarderError(f"Local-forward state path {path} is not a directory.")
        os.chmod(path, 0o700, follow_symlinks=False)
    _check_state_files(directory)
    return directory


def _open(path: Path, flags: int) -> int:
    """Open a private state file; a symlink there is a ``ForwarderError``."""
    try:
        return egress_relay._open_private(path, flags)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise ForwarderError(
                f"Refusing to use local-forward state path {path}: it is a symlink."
            ) from exc
        raise ForwarderError(f"Cannot open {path}: {exc.strerror or exc}") from exc


def _lock(fd: int, operation: int, path: Path, timeout: float) -> None:
    """``flock(fd, operation)``, waiting at most ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, operation | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise ForwarderError(
                    f"Timed out after {timeout:.0f}s waiting for the lock {path}."
                ) from None
            time.sleep(_LOCK_RETRY)
        except OSError as exc:  # e.g. a filesystem without flock support
            raise ForwarderError(
                f"Cannot lock {path}: {exc.strerror or exc}. The local-forward "
                "state directory must be on a filesystem that supports flock."
            ) from exc


def write_upstream(directory: Path, target_host: str) -> None:
    egress_relay._write_atomic(
        directory / UPSTREAM_NAME, f"{target_host}\n{__version__}\n"
    )


def read_upstream(directory: Path) -> tuple[str, str] | None:
    """``(loopback host, project-sandbox version)`` recorded by the forwarder."""
    try:
        fd = _open(directory / UPSTREAM_NAME, os.O_RDONLY)
    except ForwarderError:
        return None
    with os.fdopen(fd, encoding="utf-8", errors="replace") as handle:
        lines = handle.read().splitlines()
    if not lines or not lines[0]:
        return None
    return lines[0], (lines[1] if len(lines) > 1 else "")


def _format(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


# --------------------------------------------------------------------------
# Forwarder side
# --------------------------------------------------------------------------


class IdleCheck:
    """The forwarder's exit check, run as ``_run_locked``'s ``idle`` callback.

    Returns ``True`` when no session holds a lease, and then keeps both
    ``ctl.lock`` and ``lease`` locked until :meth:`close` (process exit), so a
    session attaching meanwhile waits and then finds the forwarder gone.
    """

    def __init__(self, directory: Path) -> None:
        self._ctl = _open(directory / CTL_NAME, os.O_RDWR | os.O_CREAT)
        try:
            self._lease = _open(directory / LEASE_NAME, os.O_RDWR | os.O_CREAT)
        except BaseException:
            os.close(self._ctl)
            raise
        self.exiting = False

    def __call__(self) -> bool:
        try:
            fcntl.flock(self._ctl, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False  # a session is attaching; check again next round
        try:
            fcntl.flock(self._lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fcntl.flock(self._ctl, fcntl.LOCK_UN)
            return False  # at least one session holds its lease
        self.exiting = True
        return True

    def close(self) -> None:
        for fd in (self._lease, self._ctl):
            try:
                os.close(fd)  # releases its locks
            except OSError:
                pass


def serve(
    directory: Path,
    *,
    bind: str,
    port: int,
    target_host: str,
    target_port: int,
    idle_poll: float = IDLE_POLL,
    stop: threading.Event | None = None,
    install_signals: bool = True,
) -> int:
    """Run the shared forwarder until no session holds a lease (or SIGTERM)."""
    try:
        ensure_state_dir(directory)
        check = IdleCheck(directory)
    except ForwarderError as exc:
        print(f"local forwarder: {exc}", file=sys.stderr)
        return 1
    try:
        return egress_relay.serve_forward(
            directory,
            bind=bind,
            peer=None,
            target_port=target_port,
            port=port,
            target_host=target_host,
            exact_port=True,
            stop=stop,
            install_signals=install_signals,
            files=FILES,
            idle=check,
            idle_poll=idle_poll,
            on_ready=lambda _server: write_upstream(directory, target_host),
        )
    finally:
        check.close()  # after fwd.lock: an attach waiting on ctl.lock respawns


# --------------------------------------------------------------------------
# Session side
# --------------------------------------------------------------------------


class Lease:
    """A session's shared lease on a forwarder; close it when the session ends."""

    def __init__(self, fd: int, directory: Path) -> None:
        self._fd: int | None = fd
        self.directory = directory

    def fileno(self) -> int:
        if self._fd is None:
            raise ValueError("lease already released")
        return self._fd

    @property
    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def _control() -> egress_relay.RelayControl:
    return egress_relay.RelayControl(files=FILES)


def probe_bind(gateway: str, port: int, *, label: str) -> None:
    """Fail unless ``gateway:port`` can be bound right now."""
    family = (
        socket.AF_INET6
        if ipaddress.ip_address(gateway).version == 6
        else socket.AF_INET
    )
    probe = socket.socket(family, socket.SOCK_STREAM)
    try:
        # As the forwarder binds: TIME_WAIT sockets left by a previous one must
        # not fail the probe, while a live listener still does.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((gateway, port))
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            raise ForwarderError(
                f"Cannot bind the {label} proxy to {gateway}:{port}: another "
                "process owns that address and port (it is not a project-sandbox "
                "forwarder)."
            ) from exc
        raise ForwarderError(
            f"Cannot bind the {label} proxy to {gateway}:{port}: {exc}"
        ) from exc
    finally:
        probe.close()


def _require_compatible(
    directory: Path,
    control: egress_relay.RelayControl,
    *,
    gateway: str,
    port: int,
    target_host: str,
    label: str,
) -> None:
    """Refuse a live forwarder whose upstream or version differs from ours."""
    deadline = time.monotonic() + READY_TIMEOUT
    while control.port(directory) is None:
        if not control.is_live(directory):
            raise ForwarderError(
                f"The {label} forwarder on {gateway}:{port} exited while this "
                "session was attaching; retry.\n" + control.log_tail(directory)
            )
        if time.monotonic() >= deadline:
            raise ForwarderError(
                f"The {label} forwarder on {gateway}:{port} is running but not "
                "ready.\n" + control.log_tail(directory)
            )
        time.sleep(0.05)
    recorded = read_upstream(directory)
    if recorded is None:
        raise ForwarderError(
            f"The {label} forwarder on {gateway}:{port} has not recorded its "
            f"upstream ({directory / UPSTREAM_NAME})."
        )
    host, version = recorded
    if host != target_host:
        raise ForwarderError(
            f"Cannot share the forwarder on {gateway}:{port}: it forwards to "
            f"{_format(host, port)}, but {label} needs {_format(target_host, port)}. "
            "One bridge port can serve only one loopback address; stop the "
            "sessions that use the other service, or move it to another port."
        )
    if version != __version__:
        raise ForwarderError(
            f"Cannot share the {label} forwarder on {gateway}:{port}: a forwarder "
            f"from another project-sandbox version ({version or 'unknown'}) is "
            f"running (this is {__version__}); wait for its sessions to end."
        )


def check_available(
    gateway: str,
    port: int,
    *,
    target_host: str = "127.0.0.1",
    label: str = "Ollama",
    root_dir: Path | None = None,
) -> None:
    """Pre-launch check: ``gateway:port`` is free or served by a compatible forwarder.

    Read-only (no state is created). :func:`attach` repeats the check under
    ``ctl.lock``, which is the authoritative one.
    """
    directory = state_dir(gateway, port, root_dir=root_dir)
    if _refuse_symlink(directory.parent) and _refuse_symlink(directory):
        _check_state_files(directory)
        control = _control()
        if control.is_live(directory):
            _require_compatible(
                directory,
                control,
                gateway=gateway,
                port=port,
                target_host=target_host,
                label=label,
            )
            return
    probe_bind(gateway, port, label=label)


def attach(
    gateway: str,
    port: int,
    *,
    target_host: str = "127.0.0.1",
    label: str = "Ollama",
    root_dir: Path | None = None,
    control: egress_relay.RelayControl | None = None,
    ctl_timeout: float = CTL_TIMEOUT,
    idle_poll: float | None = None,
) -> Lease:
    """Ensure a forwarder serves ``gateway:port`` -> ``target_host:port``; lease it.

    The returned lease keeps the forwarder alive until it is released (or this
    process dies). Its fd is close-on-exec, so no child process inherits it.
    """
    egress_relay.validate_target_host(target_host)
    directory = ensure_state_dir(state_dir(gateway, port, root_dir=root_dir))
    control = control or _control()
    ctl_path = directory / CTL_NAME
    ctl = _open(ctl_path, os.O_RDWR | os.O_CREAT)
    try:
        _lock(ctl, fcntl.LOCK_EX, ctl_path, ctl_timeout)
        if not control.is_live(directory):
            probe_bind(gateway, port, label=label)
            (directory / UPSTREAM_NAME).unlink(missing_ok=True)
            try:
                control.spawn_local_forward(
                    directory,
                    bind=gateway,
                    port=port,
                    target_host=target_host,
                    target_port=port,
                    idle_poll=idle_poll,
                )
            except egress_relay.RelayError as exc:
                raise ForwarderError(
                    f"Could not start the {label} forwarder on {gateway}:{port}: {exc}"
                ) from exc
        _require_compatible(
            directory,
            control,
            gateway=gateway,
            port=port,
            target_host=target_host,
            label=label,
        )
        lease_path = directory / LEASE_NAME
        lease_fd = _open(lease_path, os.O_RDWR | os.O_CREAT)
        try:
            # The forwarder takes lease exclusively only while it holds
            # ctl.lock, which this session holds now: this does not block.
            _lock(lease_fd, fcntl.LOCK_SH, lease_path, ctl_timeout)
        except BaseException:
            os.close(lease_fd)
            raise
        return Lease(lease_fd, directory)
    finally:
        os.close(ctl)  # releases ctl.lock
