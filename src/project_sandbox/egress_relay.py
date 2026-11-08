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
from pathlib import Path
from typing import Protocol, Self

from . import build_proxy_forwarder, proxy_endpoint, session_ca

LOCK_NAME = "relay.lock"
PID_NAME = "relay.pid"
PORT_NAME = "relay.port"
LOG_NAME = "relay.log"
# Separates the relay's preferred port from the build and session forwarders.
PORT_PURPOSE = "egress-relay:"
SPAWN_TIMEOUT = 10.0
STOP_TIMEOUT = 10.0
EXIT_ALREADY_RUNNING = 3
# The forwarder connects only here, on the parent's port.
FORWARD_TARGET_HOST = "127.0.0.1"
FORWARD_CONNECT_TIMEOUT = 10.0
FORWARD_POLL_INTERVAL = 0.2


class RelayError(RuntimeError):
    """The relay could not be started or stopped; the message is user-facing."""


def preferred_port(endpoint: proxy_endpoint.ProxyEndpoint) -> int:
    return build_proxy_forwarder.preferred_port(endpoint, purpose=PORT_PURPOSE)


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


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
    start: Callable[[Callable[[str], None]], object],
    *,
    stop: threading.Event | None,
    install_signals: bool,
) -> int:
    """Hold ``relay.lock`` and run the server ``start(note)`` builds until stopped.

    ``start`` returns a context manager with a ``port`` and a ``describe()``
    line, or raises ``SystemExit``/``OSError`` when it cannot bind.
    """
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_fd = os.open(
        state_dir / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    try:
        # Not a single LOCK_NB attempt: RelayControl's liveness probes (spawn
        # polls them while this process starts) take the lock for an instant.
        if not session_ca.acquire_exclusive(lock_fd):
            print("egress relay: already running", file=sys.stderr)
            return EXIT_ALREADY_RUNNING
        with (state_dir / LOG_NAME).open("w", encoding="utf-8") as log:

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
            with server:  # type: ignore[attr-defined]
                _write_atomic(state_dir / PID_NAME, f"{os.getpid()}\n")
                _write_atomic(state_dir / PORT_NAME, f"{server.port}\n")  # type: ignore[attr-defined]
                note(server.describe())  # type: ignore[attr-defined]
                stop.wait()  # the with block started the server
                note("stopping")
            for name in (PID_NAME, PORT_NAME):
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

    def start(note: Callable[[str], None]) -> object:
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


class TcpForwarder:
    """Forward the sidecar's connections from the gateway to a loopback parent.

    Binds ``bind:port`` on construction (``port`` busy: a free one). Each
    accepted connection whose peer address is not ``peer`` is closed before
    anything is connected upstream (design.md Decision 2).
    """

    def __init__(
        self,
        *,
        bind: str,
        peer: str,
        target_port: int,
        port: int = 0,
        note: Callable[[str], None] = lambda _m: None,
        target_host: str = FORWARD_TARGET_HOST,
    ) -> None:
        ipaddress.ip_address(bind)  # an address, never a name or wildcard
        if ipaddress.ip_address(bind).is_unspecified:
            raise SystemExit("the parent forwarder must not listen on a wildcard")
        self._peer = str(ipaddress.ip_address(peer))
        self._bind_address = bind
        self._target = (target_host, target_port)
        self._note = note
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._connections: set[socket.socket] = set()
        self._thread: threading.Thread | None = None
        self._listener = self._bind(port)
        self.port: int = self._listener.getsockname()[1]
        self.refused = 0

    def _bind(self, port: int) -> socket.socket:
        for candidate in (port, 0) if port else (0,):
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.bind((self._bind_address, candidate))
                listener.listen(socket.SOMAXCONN)
            except OSError as exc:
                listener.close()
                if candidate == 0:
                    raise SystemExit(
                        "Could not start the egress parent forwarder on "
                        f"{self._bind_address}: {exc}"
                    ) from exc
                self._note(f"port {candidate} is in use; using a free port")
                continue
            return listener
        raise AssertionError("unreachable")

    def describe(self) -> str:
        host, port = self._target
        return (
            f"forwarding {self._bind_address}:{self.port} to {host}:{port} "
            f"for peer {self._peer} only"
        )

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
                if address[0] != self._peer:
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
    peer: str,
    target_port: int,
    port: int = 0,
    stop: threading.Event | None = None,
    install_signals: bool = True,
) -> int:
    """Run the Docker Engine parent forwarder under the relay's process model."""

    def start(note: Callable[[str], None]) -> object:
        return TcpForwarder(
            bind=bind, peer=peer, target_port=target_port, port=port, note=note
        )

    return _run_locked(state_dir, start, stop=stop, install_signals=install_signals)


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
    args = parser.parse_args(argv)
    if args.forward:
        if not (args.bind and args.peer and args.target_port):
            parser.error("--forward needs --bind, --peer and --target-port")
        return serve_forward(
            args.state_dir,
            bind=args.bind,
            peer=args.peer,
            target_port=args.target_port,
            port=args.port,
        )
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
    ) -> None:
        self._popen = popen
        self._kill = kill
        self._python = python
        self._spawn_timeout = spawn_timeout
        self._stop_timeout = stop_timeout
        self._sleep = sleep
        self._clock = clock

    def is_live(self, state_dir: Path) -> bool:
        return session_ca.lock_held(state_dir / LOCK_NAME)

    def port(self, state_dir: Path) -> int | None:
        """The bound port of a live relay, or ``None``."""
        return _read_int(state_dir / PORT_NAME) if self.is_live(state_dir) else None

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

    def _start(self, state_dir: Path, argv: list[str]) -> int:
        (state_dir / PORT_NAME).unlink(missing_ok=True)
        # A new session: terminal signals and the CLI's own signal handling do
        # not reach it, and it outlives the launching CLI.
        self._popen(
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
        pid = _read_int(state_dir / PID_NAME)
        if pid is None or pid <= 1:
            raise RelayError(
                f"the egress relay holds {state_dir / LOCK_NAME} but has no pid file"
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

    @staticmethod
    def log_tail(state_dir: Path, lines: int = 10) -> str:
        try:
            text = (state_dir / LOG_NAME).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "Relay log: (none)"
        tail = text.strip().splitlines()[-lines:]
        return "Relay log (last lines):\n" + "\n".join(f"  {line}" for line in tail)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
