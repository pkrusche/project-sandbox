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

:class:`RelayControl` is the host side: spawn the relay detached
(``start_new_session``), check liveness through the lock (never through a pid,
which may have been reused), and stop it.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

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
                endpoint = proxy_endpoint.parse(
                    upstream, option="egress relay upstream"
                )
                ca = (ca_file.read_bytes(),) if ca_file is not None else ()
                forwarder = build_proxy_forwarder.BuildProxyForwarder(
                    endpoint,
                    ca_certificates=ca,
                    warn=note,
                    port=port,
                    fallback=True,
                    label="Egress relay",
                    ca_option="[egress.upstream] ca_cert",
                )
            except (SystemExit, OSError) as exc:
                note(f"failed to start: {exc}")
                return 1
            stop = stop or threading.Event()
            if install_signals:
                for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                    signal.signal(signum, lambda *_: stop.set())
            with forwarder:
                _write_atomic(state_dir / PID_NAME, f"{os.getpid()}\n")
                _write_atomic(state_dir / PORT_NAME, f"{forwarder.port}\n")
                note(
                    f"relaying {build_proxy_forwarder.LISTEN_HOST}:{forwarder.port} "
                    f"to {endpoint.scheme}://{endpoint.host}:{endpoint.port}"
                )
                stop.wait()  # the with block started the forwarder
                note("stopping")
            for name in (PID_NAME, PORT_NAME):
                (state_dir / name).unlink(missing_ok=True)
            return 0
    finally:
        os.close(lock_fd)  # releases the lock


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m project_sandbox.egress_relay")
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--ca-file", type=Path)
    args = parser.parse_args(argv)
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
        (state_dir / PORT_NAME).unlink(missing_ok=True)
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
