"""Live Squid adversarial checks using a controlled origin; no provider calls."""

import concurrent.futures
import json
import socket
import sys
import time
from pathlib import Path

import pytest
from conftest import ROOT, run
from origin import Origin

from project_sandbox import egress_policy, egress_sidecar

# Sidecar probes check runtime policy; one image per egress mode serves them.
pytestmark = pytest.mark.shared_image

PROBE = r"""
import http.client, json, os, socket, ssl, urllib.parse
from pathlib import Path
cfg = json.loads(Path('/workspace/probe-config.json').read_text())
p = urllib.parse.urlsplit(os.environ['HTTPS_PROXY'])
out = []
for item in cfg:
    target = item.get('connect', 'allowed.example.test')
    sni = item.get('sni', target)
    host = item.get('host', target)
    path = item.get('path', '/')
    plain = item.get('plain', False)
    port = item.get('port', 443)
    try:
        sock = socket.create_connection((p.hostname, p.port), timeout=10)
        if not plain:
            sock.sendall(f'CONNECT {target}:{port} HTTP/1.1\r\nHost: {target}:{port}\r\n\r\n'.encode())
            header = b''
            while b'\r\n\r\n' not in header and len(header) < 65536:
                part = sock.recv(1)
                if not part:
                    break
                header += part
            if b' 200 ' not in header.split(b'\r\n')[0]:
                out.append({'denied': True, 'connect_reply': header.decode(errors='replace')})
                sock.close()
                continue
            context = ssl._create_unverified_context()
            sock = context.wrap_socket(sock, server_hostname=sni)
        url = f'http://{target}{path}' if plain else path
        method = item.get('method', 'GET')
        sock.sendall(f'{method} {url} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n'.encode())
        response = http.client.HTTPResponse(sock)
        response.begin()
        body = response.read().decode(errors='replace')
        out.append({'status': response.status, 'body': body, 'location': response.getheader('Location')})
        sock.close()
    except (OSError, ValueError, http.client.HTTPException) as error:
        out.append({'error': str(error)})
Path('/workspace/probes.json').write_text(json.dumps(out))
"""


# Printed by the sandbox firewall preflight (init-firewall.sh.j2) when the
# egress proxy does not accept a connection from inside the container.
PROXY_UNREACHABLE = "Internet proxy is unreachable from inside the sandbox"
PROXY_REACHABLE_TIMEOUT = 60.0
PROXY_RETRY_INTERVAL = 3.0


class Sidecar:
    def __init__(self, sandbox, origin):
        self.sandbox, self.origin = sandbox, origin
        self.project = sandbox.project()
        (self.project / "probe.py").write_text(PROBE)
        self.built = False

    def cli(self, *args, runtime=None, env=None, check=True):
        return run(
            [
                sys.executable,
                "-m",
                "project_sandbox.egress_cli",
                *args,
                "--runtime",
                runtime or self.sandbox.runtime.name,
            ],
            env=env or self.sandbox.env,
            check=check,
            timeout=1800,
        )

    def policy_text(
        self, hosts=("allowed.example.test",), *, copilot=False, parent=True
    ):
        text = "[egress]\nallow = " + json.dumps(list(hosts)) + "\n"
        if parent:
            text += (
                "[egress.upstream]\n"
                + f'url = "http://127.0.0.1:{self.origin.port}"\n'
                + "ca_cert = "
                + json.dumps([str(self.origin.ca)])
                + "\n"
            )
        if copilot:
            text += "[egress.copilot]\nenabled = true\n"
        return text

    def policy(self, hosts=("allowed.example.test",), *, copilot=False, parent=True):
        text = self.policy_text(hosts, copilot=copilot, parent=parent)
        file = Path(self.sandbox.env["XDG_CONFIG_HOME"]) / "project-sandbox/egress.toml"
        file.parent.mkdir(exist_ok=True)
        file.write_text(text)
        self.cli("restart")

    def probe(self, items):
        (self.project / "probe-config.json").write_text(json.dumps(items))
        (self.project / "probes.json").unlink(missing_ok=True)
        # `restart` waits until the sidecar answers on the host's loopback
        # endpoint, but the sandbox reaches it through host.docker.internal,
        # a separate forwarding path (Podman's gvproxy on macOS) that can lag
        # behind. The firewall preflight connects once and aborts the session,
        # so relaunch until that path answers; any other failure is real.
        deadline = time.monotonic() + PROXY_REACHABLE_TIMEOUT
        while True:
            argv = self.sandbox.argv(
                self.project,
                "python3 -I probe.py",
                build=not self.built,
                extra=["--egress", "sidecar"],
            )
            argv.remove("--no-firewall")
            before = set(self.session_logs())
            proc = self.sandbox.start(argv)
            code = self.sandbox.finish(proc, ok=False, timeout=1800)
            if code == 0:
                break
            unreachable = any(
                PROXY_UNREACHABLE in path.read_text(errors="replace")
                for path in set(self.session_logs()) - before
            )
            if not unreachable or time.monotonic() >= deadline:
                assert code == 0, proc.audit_log.read_text()[-8000:]
            # The image was built before the preflight ran; do not rebuild.
            self.built = True
            time.sleep(PROXY_RETRY_INTERVAL)
        self.built = True
        return json.loads((self.project / "probes.json").read_text())

    def session_logs(self):
        return (self.project / ".project-sandbox/sessions").glob("*.log")

    def logs(self):
        text = run(egress_sidecar.logs_argv(self.sandbox.runtime), check=False).stdout
        (self.sandbox.directory / "squid.log").write_text(text)
        return text


@pytest.fixture
def sidecar(sandbox):
    # Global container name: never replace somebody else's running sidecar.
    assert (
        egress_sidecar.inspect(sandbox.runtime, egress_sidecar.default_runner) is None
    ), "stop your existing project-sandbox-egress sidecar before running this suite"
    with Origin(sandbox.directory) as origin:
        fixture = Sidecar(sandbox, origin)
        try:
            yield fixture
        finally:
            try:
                fixture.logs()
            finally:
                fixture.cli("down", check=False)


def reached(row):
    if row.get("status") != 200:
        return False
    try:
        return "host" in json.loads(row["body"])
    except (KeyError, ValueError):
        return False


def test_spliced_domain_fronting_and_http_default(sidecar):
    sidecar.policy()
    control, denied, fronted, plain = sidecar.probe(
        [
            {},
            {"connect": "blocked.example.test"},
            {"host": "blocked.example.test"},
            {"plain": True},
        ]
    )
    assert reached(control), control
    assert not reached(denied), denied
    # These are documented limits, not a claimed bypass of an encrypted-Host ACL.
    assert (
        reached(fronted)
        and json.loads(fronted["body"])["host"] == "blocked.example.test"
    ), fronted
    assert reached(plain), plain
    pytest.xfail(
        "TODO: spliced TLS cannot enforce encrypted Host; allowed hosts permit plain HTTP"
    )


@pytest.mark.parametrize(
    "path",
    [
        "/repos/secret",
        "/copilot_internal/../repos/secret",
        "/copilot_internal/%2e%2e/repos/secret",
        "/copilot_internal%2f..%2frepos/secret",
        "/%63opilot_internal/user",
        "/copilot_internal/user?token=SECURITY_E2E_QUERY_CANARY",
    ],
)
def test_copilot_paths(sidecar, path):
    sidecar.policy(copilot=True)
    control, probe = sidecar.probe(
        [
            {"connect": "api.github.com", "path": "/copilot_internal/user"},
            {"connect": "api.github.com", "path": path},
        ]
    )
    assert reached(control), control
    if reached(probe) and (".." in path or "%2e" in path or "%2f" in path):
        pytest.fail(f"origin received an ambiguous repository path: {probe}")
    if path == "/repos/secret":
        assert probe.get("status") == 403 and "path-not-allowed" in probe.get(
            "body", ""
        ), probe
    if "QUERY_CANARY" in path:
        assert reached(probe), probe
        if "SECURITY_E2E_QUERY_CANARY" in sidecar.logs():
            pytest.xfail(
                "TODO: visible query strings are retained in Squid access logs"
            )


def test_copilot_host_sni_methods_and_redirect(sidecar):
    sidecar.policy(copilot=True)
    rows = sidecar.probe(
        [
            {"connect": "api.github.com", "path": "/copilot_internal/user"},
            {
                "connect": "api.github.com",
                "sni": "blocked.example.test",
                "path": "/copilot_internal/user",
            },
            {
                "connect": "api.github.com",
                "host": "blocked.example.test",
                "path": "/copilot_internal/user",
            },
            {"connect": "api.github.com", "path": "/copilot_internal/redirect"},
            {"connect": "blocked.example.test", "path": "/stolen"},
            {
                "connect": "api.github.com",
                "method": "DELETE",
                "path": "/copilot_internal/user",
            },
        ]
    )
    assert reached(rows[0]), rows
    assert not reached(rows[1]) and not reached(rows[2]), rows
    assert (
        rows[3].get("status") == 302
        and rows[3]["location"] == "https://blocked.example.test/stolen"
    ), rows
    assert not reached(rows[4]), rows
    if reached(rows[5]):
        pytest.xfail(
            "TODO: Copilot entitlement prefix currently permits arbitrary methods"
        )


def test_wildcard_apex_public_suffix_and_implicit_providers(sidecar):
    sidecar.policy(hosts=("*.example.test",))
    apex, subdomain, provider = sidecar.probe(
        [{"connect": "example.test"}, {}, {"connect": "api.openai.com"}]
    )
    assert reached(apex) and reached(subdomain), (apex, subdomain)
    assert reached(provider), provider
    broad = sidecar.cli("allow", "add", "*.co.uk", check=False)
    if broad.returncode == 0:
        pytest.xfail(
            "TODO: wildcard apex semantics, public-suffix patterns, and nonremovable provider union"
        )


@pytest.mark.parametrize(
    "host", sorted(set(egress_policy.GITHUB_REPOSITORY_HOSTS) | {"lfs.github.com"})
)
def test_repository_host_exclusions(sidecar, host):
    sidecar.policy(copilot=True)
    rows = sidecar.probe(
        [
            {},
            {"connect": host},
            {"connect": host, "plain": True},
            {"connect": host, "port": 22},
            {"connect": host, "port": 9418},
        ]
    )
    assert reached(rows[0]), rows[0]
    assert all(not reached(row) for row in rows[1:]), (
        f"repository transport escaped profile: {host}: {rows}"
    )
    logs = sidecar.logs()
    assert "host-not-allowed" in logs and "port-not-allowed" in logs


def free_loopback_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_concurrent_cross_runtime_edits_and_delayed_launch(sidecar, request):
    peer = request.config.getoption("--peer-runtime")
    if not peer or peer == sidecar.sandbox.runtime.name:
        pytest.skip(
            "cross-runtime editing requires --peer-runtime with a second live runtime"
        )
    from project_sandbox import container_cli

    peer_runtime = container_cli.select_runtime(peer)
    assert (
        egress_sidecar.inspect(peer_runtime, egress_sidecar.default_runner) is None
    ), "peer runtime has an existing sidecar"
    sidecar.policy()
    # [egress.sidecar] port is one host port per policy file, and every runtime
    # publishes its sidecar on the host, so two live sidecars need two policies.
    # The peer gets its own XDG config with a free port, sharing the state home.
    sandbox_config = Path(sidecar.sandbox.env["XDG_CONFIG_HOME"])
    peer_config = sidecar.sandbox.directory / "peer-xdg-config"
    (peer_config / "project-sandbox").mkdir(parents=True)
    if (sandbox_config / "containers").is_symlink():
        (peer_config / "containers").symlink_to(
            (sandbox_config / "containers").readlink()
        )
    peer_policy = peer_config / "project-sandbox/egress.toml"
    peer_policy.write_text(
        sidecar.policy_text() + f"[egress.sidecar]\nport = {free_loopback_port()}\n"
    )
    peer_env = {**sidecar.sandbox.env, "XDG_CONFIG_HOME": str(peer_config)}
    sidecar.cli("up", runtime=peer, env=peer_env)
    request.addfinalizer(
        lambda: sidecar.cli("down", runtime=peer, env=peer_env, check=False)
    )
    # Each command performs the real read/edit/reload transaction against its
    # runtime's live sidecar, concurrently and under separate runtime locks.
    # Both updates must land, each only in its own runtime's policy.
    with concurrent.futures.ThreadPoolExecutor() as pool:
        futures = [
            pool.submit(sidecar.cli, "allow", "add", host, runtime=runtime, env=env)
            for host, runtime, env in [
                ("one.example.test", sidecar.sandbox.runtime.name, None),
                ("two.example.test", peer, peer_env),
            ]
        ]
        for future in futures:
            future.result()
    policy = sandbox_config / "project-sandbox/egress.toml"
    assert "one.example.test" in policy.read_text()
    assert "two.example.test" not in policy.read_text()
    assert "two.example.test" in peer_policy.read_text()
    assert "one.example.test" not in peer_policy.read_text()
    control = sidecar.probe([{}])[0]
    assert reached(control), control
    sidecar.cli("allow", "remove", "allowed.example.test")
    # A fresh launch after removal must not resurrect cached policy state.
    assert not reached(sidecar.probe([{}])[0])
    assert "allowed.example.test" not in policy.read_text()
    # Revoking on one runtime leaves the other runtime's policy untouched.
    assert "allowed.example.test" in peer_policy.read_text()


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1.nip.io",
        "169.254.169.254.nip.io",
        "[::ffff:127.0.0.1]",
        "[64:ff9b::a9fe:a9fe]",
    ],
)
def test_direct_private_and_translated_literals(sidecar, host):
    sidecar.policy(
        hosts=("allowed.example.test", "127.0.0.1.nip.io", "169.254.169.254.nip.io"),
        parent=False,
    )
    row = sidecar.probe([{"connect": host}])[0]
    assert not reached(row), row
    logs = sidecar.logs()
    assert "private-address" in logs or "ip-literal" in logs, (
        "DNS/network failure is not proof of an ACL denial"
    )


def test_revocation_existing_tunnel_vs_new_connection(sidecar):
    from conftest import wait_for

    sidecar.policy()
    assert reached(sidecar.probe([{}])[0])
    source = r"""
import http.client, json, os, socket, ssl, time, urllib.parse
from pathlib import Path
p = urllib.parse.urlsplit(os.environ['HTTPS_PROXY'])
s = socket.create_connection((p.hostname, p.port), timeout=10)
s.sendall(b'CONNECT allowed.example.test:443 HTTP/1.1\r\nHost: allowed.example.test:443\r\n\r\n')
header = b''
while not header.endswith(b'\r\n\r\n'):
    part = s.recv(1)
    assert part, 'CONNECT reset'
    header += part
assert b' 200 ' in header
s = ssl._create_unverified_context().wrap_socket(s, server_hostname='allowed.example.test')
def request():
    s.sendall(b'GET / HTTP/1.1\r\nHost: allowed.example.test\r\nConnection: keep-alive\r\n\r\n')
    r = http.client.HTTPResponse(s)
    r.begin()
    body = r.read()
    return r.status == 200 and b'"host"' in body
assert request()
Path('tunnel-ready').touch()
end = time.monotonic() + 120
while not Path('revoked').exists():
    assert time.monotonic() < end
    time.sleep(.1)
try:
    survived = request()
except (OSError, http.client.HTTPException):
    survived = False
Path('tunnel-result.json').write_text(json.dumps({'survived': survived}))
"""
    (sidecar.project / "tunnel.py").write_text(source)
    argv = sidecar.sandbox.argv(
        sidecar.project, "python3 -I tunnel.py", extra=["--egress", "sidecar"]
    )
    argv.remove("--no-firewall")
    proc = sidecar.sandbox.start(argv)
    wait_for(lambda: (sidecar.project / "tunnel-ready").exists(), processes=(proc,))
    sidecar.cli("allow", "remove", "allowed.example.test")
    (sidecar.project / "revoked").touch()
    sidecar.sandbox.finish(proc)
    assert not reached(sidecar.probe([{}])[0]), "new connection survived revocation"
    if json.loads((sidecar.project / "tunnel-result.json").read_text())["survived"]:
        pytest.xfail(
            "TODO: policy reload denies new connections but retains existing spliced tunnels"
        )


def test_launch_waiting_for_image_lock_cannot_restore_revoked_policy(sidecar):
    from conftest import wait_for

    from project_sandbox import build_cache

    sidecar.policy()
    assert reached(sidecar.probe([{}])[0])
    (sidecar.project / "probe-config.json").write_text(json.dumps([{}]))
    (sidecar.project / "probes.json").unlink()
    generated = sidecar.project / ".project-sandbox/Dockerfile"
    before = generated.stat().st_mtime_ns
    argv = sidecar.sandbox.argv(
        sidecar.project, "python3 -I probe.py", extra=["--egress", "sidecar"]
    )
    argv.remove("--no-firewall")
    with build_cache.image_lock(sidecar.sandbox.runtime.name):
        delayed = sidecar.sandbox.start(argv)
        wait_for(lambda: generated.stat().st_mtime_ns != before, processes=(delayed,))
        sidecar.cli("allow", "remove", "allowed.example.test")
        assert delayed.poll() is None
    sidecar.sandbox.finish(delayed)
    assert not reached(json.loads((sidecar.project / "probes.json").read_text())[0])


def test_mixed_dns_rebinding_mapped_ipv6_and_nat64(sidecar):
    """Exercise Squid's actual DNS cache/ACL, with a local authoritative resolver."""
    import subprocess

    from conftest import wait_for

    hosts = [
        f"{name}.example.test"
        for name in ("mixed", "rebind", "private", "mapped", "nat64")
    ]
    sidecar.policy(hosts=hosts, parent=False)
    runtime = sidecar.sandbox.runtime

    def execute(*command, check=True):
        return run(
            [
                runtime.binary,
                "exec",
                "--user",
                "root",
                egress_sidecar.CONTAINER,
                *command,
            ],
            check=check,
        )

    # This is an instrumented fixture container: production config/ACLs remain
    # intact; only its DNS resolver and cache lifetime change for the experiment.
    execute("apk", "add", "--no-cache", "python3")
    execute(
        "python3",
        "-c",
        "from pathlib import Path; Path('/tmp/security-dns-phase').write_text('public')",
    )
    source = (ROOT / "scripts/security_e2e/dns_fixture.py").read_text()
    with (sidecar.sandbox.directory / "dns.log").open("w") as log:
        resolver = subprocess.Popen(
            [
                runtime.binary,
                "exec",
                "--user",
                "root",
                egress_sidecar.CONTAINER,
                "python3",
                "-c",
                source,
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        wait_for(
            lambda: (
                execute("test", "-f", "/tmp/security-dns-ready", check=False).returncode
                == 0
            ),
            processes=(resolver,),
        )
        execute(
            "python3",
            "-c",
            "from pathlib import Path; p=Path('/etc/squid/squid.conf'); p.write_text(p.read_text() + '\\ndns_nameservers 127.0.0.1\\npositive_dns_ttl 1 seconds\\nnegative_dns_ttl 1 seconds\\n')",
        )
        execute("squid", "-k", "reconfigure")

        def connect(host):
            return execute(
                "python3",
                "-c",
                r"""
import socket, sys
with socket.create_connection(('127.0.0.1', 3128), timeout=5) as s:
    s.sendall(('CONNECT ' + sys.argv[1] + ':443 HTTP/1.1\r\nHost: ' + sys.argv[1] + ':443\r\n\r\n').encode())
    try:
        print(repr(s.recv(1024)))
    except ConnectionResetError:
        print('RESET')
""",
                host,
            ).stdout

        initial = connect("rebind.example.test")
        assert "200 Connection established" in initial, initial
        results = {
            host: connect(host) for host in hosts if not host.startswith("rebind")
        }
        execute(
            "python3",
            "-c",
            "from pathlib import Path; Path('/tmp/security-dns-phase').write_text('private')",
        )
        # Bound the cache expiry wait and capture each observation.
        for _ in range(30):
            changed = connect("rebind.example.test")
            if "200 Connection established" not in changed:
                break
            time.sleep(0.2)
        results["rebind.example.test"] = changed
        (sidecar.sandbox.directory / "dns-results.json").write_text(
            json.dumps(results, indent=2)
        )
        # A denial is a reset, or (documented limit below) a bare 200 and
        # close. Anything else means bytes came back through a tunnel.
        established = "b'HTTP/1.1 200 Connection established\\r\\n\\r\\n'"
        for host, reply in results.items():
            assert reply.strip() in ("RESET", "b''", established), (host, reply)
        logs = sidecar.logs()
        for host in hosts:
            denials = [
                line
                for line in logs.splitlines()
                if f" {host}:443 " in line and "cause=private-address" in line
            ]
            assert denials, f"{host}: must prove ACL denial, not a failed connection"
            # The security property: Squid never dialled the private address.
            assert all(" peer=- " in line for line in denials), denials
        if any(reply.strip() != "RESET" for reply in results.values()):
            # Squid 7 error-bumps every denied CONNECT on an ssl-bump port; a
            # TCP_RESET page only pre-empts the queued 200 when http_access
            # finishes synchronously (clientProcessRequestFinished). A `dst`
            # ACL that waits for an uncached DNS answer finishes later, so
            # the client sees 200, then the reset. docs/egress-sidecar.md.
            pytest.xfail(
                "TODO: uncached private-address denials send 200 before the reset"
            )
    finally:
        # Stop the container first: killing runtime exec alone can orphan its child.
        sidecar.cli("down", check=False)
        resolver.wait(timeout=15)


def test_deployment_nat64_path(sidecar, request):
    host = request.config.getoption("--nat64-origin")
    if not host:
        pytest.skip(
            "requires --nat64-origin and a controlled HTTPS origin behind the deployment NAT64 translator"
        )
    sidecar.policy(hosts=(host,), parent=False)
    # Establish a positive control through the actual deployment translator,
    # outside the sandbox's firewall, before attributing a denial to Squid.
    (sidecar.project / "nat64-control.py").write_text(
        "import json, ssl, urllib.request\nfrom pathlib import Path\n"
        + "opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))\n"
        + f"with opener.open({'https://' + host + '/'!r}, timeout=15) as response:\n"
        + "    body=json.load(response)\n    assert 'host' in body\n"
        + "Path('nat64-control.ok').touch()\n"
    )
    sidecar.sandbox.session(sidecar.project, "python3 -I nat64-control.py", build=True)
    assert (sidecar.project / "nat64-control.ok").exists()
    # Egress mode changes the generated entrypoint/firewall: the probe uses
    # the egress-sidecar image, not the default one built above.
    row = sidecar.probe([{"connect": host}])[0]
    if reached(row):
        pytest.xfail(
            "TODO: this deployment NAT64 prefix is not classified as a private destination"
        )
    assert "cause=private-address" in sidecar.logs(), (
        "network failure is not proof of NAT64 policy enforcement"
    )
