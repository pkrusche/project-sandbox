"""Host-loopback forwarder that lets Apple container reach a VPN-only proxy.

Apple container's bridge network cannot route to a proxy that is only reachable
through the host's VPN. The configured localhost alias does reach the host's
loopback, so a standard-library forwarder relays builder (``--build-proxy``)
or sandbox (``--internet-proxy``) connections from ``127.0.0.1`` to the real
proxy, resolved and connected from the host. HTTPS proxies are re-originated with verified TLS from the host, so
the builder speaks plain HTTP to the forwarder.
"""

from __future__ import annotations

import selectors
import socket
import ssl
import threading
import zlib
from typing import Self

from . import proxy_endpoint
from .local_service_network import APPLE_LOCALHOST_ADDRESS

LISTEN_HOST = "127.0.0.1"
# Below macOS's ephemeral range, so the preferred port is rarely taken by an
# outgoing connection. The port only reaches the builder as a predefined proxy
# build-arg, so a fallback port changes neither Dockerfiles nor the layer cache.
PORT_BASE = 40000
PORT_SPAN = 9000
CONNECT_TIMEOUT = 10.0
POLL_INTERVAL = 0.2
BUFFER_SIZE = 65536
# Per-direction backlog; reading pauses while the peer cannot keep up.
BUFFER_LIMIT = 4 * BUFFER_SIZE


def preferred_port(endpoint: proxy_endpoint.ProxyEndpoint, *, purpose: str = "") -> int:
    """Derive a stable port; ``purpose`` separates concurrently used relays."""
    key = f"{purpose}{endpoint.scheme}://{endpoint.host}:{endpoint.port}".encode()
    return PORT_BASE + zlib.crc32(key) % PORT_SPAN


def sandbox_url(port: int) -> str:
    """Proxy URL as seen from the Apple container builder."""
    return f"http://{APPLE_LOCALHOST_ADDRESS}:{port}"


class BuildProxyForwarder:
    """Bind on construction; relay only between ``start()`` and ``close()``.

    ``port`` without ``fallback`` makes the listening port part of the
    contract: the runtime firewall bakes it in, so a busy port is an error.
    """

    def __init__(
        self,
        endpoint: proxy_endpoint.ProxyEndpoint,
        *,
        ca_certificates: tuple[bytes, ...] = (),
        warn=print,
        port: int | None = None,
        fallback: bool = True,
        label: str = "Build proxy",
        ca_option: str = "--build-ca-cert",
    ) -> None:
        self._upstream = endpoint
        self._tls = (
            proxy_endpoint.tls_context(ca_certificates)
            if endpoint.scheme == "https"
            else None
        )
        self._warn = warn
        self._warned = False
        self._label = label
        self._ca_option = ca_option
        self._fallback = fallback
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._connections: set[socket.socket] = set()
        self._thread: threading.Thread | None = None
        self._listener = self._bind(
            port if port is not None else preferred_port(endpoint)
        )
        self.port: int = self._listener.getsockname()[1]

    @property
    def sandbox_url(self) -> str:
        return sandbox_url(self.port)

    def _bind(self, port: int) -> socket.socket:
        for candidate in (port, 0) if self._fallback else (port,):
            # No SO_REUSEADDR: on BSD/macOS it would allow binding 127.0.0.1:P
            # while another process listens on *:P. Without it the kernel
            # atomically refuses any port another socket owns; a port held only
            # by TIME_WAIT connections just falls back, which costs no rebuild.
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.bind((LISTEN_HOST, candidate))
                listener.listen(socket.SOMAXCONN)
            except OSError as exc:
                listener.close()
                if candidate == 0 or not self._fallback:
                    raise SystemExit(
                        f"Could not start the {self._label.lower()} forwarder on "
                        f"{LISTEN_HOST}:{candidate}: {exc}"
                    ) from exc
                continue
            if candidate != port:
                self._warn(
                    f"{self._label} forwarder port {port} is in use; using a random port."
                )
            return listener
        raise AssertionError("unreachable")

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._serve,
            name=f"{self._label.lower().replace(' ', '-')}-forwarder",
            daemon=True,
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

    def _track(self, connection: socket.socket) -> bool:
        with self._lock:
            if self._stop.is_set():
                connection.close()
                return False
            self._connections.add(connection)
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
                if not selector.select(timeout=POLL_INTERVAL):
                    continue
                try:
                    client, _ = self._listener.accept()
                except OSError:
                    continue
                if self._track(client):
                    threading.Thread(
                        target=self._handle, args=(client,), daemon=True
                    ).start()

    def _connect_upstream(self) -> socket.socket | None:
        host, port = self._upstream.host, self._upstream.port
        try:
            upstream = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
        except OSError as exc:
            self._warn_once(
                f"could not connect to {host}:{port} ({exc.strerror or exc})"
            )
            return None
        if self._tls is None:
            upstream.settimeout(None)
            return upstream
        try:
            secure = self._tls.wrap_socket(upstream, server_hostname=host)
        except ssl.SSLCertVerificationError:
            upstream.close()
            self._warn_once(
                f"TLS certificate verification failed for {host}:{port}; "
                f"supply the proxy's CA with {self._ca_option}"
            )
            return None
        except OSError as exc:
            upstream.close()
            self._warn_once(f"TLS handshake with {host}:{port} failed ({exc})")
            return None
        secure.settimeout(None)
        return secure

    def _warn_once(self, detail: str) -> None:
        if not self._warned:
            self._warned = True
            self._warn(f"[W] {self._label} forwarder {detail}")

    def _handle(self, client: socket.socket) -> None:
        upstream = self._connect_upstream()
        if upstream is None:
            self._untrack(client)
            return
        if not self._track(upstream):
            self._untrack(client)
            return
        try:
            _relay(client, upstream, self._stop)
        except (OSError, ssl.SSLError):
            pass
        finally:
            self._untrack(client, upstream)


class _Direction:
    """One copy direction with a bounded backlog and EOF bookkeeping."""

    def __init__(self, source: socket.socket, sink: socket.socket) -> None:
        self.source = source
        self.sink = sink
        self.buffer = bytearray()
        self.eof = False
        self.sink_closed = False

    @property
    def done(self) -> bool:
        return self.eof and not self.buffer


def _relay(client: socket.socket, upstream: socket.socket, stop: threading.Event):
    """Copy bytes both ways on one thread (TLS objects are not thread-safe).

    Sockets are non-blocking and each direction buffers at most BUFFER_LIMIT
    bytes, so a peer that stops reading only pauses the opposite read instead
    of blocking the thread (and with it the other direction).
    """
    tls_upstream = isinstance(upstream, ssl.SSLSocket)
    up = _Direction(client, upstream)
    down = _Direction(upstream, client)
    directions = (up, down)
    # TLS operations may need the opposite readiness (SSLWantRead/WantWrite).
    wants: dict[socket.socket, int] = {client: 0, upstream: 0}
    for connection in (client, upstream):
        connection.setblocking(False)
    with selectors.DefaultSelector() as selector:
        registered: dict[socket.socket, int] = {}
        while not stop.is_set():
            if down.done:
                # The proxy finished and its reply was delivered.
                return
            if up.done and not up.sink_closed:
                # Forward the client's half-close. TLS cannot half-close, so a
                # TLS upstream just stops receiving while its reply is drained.
                up.sink_closed = True
                if not tls_upstream:
                    upstream.shutdown(socket.SHUT_WR)
            interest = dict(wants)
            for direction in directions:
                if not direction.eof and len(direction.buffer) < BUFFER_LIMIT:
                    interest[direction.source] |= selectors.EVENT_READ
                if direction.buffer:
                    interest[direction.sink] |= selectors.EVENT_WRITE
            for connection, events in interest.items():
                if events == registered.get(connection, 0):
                    continue
                if not events:
                    selector.unregister(connection)
                    del registered[connection]
                    continue
                if connection in registered:
                    selector.modify(connection, events)
                else:
                    selector.register(connection, events)
                registered[connection] = events
            # Decrypted TLS data can be buffered without the socket polling ready.
            pending = {
                direction
                for direction in directions
                if interest[direction.source] & selectors.EVENT_READ
                and isinstance(direction.source, ssl.SSLSocket)
                and direction.source.pending()
            }
            ready = selector.select(timeout=0 if pending else POLL_INTERVAL)
            readiness = {key.fileobj: mask for key, mask in ready}
            wants = {client: 0, upstream: 0}
            for direction in directions:
                if (
                    direction in pending
                    or readiness.get(direction.source, 0) & (interest[direction.source])
                ):
                    _pump_read(direction, wants)
                if direction.buffer and readiness.get(direction.sink, 0):
                    _pump_write(direction, wants)


def _pump_read(direction: _Direction, wants: dict[socket.socket, int]) -> None:
    if direction.eof or len(direction.buffer) >= BUFFER_LIMIT:
        return
    try:
        data = direction.source.recv(BUFFER_SIZE)
    except ssl.SSLWantReadError:
        wants[direction.source] |= selectors.EVENT_READ
        return
    except ssl.SSLWantWriteError:
        wants[direction.source] |= selectors.EVENT_WRITE
        return
    except BlockingIOError:
        return
    except (ssl.SSLZeroReturnError, ssl.SSLEOFError):
        data = b""
    if data:
        direction.buffer += data
    else:
        direction.eof = True


def _pump_write(direction: _Direction, wants: dict[socket.socket, int]) -> None:
    try:
        sent = direction.sink.send(direction.buffer)
    except ssl.SSLWantReadError:
        wants[direction.sink] |= selectors.EVENT_READ
        return
    except ssl.SSLWantWriteError:
        wants[direction.sink] |= selectors.EVENT_WRITE
        return
    except BlockingIOError:
        return
    del direction.buffer[:sent]
