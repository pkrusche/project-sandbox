"""Host-loopback forwarder that lets Apple container builds reach a VPN-only proxy.

Apple container's bridge network cannot route to a proxy that is only reachable
through the host's VPN. The configured localhost alias does reach the host's
loopback, so a short-lived standard-library forwarder relays builder
connections from ``127.0.0.1`` to the real proxy, resolved and connected from
the host. HTTPS proxies are re-originated with verified TLS from the host, so
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
# outgoing connection. A stable port keeps rendered Dockerfiles and the
# image layer cache unchanged between runs.
PORT_BASE = 40000
PORT_SPAN = 9000
CONNECT_TIMEOUT = 10.0
POLL_INTERVAL = 0.2
BUFFER_SIZE = 65536


def preferred_port(endpoint: proxy_endpoint.ProxyEndpoint) -> int:
    key = f"{endpoint.scheme}://{endpoint.host}:{endpoint.port}".encode()
    return PORT_BASE + zlib.crc32(key) % PORT_SPAN


def sandbox_url(port: int) -> str:
    """Proxy URL as seen from the Apple container builder."""
    return f"http://{APPLE_LOCALHOST_ADDRESS}:{port}"


class BuildProxyForwarder:
    """Bind on construction; relay only between ``start()`` and ``close()``."""

    def __init__(
        self,
        endpoint: proxy_endpoint.ProxyEndpoint,
        *,
        ca_certificates: tuple[bytes, ...] = (),
        warn=print,
    ) -> None:
        self._upstream = endpoint
        self._tls = (
            proxy_endpoint.tls_context(ca_certificates)
            if endpoint.scheme == "https"
            else None
        )
        self._warn = warn
        self._warned = False
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._connections: set[socket.socket] = set()
        self._thread: threading.Thread | None = None
        self._listener = self._bind(preferred_port(endpoint))
        self.port: int = self._listener.getsockname()[1]

    @property
    def sandbox_url(self) -> str:
        return sandbox_url(self.port)

    def _bind(self, port: int) -> socket.socket:
        for candidate in (port, 0):
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # Lets a rerun rebind the stable port while old connections are in
            # TIME_WAIT; it never allows binding over an active listener.
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.bind((LISTEN_HOST, candidate))
                listener.listen(socket.SOMAXCONN)
            except OSError as exc:
                listener.close()
                if candidate == 0:
                    raise SystemExit(
                        f"Could not start the build proxy forwarder: {exc}"
                    ) from exc
                continue
            if candidate == 0:
                self._warn(
                    f"[W] Build proxy forwarder port {port} is in use; using a "
                    "random port, which invalidates the image build cache."
                )
            return listener
        raise AssertionError("unreachable")

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._serve, name="build-proxy-forwarder", daemon=True
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
                "supply the proxy's CA with --build-ca-cert"
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
            self._warn(f"[W] Build proxy forwarder {detail}")

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
        except OSError:
            pass
        finally:
            self._untrack(client, upstream)


def _relay(client: socket.socket, upstream: socket.socket, stop: threading.Event):
    """Copy bytes both ways on one thread (TLS objects are not thread-safe)."""
    peers = {client: upstream, upstream: client}
    readable = {client, upstream}
    with selectors.DefaultSelector() as selector:
        for connection in readable:
            selector.register(connection, selectors.EVENT_READ)
        while readable and not stop.is_set():
            # Decrypted TLS data can be buffered without the socket polling ready.
            pending = {
                connection
                for connection in readable
                if isinstance(connection, ssl.SSLSocket) and connection.pending()
            }
            events = selector.select(timeout=0 if pending else POLL_INTERVAL)
            for source in pending | {key.fileobj for key, _ in events}:
                assert isinstance(source, socket.socket)
                if source not in readable:
                    continue
                data = source.recv(BUFFER_SIZE)
                if data:
                    peers[source].sendall(data)
                    continue
                if source is upstream or isinstance(upstream, ssl.SSLSocket):
                    # The proxy finished, or a TLS upstream cannot half-close.
                    return
                # Forward the client's half-close and drain the proxy's reply.
                readable.discard(source)
                selector.unregister(source)
                upstream.shutdown(socket.SHUT_WR)
