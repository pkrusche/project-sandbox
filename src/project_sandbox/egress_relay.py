"""Sidecar-owned host relay to a remote parent proxy (design.md Decision 9).

Apple ``container`` cannot route to a proxy that only the host reaches (a VPN),
so the global sidecar's parent is a loopback relay on the host, reached through
``host.docker.internal``. The relay belongs to the sidecar, not to any
sandbox's CLI process, so in-flight tunnels survive the session that started
it.

``python -m project_sandbox.egress_relay --state-dir D --port P --upstream URL``
runs :class:`~project_sandbox.build_proxy_forwarder.BuildProxyForwarder` in the
foreground. For its whole life it holds an exclusive ``flock`` on
``D/relay.lock``, and it writes ``relay.pid``, ``relay.port`` (the port it
actually bound: ``P`` if free, otherwise a random one) and ``relay.log``
(truncated at start). SIGTERM stops it and releases the lock.

On Docker Engine with a parent proxy on host loopback (egress-sidecar-linux,
design.md Decision 2) the same process model runs a plain TCP forwarder instead:
``--forward --bind GATEWAY --peer SIDECAR_IP --target-port P`` listens on the
default bridge's gateway (``--port``, ``0`` for a free one), closes every
connection whose peer is not the sidecar without connecting anywhere, and
forwards the sidecar's connections to ``127.0.0.1:P``.

The shared local-service forwarder (``local_forwarder``, share-local-service-
forwarders design.md Decision 3) is the same forwarder with ``--exact-port``
(bind only ``--port``, no free-port fallback), no ``--peer`` filter, a
``--target-host`` of ``127.0.0.1`` or ``::1``, and ``--lease``: it uses the
``fwd.*`` state files and exits once no session holds a lease.

:class:`RelayControl` is the host side: spawn the relay detached
(``start_new_session``), check liveness through the lock (never through a pid,
which may have been reused), and stop it.
"""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import os
import selectors
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Self

from . import build_proxy_forwarder, proxy_endpoint, session_ca

LOCK_NAME = "relay.lock"
PID_NAME = "relay.pid"
PORT_NAME = "relay.port"
LOG_NAME = "relay.log"


@dataclass(frozen=True)
class StateFiles:
    """The names of a relay-model process's state files inside its directory."""

    lock: str
    pid: str
    port: str
    log: str


RELAY_FILES = StateFiles(lock=LOCK_NAME, pid=PID_NAME, port=PORT_NAME, log=LOG_NAME)
# Separates the relay's preferred port from the build and session forwarders.
PORT_PURPOSE = "egress-relay:"
SPAWN_TIMEOUT = 10.0
STOP_TIMEOUT = 10.0
EXIT_ALREADY_RUNNING = 3
# The forwarder connects only here, on the parent's port.
FORWARD_TARGET_HOST = "127.0.0.1"
# The only upstream hosts a forwarder connects to: host loopback, either family.
FORWARD_TARGET_HOSTS = ("127.0.0.1", "::1")
FORWARD_CONNECT_TIMEOUT = 10.0
FORWARD_POLL_INTERVAL = 0.2


class RelayError(RuntimeError):
    """The relay could not be started or stopped; the message is user-facing."""


class _RelayServer(Protocol):
    """What ``_run_locked`` needs from the server its ``start`` callback builds."""

    port: int

    def describe(self) -> str: ...

    def __enter__(self) -> object: ...

    def __exit__(self, *exc: object) -> object: ...


def preferred_port(endpoint: proxy_endpoint.ProxyEndpoint) -> int:
    return build_proxy_forwarder.preferred_port(endpoint, purpose=PORT_PURPOSE)


def _open_private(path: Path, flags: int) -> int:
    """``os.open`` a ``0600`` state file, never following a symlink."""
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.fchmod(fd, 0o600)  # independent of the umask and of an older file
    except OSError:
        os.close(fd)
        raise
    return fd


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    fd = _open_private(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)  # replaces a planted symlink itself, never its target


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------
# The relay process
# --------------------------------------------------------------------------


def _run_locked(
    state_dir: Path,
    start: Callable[[Callable[[str], None]], _RelayServer],
    *,
    stop: threading.Event | None,
    install_signals: bool,
    files: StateFiles = RELAY_FILES,
    idle: Callable[[], bool] | None = None,
    idle_poll: float = 2.0,
    on_ready: Callable[[_RelayServer], None] | None = None,
) -> int:
    """Hold ``files.lock`` and run the server ``start(note)`` builds until stopped.

    ``start`` returns a context manager with a ``port`` and a ``describe()``
    line, or raises ``SystemExit``/``OSError`` when it cannot bind.

    ``on_ready(server)`` runs once the server is serving and before the port
    file (the readiness signal ``RelayControl`` waits for) is written. With
    ``idle``, the server also stops when ``idle()`` returns true; it is called
    every ``idle_poll`` seconds.
    """
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_fd = _open_private(state_dir / files.lock, os.O_RDWR | os.O_CREAT)
    try:
        # Not a single LOCK_NB attempt: RelayControl's liveness probes (spawn
        # polls them while this process starts) take the lock for an instant.
        if not session_ca.acquire_exclusive(lock_fd):
            print("egress relay: already running", file=sys.stderr)
            return EXIT_ALREADY_RUNNING
        try:
            log_fd = _open_private(
                state_dir / files.log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            )
        except OSError as exc:  # e.g. a planted symlink (ELOOP)
            print(f"egress relay: cannot open {exc.filename}: {exc}", file=sys.stderr)
            return 1
        with os.fdopen(log_fd, "w", encoding="utf-8") as log:

            def note(message: str) -> None:
                log.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")
                log.flush()

            try:
                server = start(note)
            except (SystemExit, OSError) as exc:
                note(f"failed to start: {exc}")
                return 1
            stop = stop or threading.Event()
            if install_signals:
                for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                    signal.signal(signum, lambda *_: stop.set())
            with server:
                _write_atomic(state_dir / files.pid, f"{os.getpid()}\n")
                if on_ready is not None:
                    on_ready(server)
                _write_atomic(state_dir / files.port, f"{server.port}\n")
                note(server.describe())
                # The with block started the server.
                if idle is None:
                    stop.wait()
                    note("stopping")
                else:
                    while not stop.wait(idle_poll):
                        if idle():
                            note("no session holds a lease; stopping")
                            break
                    else:
                        note("stopping")
            for name in (files.pid, files.port):
                (state_dir / name).unlink(missing_ok=True)
            return 0
    finally:
        os.close(lock_fd)  # releases the lock


class _DescribedForwarder(build_proxy_forwarder.BuildProxyForwarder):
    def __init__(self, endpoint: proxy_endpoint.ProxyEndpoint, **kwargs) -> None:
        super().__init__(endpoint, **kwargs)
        self._endpoint = endpoint

    def describe(self) -> str:
        e = self._endpoint
        return (
            f"relaying {build_proxy_forwarder.LISTEN_HOST}:{self.port} "
            f"to {e.scheme}://{e.host}:{e.port}"
        )


def serve(
    state_dir: Path,
    *,
    upstream: str,
    port: int,
    ca_file: Path | None = None,
    stop: threading.Event | None = None,
    install_signals: bool = True,
) -> int:
    """Run the relay until ``stop`` is set or SIGTERM arrives; return an exit code."""

    def start(note: Callable[[str], None]) -> _RelayServer:
        endpoint = proxy_endpoint.parse(upstream, option="egress relay upstream")
        ca = (ca_file.read_bytes(),) if ca_file is not None else ()
        return _DescribedForwarder(
            endpoint,
            ca_certificates=ca,
            warn=note,
            port=port,
            fallback=True,
            label="Egress relay",
            ca_option="[egress.upstream] ca_cert",
        )

    return _run_locked(state_dir, start, stop=stop, install_signals=install_signals)


def validate_target_host(value: str) -> str:
    """``value`` if it is ``127.0.0.1`` or ``::1``; ``SystemExit`` otherwise."""
    if value not in FORWARD_TARGET_HOSTS:
        raise SystemExit(
            f"the forwarder connects only to host loopback "
            f"({' or '.join(FORWARD_TARGET_HOSTS)}), not {value!r}"
        )
    return value


class TcpForwarder:
    """Forward connections from a bridge gateway address to host loopback.

    Binds ``bind:port`` on construction (``port`` busy: a free one, unless
    ``exact_port``, which fails instead). With a ``peer``, each accepted
    connection whose peer address is not ``peer`` is closed before anything is
    connected upstream (egress-sidecar-linux design.md Decision 2); without
    one, every peer is forwarded, as the local-service forwarder does.
    """

    def __init__(
        self,
        *,
        bind: str,
        peer: str | None,
        target_port: int,
        port: int = 0,
        note: Callable[[str], None] = lambda _m: None,
        target_host: str = FORWARD_TARGET_HOST,
        exact_port: bool = False,
    ) -> None:
        address = ipaddress.ip_address(bind)  # an address, never a name
        if address.is_unspecified:
            raise SystemExit("the parent forwarder must not listen on a wildcard")
        if exact_port and not port:
            raise SystemExit("an exact-port forwarder needs a non-zero port")
        self._peer = str(ipaddress.ip_address(peer)) if peer is not None else None
        self._bind_address = bind
        self._family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        self._target = (validate_target_host(target_host), target_port)
        self._exact_port = exact_port
        self._note = note
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._connections: set[socket.socket] = set()
        self._thread: threading.Thread | None = None
        self._listener = self._bind(port)
        self.port: int = self._listener.getsockname()[1]
        self.refused = 0

    def _bind(self, port: int) -> socket.socket:
        if self._exact_port:
            candidates: tuple[int, ...] = (port,)
        else:
            candidates = (port, 0) if port else (0,)
        for candidate in candidates:
            listener = socket.socket(self._family, socket.SOCK_STREAM)
            try:
                # Like socat's reuseaddr: TIME_WAIT sockets left by a forwarder
                # that just exited must not block its replacement. A live
                # listener on the address still fails the bind.
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind((self._bind_address, candidate))
                listener.listen(socket.SOMAXCONN)
            except OSError as exc:
                listener.close()
                if candidate == 0 or self._exact_port:
                    raise SystemExit(
                        "Could not start the forwarder on "
                        f"{self._bind_address}:{candidate}: {exc}"
                    ) from exc
                self._note(f"port {candidate} is in use; using a free port")
                continue
            return listener
        raise AssertionError("unreachable")

    def describe(self) -> str:
        host, port = self._target
        target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        peers = f"peer {self._peer} only" if self._peer else "any peer"
        return f"forwarding {self._bind_address}:{self.port} to {target} for {peers}"

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._serve, name="egress-parent-forwarder", daemon=True
            )
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5)
        self._listener.close()
        with self._lock:
            connections = list(self._connections)
            self._connections.clear()
        for connection in connections:
            connection.close()

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _track(self, *connections: socket.socket) -> bool:
        with self._lock:
            if self._stop.is_set():
                for connection in connections:
                    connection.close()
                return False
            self._connections.update(connections)
            return True

    def _untrack(self, *connections: socket.socket) -> None:
        with self._lock:
            self._connections.difference_update(connections)
        for connection in connections:
            connection.close()

    def _serve(self) -> None:
        with selectors.DefaultSelector() as selector:
            selector.register(self._listener, selectors.EVENT_READ)
            while not self._stop.is_set():
                if not selector.select(timeout=FORWARD_POLL_INTERVAL):
                    continue
                try:
                    client, address = self._listener.accept()
                except OSError:
                    continue
                if self._peer is not None and address[0] != self._peer:
                    # Every container on any bridge reaches the gateway; only
                    # the sidecar may use the parent.
                    self.refused += 1
                    self._note(f"refused peer {address[0]}")
                    with contextlib.suppress(OSError):
                        client.close()
                    continue
                if self._track(client):
                    threading.Thread(
                        target=self._handle, args=(client,), daemon=True
                    ).start()

    def _handle(self, client: socket.socket) -> None:
        try:
            upstream = socket.create_connection(
                self._target, timeout=FORWARD_CONNECT_TIMEOUT
            )
        except OSError as exc:
            self._note(
                f"could not connect to {self._target[0]}:{self._target[1]}: {exc}"
            )
            self._untrack(client)
            return
        upstream.settimeout(None)
        if not self._track(upstream):
            self._untrack(client)
            return
        try:
            build_proxy_forwarder._relay(client, upstream, self._stop)
        except OSError:
            pass
        finally:
            self._untrack(client, upstream)


def serve_forward(
    state_dir: Path,
    *,
    bind: str,
    peer: str | None,
    target_port: int,
    port: int = 0,
    target_host: str = FORWARD_TARGET_HOST,
    exact_port: bool = False,
    stop: threading.Event | None = None,
    install_signals: bool = True,
    files: StateFiles = RELAY_FILES,
    idle: Callable[[], bool] | None = None,
    idle_poll: float = 2.0,
    on_ready: Callable[[_RelayServer], None] | None = None,
) -> int:
    """Run a TCP forwarder (sidecar parent or local service) as a relay process."""

    def start(note: Callable[[str], None]) -> _RelayServer:
        return TcpForwarder(
            bind=bind,
            peer=peer,
            target_port=target_port,
            port=port,
            note=note,
            target_host=target_host,
            exact_port=exact_port,
        )

    return _run_locked(
        state_dir,
        start,
        stop=stop,
        install_signals=install_signals,
        files=files,
        idle=idle,
        idle_poll=idle_poll,
        on_ready=on_ready,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m project_sandbox.egress_relay")
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--upstream")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--ca-file", type=Path)
    parser.add_argument("--forward", action="store_true")
    parser.add_argument("--bind")
    parser.add_argument("--peer")
    parser.add_argument("--target-port", type=int)
    parser.add_argument(
        "--target-host", choices=FORWARD_TARGET_HOSTS, default=FORWARD_TARGET_HOST
    )
    parser.add_argument("--exact-port", action="store_true")
    parser.add_argument("--lease", action="store_true")
    parser.add_argument("--idle-poll", type=float)
    args = parser.parse_args(argv)
    if args.forward:
        if not (args.bind and args.target_port):
            parser.error("--forward needs --bind and --target-port")
        # Only an exact-port forwarder may drop the peer filter, so the sidecar
        # parent forwarder cannot lose it by accident.
        if not args.peer and not args.exact_port:
            parser.error("--forward needs --peer unless --exact-port is given")
        if args.exact_port and not args.port:
            parser.error("--exact-port needs a non-zero --port")
        if args.lease:
            if args.peer or not args.exact_port:
                parser.error("--lease needs --exact-port and no --peer")
            from . import local_forwarder  # imports this module

            return local_forwarder.serve(
                args.state_dir,
                bind=args.bind,
                port=args.port,
                target_host=args.target_host,
                target_port=args.target_port,
                idle_poll=args.idle_poll or local_forwarder.IDLE_POLL,
            )
        return serve_forward(
            args.state_dir,
            bind=args.bind,
            peer=args.peer,
            target_port=args.target_port,
            port=args.port,
            target_host=args.target_host,
            exact_port=args.exact_port,
        )
    if args.lease:
        parser.error("--lease needs --forward")
    if not args.upstream:
        parser.error("--upstream is required")
    return serve(
        args.state_dir, upstream=args.upstream, port=args.port, ca_file=args.ca_file
    )


# --------------------------------------------------------------------------
# Host-side control
# --------------------------------------------------------------------------


class RelayLike(Protocol):
    """What the sidecar needs from a relay controller (tests inject fakes)."""

    def is_live(self, state_dir: Path) -> bool: ...

    def port(self, state_dir: Path) -> int | None: ...

    def spawn(
        self,
        state_dir: Path,
        *,
        upstream: str,
        port: int,
        ca_file: Path | None = None,
    ) -> int: ...

    def spawn_forward(
        self,
        state_dir: Path,
        *,
        bind: str,
        peer: str,
        target_port: int,
        port: int = 0,
    ) -> int: ...

    def stop(self, state_dir: Path) -> bool: ...


class RelayControl:
    """Spawn, probe and stop the relay; every collaborator is injectable."""

    def __init__(
        self,
        *,
        popen: Callable[..., object] = subprocess.Popen,
        kill: Callable[[int, int], None] = os.kill,
        python: str = sys.executable,
        spawn_timeout: float = SPAWN_TIMEOUT,
        stop_timeout: float = STOP_TIMEOUT,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        files: StateFiles = RELAY_FILES,
    ) -> None:
        self._files = files
        self._popen = popen
        self._kill = kill
        self._python = python
        self._spawn_timeout = spawn_timeout
        self._stop_timeout = stop_timeout
        self._sleep = sleep
        self._clock = clock

    def is_live(self, state_dir: Path) -> bool:
        return session_ca.lock_held(state_dir / self._files.lock)

    def port(self, state_dir: Path) -> int | None:
        """The bound port of a live relay, or ``None``."""
        if not self.is_live(state_dir):
            return None
        return _read_int(state_dir / self._files.port)

    def spawn(
        self,
        state_dir: Path,
        *,
        upstream: str,
        port: int,
        ca_file: Path | None = None,
    ) -> int:
        """Start the relay detached and return the port it bound."""
        argv = [
            self._python,
            "-m",
            "project_sandbox.egress_relay",
            "--state-dir",
            str(state_dir),
            "--upstream",
            upstream,
            "--port",
            str(port),
        ]
        if ca_file is not None:
            argv += ["--ca-file", str(ca_file)]
        return self._start(state_dir, argv)

    def spawn_forward(
        self,
        state_dir: Path,
        *,
        bind: str,
        peer: str,
        target_port: int,
        port: int = 0,
    ) -> int:
        """Start the Docker Engine parent forwarder detached; return its port."""
        argv = [
            self._python,
            "-m",
            "project_sandbox.egress_relay",
            "--state-dir",
            str(state_dir),
            "--forward",
            "--bind",
            bind,
            "--peer",
            peer,
            "--target-port",
            str(target_port),
            "--port",
            str(port),
        ]
        return self._start(state_dir, argv)

    def spawn_local_forward(
        self,
        state_dir: Path,
        *,
        bind: str,
        port: int,
        target_host: str,
        target_port: int,
        idle_poll: float | None = None,
    ) -> int:
        """Start a shared local-service forwarder detached; return its port.

        It binds exactly ``bind:port``, forwards every peer to
        ``target_host:target_port``, and exits once no session holds a lease
        (see ``local_forwarder``).
        """
        argv = [
            self._python,
            "-m",
            "project_sandbox.egress_relay",
            "--state-dir",
            str(state_dir),
            "--forward",
            "--lease",
            "--exact-port",
            "--bind",
            bind,
            "--target-host",
            validate_target_host(target_host),
            "--target-port",
            str(target_port),
            "--port",
            str(port),
        ]
        if idle_poll is not None:
            argv += ["--idle-poll", str(idle_poll)]
        return self._start(state_dir, argv)

    def _start(self, state_dir: Path, argv: list[str]) -> int:
        (state_dir / self._files.port).unlink(missing_ok=True)
        # A new session: terminal signals and the CLI's own signal handling do
        # not reach it, and it outlives the launching CLI. No preexec_fn, so
        # spawning is fork-safe from a multi-threaded parent.
        process = self._popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        deadline = self._clock() + self._spawn_timeout
        while True:
            bound = self.port(state_dir)
            if bound is not None:
                return bound
            poll = getattr(process, "poll", None)
            returncode = poll() if callable(poll) else None
            if returncode is not None:
                raise RelayError(
                    f"the egress relay exited with status {returncode} "
                    "before it was ready.\n" + self.log_tail(state_dir)
                )
            if self._clock() >= deadline:
                raise RelayError(
                    "the egress relay did not start within "
                    f"{self._spawn_timeout:.0f}s.\n" + self.log_tail(state_dir)
                )
            self._sleep(0.1)

    def stop(self, state_dir: Path) -> bool:
        """SIGTERM a live relay and wait for its lock; ``False`` if none ran.

        A pid is signalled only while the relay lock is held, so a stale pid
        file (the relay died, the pid was reused) is never acted on.
        """
        if not self.is_live(state_dir):
            return False
        pid = _read_int(state_dir / self._files.pid)
        if pid is None or pid <= 1:
            raise RelayError(
                f"the egress relay holds {state_dir / self._files.lock} "
                "but has no pid file"
            )
        try:
            self._kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = self._clock() + self._stop_timeout
        while self.is_live(state_dir):
            if self._clock() >= deadline:
                raise RelayError(f"the egress relay (pid {pid}) did not stop")
            self._sleep(0.1)
        return True

    def log_tail(self, state_dir: Path, lines: int = 10) -> str:
        try:
            text = (state_dir / self._files.log).read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            return "Relay log: (none)"
        tail = text.strip().splitlines()[-lines:]
        return "Relay log (last lines):\n" + "\n".join(f"  {line}" for line in tail)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
