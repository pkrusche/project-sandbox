"""Shared fixtures for local-forwarder tests: a second loopback and an echo service."""

import socket
import threading
import time
import unittest

BIND = "127.0.0.2"


def can_bind(host: str, port: int = 0) -> bool:
    try:
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))
    except OSError:
        return False
    return True


def has_ipv6_loopback() -> bool:
    try:
        with socket.socket(socket.AF_INET6) as probe:
            probe.bind(("::1", 0))
    except OSError:
        return False
    return True


requires_second_loopback = unittest.skipUnless(
    can_bind(BIND), f"{BIND} is not bindable here (Linux routes all of 127/8)"
)


class Echo:
    """An echo service on ``host`` whose port is also free on ``BIND``."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        for _ in range(50):
            server = socket.create_server((host, port), family=family)
            chosen = server.getsockname()[1]
            if port or can_bind(BIND, chosen):
                break
            server.close()
        else:  # pragma: no cover
            raise RuntimeError("no port free on both addresses")
        self.server = server
        self.port = chosen
        self.accepted = 0
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            self.accepted += 1
            threading.Thread(target=self._echo, args=(conn,), daemon=True).start()

    @staticmethod
    def _echo(conn: socket.socket) -> None:
        with conn:
            while chunk := conn.recv(4096):
                conn.sendall(chunk)

    def close(self) -> None:
        self.server.close()


def roundtrip(port: int, payload: bytes = b"ping") -> bytes:
    with socket.create_connection((BIND, port), timeout=5) as c:
        c.sendall(payload)
        return c.recv(4096)


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()
