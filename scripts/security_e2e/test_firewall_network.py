"""Controlled host-port probes; captures known firewall-mode limitations."""

import http.server
import json
import socket
import threading

import pytest
from conftest import run

from project_sandbox import local_service_network


class Listener(http.server.BaseHTTPRequestHandler):
    # Keep-alive lets the startup probe prove a connection works before the
    # firewall initializes and then reuse it afterwards.
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        body = b"SECURITY_E2E_ORIGIN"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def listener():
    server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), Listener)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def fixture_ip(runtime: str) -> str:
    """Host IPv4 address a plain ``RUNTIME run`` container uses to reach a listener.

    Apple containers reach host loopback through the localhost address of the
    configured ``host.docker.internal`` DNS domain (the same alias pi-ollama
    uses), which avoids host-side filters on LAN traffic. Other runtimes reach
    the host through its outbound IPv4 source address.
    """
    if runtime == "apple-container":
        return local_service_network.APPLE_LOCALHOST_ADDRESS
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(("192.0.2.1", 9))  # routing lookup only; sends no packet
        return sock.getsockname()[0]


def default_fixture_host(runtime: str) -> str:
    """Host name or address a plain ``RUNTIME run`` container uses for a listener.

    Rootless Podman's pasta network copies the host's addresses into the
    container namespace, so the host's own LAN address points back at the
    container and refuses the connection; Podman's host alias reaches the host.
    """
    if runtime == "podman":
        return "host.containers.internal"
    return fixture_ip(runtime)


def unreachable_message(runtime: str) -> str:
    message = "fixture host unreachable without firewall; supply --fixture-host with a container-reachable address"
    if runtime == "apple-container":
        message += (
            "; Apple containers need the host alias: "
            + local_service_network.APPLE_SETUP_COMMAND
        )
    return message


PROBE = """import json, socket, urllib.request
from pathlib import Path
cfg = json.loads(Path('destination.json').read_text())
out = {}
try:
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(cfg['url'], timeout=5) as r:
        out['reached'] = r.read().decode() == 'SECURITY_E2E_ORIGIN'
except OSError as error:
    out['reached'] = False
    out['error'] = str(error)
for kind, family, proto in [('icmp', socket.AF_INET, socket.IPPROTO_ICMP), ('icmp6', socket.AF_INET6, socket.IPPROTO_ICMPV6)]:
    try:
        s = socket.socket(family, socket.SOCK_RAW, proto)
        s.close()
        out[kind] = 'raw socket permitted'
    except OSError as error:
        out[kind] = str(error)
Path('network-result.json').write_text(json.dumps(out))
"""


def test_private_domain_pin_opens_unexpected_tcp_port(sandbox, listener, request):
    # The pin below is "<ip>.nip.io", so this needs an address, not a host alias.
    runtime = request.config.getoption("--security-runtime")
    host = request.config.getoption("--fixture-host") or fixture_ip(runtime)
    project = sandbox.project()
    (project / "probe.py").write_text(PROBE)
    (project / "destination.json").write_text(
        json.dumps({"url": f"http://{host}:{listener.server_port}/"})
    )
    sandbox.session(project, "python3 -I probe.py", build=True)
    assert json.loads((project / "network-result.json").read_text())["reached"], (
        unreachable_message(runtime)
    )
    for pinned in (False, True):
        argv = sandbox.argv(
            project,
            "python3 -I probe.py",
            build=True,
            extra=["--extra-domain", f"{host}.nip.io"] if pinned else [],
        )
        argv.remove("--no-firewall")
        sandbox.finish(sandbox.start(argv), timeout=1800)
        result = json.loads((project / "network-result.json").read_text())
        (sandbox.directory / f"firewall-pinned-{pinned}.json").write_text(
            json.dumps(result, indent=2)
        )
        if not pinned:
            assert not result["reached"], "unlisted host port reachable without any pin"
        elif result["reached"]:
            pytest.xfail(
                "TODO: normal DNS pins admit private addresses and unrestricted TCP ports; ICMP capabilities recorded"
            )


def test_devcontainer_gateway_unexpected_port_and_dns(sandbox, listener):
    from project_sandbox import container_cli

    project = sandbox.project()
    sandbox.session(project, "ip -4 route show default > gateway.txt", build=True)
    gateway = (project / "gateway.txt").read_text().split()[2]
    (project / "probe.py").write_text(PROBE)
    (project / "destination.json").write_text(
        json.dumps({"url": f"http://{gateway}:{listener.server_port}/"})
    )
    sandbox.session(project, "python3 -I probe.py")
    if not json.loads((project / "network-result.json").read_text())["reached"]:
        pytest.skip(
            "default gateway is not this host; run gateway listener probe on Linux Docker Engine"
        )
    # Bind only a synthetic DNS responder, and never take over an existing port.
    dns = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        dns.bind((gateway, 53))
    except OSError as error:
        dns.close()
        pytest.skip(f"gateway UDP/53 must be free and bindable for DNS probe: {error}")
    dns.settimeout(0.2)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                data, peer = dns.recvfrom(1024)
                dns.sendto(data, peer)
            except TimeoutError:
                continue

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        assert (
            container_cli.build_image(
                runtime=sandbox.runtime,
                context_dir=project / ".project-sandbox",
                dockerfile_path=project / ".project-sandbox/Dockerfile.devcontainer",
                image_tag=sandbox.image,
            )
            == 0
        )
        (project / "dns.py").write_text(f"""import socket
from pathlib import Path
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
    s.settimeout(5)
    s.sendto(b'SECURITY_E2E_DNS', ({gateway!r}, 53))
    try:
        body, peer = s.recvfrom(1024)
        Path('dns-result').write_text('reachable' if body == b'SECURITY_E2E_DNS' else 'wrong reply')
    except TimeoutError:
        Path('dns-result').write_text('blocked')
""")
        argv = sandbox.argv(project, "python3 -I probe.py && python3 -I dns.py")
        argv.remove("--no-firewall")
        sandbox.finish(sandbox.start(argv))
        result = json.loads((project / "network-result.json").read_text())
        if result["reached"] or (project / "dns-result").read_text() == "reachable":
            pytest.xfail(
                "TODO: devcontainer default-gateway exemption admits unexpected host ports and UDP/53"
            )
    finally:
        stop.set()
        thread.join(timeout=2)
        dns.close()


def test_startup_connection_and_agent_icmp_capabilities(sandbox, listener, request):
    """Observe a connection established before initialization in a fresh container.

    The probe controls startup as fixture root; this is not evidence that an
    agent can execute before the real entrypoint has initialized the firewall.
    """
    from project_sandbox import container_cli

    runtime = request.config.getoption("--security-runtime")
    host = request.config.getoption("--fixture-host") or default_fixture_host(runtime)
    project = sandbox.project()
    (project / "startup.py").write_text(f"""import json, os, pwd, socket, subprocess
from pathlib import Path
REQUEST = b'GET / HTTP/1.1\\r\\nHost: fixture\\r\\n\\r\\n'
def exchange(s):
    s.sendall(REQUEST)
    data = b''
    while b'SECURITY_E2E_ORIGIN' not in data:
        part = s.recv(4096)
        if not part:
            raise ConnectionError('fixture closed the connection')
        data += part
s = socket.create_connection(({host!r}, {listener.server_port}), timeout=10)
# Prove the connection carries data before initialization; otherwise a reset
# afterwards would say nothing about the firewall.
try:
    exchange(s)
except OSError as error:
    raise SystemExit(f'fixture unreachable before firewall initialization: {{error}}')
# Capture real kernel policies before/after running the generated initializer.
before = subprocess.check_output(['iptables-save'], text=True)
subprocess.run(['/usr/local/bin/project-sandbox-init-firewall'], check=True)
after = subprocess.check_output(['iptables-save'], text=True)
os.setgid(pwd.getpwnam('agent').pw_gid)
os.setuid(pwd.getpwnam('agent').pw_uid)
# A reset can surface on send (EPIPE) as well as on receive.
try:
    exchange(s)
    survived = True
except OSError:
    survived = False
caps = {{}}
for family, proto in [(socket.AF_INET, socket.IPPROTO_ICMP), (socket.AF_INET6, socket.IPPROTO_ICMPV6)]:
    try:
        raw = socket.socket(family, socket.SOCK_RAW, proto)
        caps[str(family)] = 'permitted'
        raw.close()
    except OSError as error:
        caps[str(family)] = str(error)
Path('/workspace/startup.json').write_text(json.dumps(dict(before=before, after=after, survived=survived, icmp=caps)))
""")
    sandbox.session(project, build=True)
    argv = [
        sandbox.runtime.binary,
        "run",
        "--rm",
        "--user",
        "root",
        "--cap-add",
        "NET_ADMIN",
        "--cap-add",
        "NET_RAW",
        "--entrypoint",
        "python3",
        "--mount",
        f"type=bind,source={project},target=/workspace",
        sandbox.image,
        "-I",
        "/workspace/startup.py",
    ]
    if (
        sandbox.runtime.name == "podman"
        and container_cli.host_build_identity(sandbox.runtime) is not None
    ):
        argv[2:2] = ["--userns=keep-id"]
    proc = run(argv, timeout=180, check=False)
    assert proc.returncode == 0, (
        unreachable_message(runtime)
        if "fixture unreachable before firewall initialization" in proc.stdout
        else proc.stdout[-8000:]
    )
    result = json.loads((project / "startup.json").read_text())
    assert ":OUTPUT DROP" in result["after"]
    if result["survived"]:
        pytest.xfail(
            "TODO: pre-existing connections survive initialization; startup policies and agent ICMP capabilities captured"
        )
