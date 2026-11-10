"""Kill the real sole CLI owner; no model inference or credentials required."""

import http.server
import socket
import threading

import pytest
from conftest import wait_for

from project_sandbox import local_forwarder, local_service_network


class Tags(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"models": []}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_only_pi_ollama_owner_sigkill_and_reattach(sandbox):
    plan = local_service_network.prepare(
        sandbox.runtime, port=11434, label="Ollama E2E"
    )
    if plan.strategy != local_service_network.BRIDGE_STRATEGY:
        pytest.skip(
            f"{plan.strategy} uses no shared bridge forwarder; run on Linux Docker Engine"
        )
    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 11434), Tags)
    except OSError:
        pytest.skip(
            "port 11434 is occupied; use a host without a running Ollama for the deterministic crash fixture"
        )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    project = sandbox.project()
    (project / "fake-pi").write_text(
        "#!/bin/sh\nexec python3 -I /workspace/ollama_probe.py\n"
    )
    (project / "Dockerfile").write_text(
        "FROM python:3.12-slim\nCOPY fake-pi /usr/local/bin/security-pi\n"
        "RUN chmod +x /usr/local/bin/security-pi && ln -sf /usr/local/bin/security-pi /usr/local/bin/pi\n"
    )
    (
        project / "ollama_probe.py"
    ).write_text("""import json, os, pathlib, time, urllib.request
p = pathlib.Path('/workspace')
p.joinpath('container-id').write_text(os.uname().nodename)
with urllib.request.urlopen('http://host.docker.internal:11434/api/tags', timeout=10) as response:
    assert 'models' in json.load(response)
p.joinpath('ready').touch()
end = time.monotonic() + 180
while not p.joinpath('release').exists():
    assert time.monotonic() < end
    time.sleep(.1)
""")
    extra = ["--pi-ollama", "--dockerfile", str(project / "Dockerfile")]
    try:
        argv = sandbox.argv(project, build=True, extra=extra, base=None)
        argv[argv.index("--agent") + 1] = "pi"
        argv += ["--build-only"]
        sandbox.finish(sandbox.start(argv), timeout=1800)

        def launch():
            argv = sandbox.argv(project, extra=extra, base=None)
            argv[argv.index("--agent") + 1] = "pi"
            # Exercise the baked firewall exception from --build-only too.
            argv.remove("--no-firewall")
            return sandbox.start(argv)

        first = launch()
        wait_for(lambda: (project / "ready").exists(), processes=(first,))
        state = sandbox.directory / "state/project-sandbox/local-forward"
        pids = list(state.glob("*/fwd.pid"))
        assert len(pids) == 1, "expected exactly one shared forwarder"
        old_pid = pids[0].read_text()
        first.kill()
        first.wait(timeout=10)

        def released():
            try:
                with socket.create_connection((plan.endpoint, 11434), timeout=0.2):
                    return False
            except OSError:
                return True

        wait_for(released, timeout=local_forwarder.IDLE_POLL + 5)
        # Stop the now orphaned container before starting a replacement session.
        from conftest import run

        run(
            [
                sandbox.runtime.binary,
                "rm",
                "-f",
                sandbox.container_for(first),
            ]
        )
        (project / "ready").unlink()
        second = launch()
        wait_for(lambda: (project / "ready").exists(), processes=(second,))
        assert pids[0].read_text() != old_pid
        (project / "release").touch()
        sandbox.finish(second)
        wait_for(released, timeout=local_forwarder.IDLE_POLL + 5)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
