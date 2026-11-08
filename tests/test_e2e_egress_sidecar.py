"""Runtime-free tests for scripts/e2e-egress-sidecar.py."""

import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import container_cli


def _load():
    path = Path(__file__).parents[1] / "scripts" / "e2e-egress-sidecar.py"
    spec = importlib.util.spec_from_file_location("e2e_egress_sidecar", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


GOOD_LOG_LINES = (
    "SBX 1.0 TCP_DENIED/200 CONNECT example.com:443 sni=- bump=bump err=TCP_RESET cause=host-not-allowed peer=-",
    *(
        f"SBX 1.1 TCP_DENIED/200 CONNECT {target}:443 sni=- bump=bump err=TCP_RESET cause=ip-literal peer=-"
        for target in (
            "1.1.1.1",
            "127.0.0.1",
            "[::1]",
            "10.0.0.1",
            "169.254.1.1",
            "169.254.169.254",
            "[fd00::1]",
        )
    ),
    "SBX 1.2 TCP_DENIED/403 GET http://192.168.0.1/ sni=- bump=- err=ERR_SBX_IP_LITERAL cause=ip-literal peer=-",
    "SBX 1.3 TCP_DENIED/200 CONNECT pypi.org:8443 sni=- bump=bump err=TCP_RESET cause=port-not-allowed peer=-",
    "SBX 1.4 TCP_DENIED/200 CONNECT 169.254.169.254.nip.io:443 sni=169.254.169.254.nip.io bump=bump err=TCP_RESET cause=private-address peer=-",
    "SBX 1.4 TCP_DENIED/200 CONNECT 169.254.169.254.sslip.io:443 sni=169.254.169.254.sslip.io bump=bump err=TCP_RESET cause=private-address peer=-",
    "SBX 1.5 NONE_NONE/200 CONNECT pypi.org:443 sni=files.pythonhosted.org bump=terminate err=- cause=- peer=-",
    "SBX 1.5 NONE_NONE/200 CONNECT pypi.org:443 sni=example.com bump=terminate err=- cause=- peer=-",
    "SBX 1.5 NONE_NONE/200 CONNECT pypi.org:443 sni=- bump=terminate err=- cause=- peer=-",
    "SBX 1.6 NONE_NONE/200 CONNECT pypi.org:443 sni=- bump=peek err=ERR_PROTOCOL_UNKNOWN cause=- peer=-",
    "SBX 1.7 TCP_DENIED/200 CONNECT github.com:443 sni=- bump=bump err=TCP_RESET cause=host-not-allowed peer=-",
    "SBX 1.8 TCP_TUNNEL/200 CONNECT pypi.org:443 sni=pypi.org bump=splice err=- cause=- peer=151.101.0.223",
)
GOOD_LOG = "\n".join(GOOD_LOG_LINES)


INTERCEPT_LOG_LINES = (
    "SBX 1.0 TCP_DENIED/403 GET https://api.github.com/repos/octocat/Hello-World sni=api.github.com bump=bump err=ERR_SBX_PATH_NOT_ALLOWED cause=path-not-allowed peer=-",
    "SBX 1.1 TCP_DENIED/403 GET http://api.github.com/ sni=- bump=- err=ERR_SBX_PLAIN_HTTP_INTERCEPTED cause=plain-http-intercepted peer=-",
    "SBX 1.2 NONE_NONE/200 CONNECT api.github.com:443 sni=pypi.org bump=terminate err=- cause=- peer=-",
    "SBX 1.3 NONE_NONE/200 CONNECT api.github.com:443 sni=- bump=peek err=ERR_PROTOCOL_UNKNOWN cause=- peer=-",
    "SBX 1.4 TCP_MISS/401 GET https://api.github.com/copilot_internal/user sni=api.github.com bump=bump err=- cause=- peer=140.82.121.6",
)


class ArgumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = _load()

    def test_defaults(self) -> None:
        args = self.script.parse_args([])
        self.assertEqual(args.runtime, "auto")
        self.assertEqual(
            args.checks, ("policy", "intercept", "shared", "parent", "local")
        )
        self.assertEqual(args.port, 3129)
        self.assertFalse(args.no_build)
        self.assertFalse(args.keep)

    def test_checks_subset_and_rejection(self) -> None:
        self.assertEqual(
            self.script.parse_args(["--checks", "policy"]).checks, ("policy",)
        )
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.script.parse_args(["--checks", "policy,bogus"])


class GradingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = _load()

    def test_probe_grading_requires_every_probe(self) -> None:
        results = self.script.grade_probes(
            {"allowed-splice": {"ok": True, "detail": "200"}}
        )
        by_name = {r.name: r for r in results}
        self.assertTrue(by_name["allowed-splice"].ok)
        self.assertFalse(by_name["sni-mismatch"].ok)
        self.assertEqual(by_name["sni-mismatch"].detail, "probe did not run")
        self.assertEqual([r.name for r in results], list(self.script.PROBE_NAMES))
        # Decision 15's probe table: every IP-literal class and both nip/sslip names.
        for name in (
            "ip-literal-loopback",
            "ip-literal-rfc1918",
            "ip-literal-link-local",
            "ip-literal-private6",
            "ip-literal-port80",
            "metadata-name-nip.io",
            "metadata-name-sslip.io",
            "sni-unlisted",
            "raw-tunnel-ssh",
            "allowed-splice-http",
        ):
            self.assertIn(name, by_name)

    def test_log_grading_passes_on_expected_causes(self) -> None:
        results = self.script.grade_log(GOOD_LOG)
        self.assertTrue(all(r.ok for r in results), [r for r in results if not r.ok])

    def test_log_grading_flags_missing_cause_and_tunnel_to_denied_host(self) -> None:
        bad = GOOD_LOG.replace("cause=private-address", "cause=-").replace(
            "TCP_DENIED/200 CONNECT example.com:443 sni=- bump=bump err=TCP_RESET",
            "TCP_TUNNEL/200 CONNECT example.com:443 sni=example.com bump=splice err=-",
        )
        failed = {r.name for r in self.script.grade_log(bad) if not r.ok}
        self.assertIn("log:host-not-allowed:example.com", failed)
        self.assertIn("log:no-tunnel-to-denied-hosts", failed)

    def test_log_grading_flags_tunnels_to_ip_literals_and_metadata_names(self) -> None:
        for target in ("[fd00::1]:443", "169.254.169.254.sslip.io:443"):
            bad = GOOD_LOG + (
                f"\nSBX 2.0 TCP_TUNNEL/200 CONNECT {target} sni=- bump=splice err=- cause=- peer=-"
            )
            failed = {r.name for r in self.script.grade_log(bad) if not r.ok}
            self.assertEqual(failed, {"log:no-tunnel-to-denied-hosts"}, target)

    def test_log_grading_requires_each_termination_and_the_splice(self) -> None:
        bad = "\n".join(
            line
            for line in GOOD_LOG_LINES
            if "sni=example.com bump=terminate" not in line
            and "bump=splice" not in line
        )
        failed = {r.name for r in self.script.grade_log(bad) if not r.ok}
        self.assertEqual(
            failed,
            {"log:terminated:pypi.org:sni=example.com", "log:splice:pypi.org"},
        )

    def test_intercept_log_grading(self) -> None:
        good = "\n".join(INTERCEPT_LOG_LINES)
        results = self.script.grade_intercept_log(good)
        self.assertTrue(all(r.ok for r in results), [r for r in results if not r.ok])
        self.assertEqual(len(results), 5)
        bare = self.script.grade_intercept_log("")
        self.assertFalse(any(r.ok for r in bare))

    def test_parent_grading(self) -> None:
        good = self.script.grade_parent(
            {"allowed": 200, "unlisted": "ConnectionResetError"},
            "FIXTURE 1.0 TCP_TUNNEL/200 CONNECT pypi.org:443 sni=pypi.org bump=bump",
        )
        self.assertTrue(all(r.ok for r in good))
        bad = self.script.grade_parent(
            {"allowed": "URLError", "unlisted": "HTTP/1.1 200 Connection established"},
            "FIXTURE 1.0 TCP_TUNNEL/200 CONNECT example.com:443",
        )
        self.assertFalse(any(r.ok for r in bad))

    def test_parent_unlisted_needs_a_close_or_reset(self) -> None:
        def unlisted_ok(value):
            (result,) = [
                r
                for r in self.script.grade_parent({"unlisted": value}, "")
                if r.name == "parent:unlisted-reset-locally"
            ]
            return result.ok

        self.assertTrue(unlisted_ok(""))
        self.assertTrue(unlisted_ok("ConnectionResetError"))
        # A 403 page, a timeout or a refused connection is not a local reset.
        self.assertFalse(unlisted_ok("HTTP/1.1 403 Forbidden"))
        self.assertFalse(unlisted_ok("TimeoutError"))
        self.assertFalse(unlisted_ok("ConnectionRefusedError"))

    def test_session_id_from_output(self) -> None:
        self.assertEqual(
            self.script.session_id_from(
                "x\nSession: 20261105T181615-f066e939087e\nContainer: y\n"
            ),
            "20261105T181615-f066e939087e",
        )
        self.assertIsNone(self.script.session_id_from("no session"))

    def test_local_grading(self) -> None:
        ok = {"body": "local-service-ok", "no_proxy": "localhost,host.docker.internal"}
        self.assertTrue(all(r.ok for r in self.script.grade_local(ok, "", 4000)))
        failed = {
            r.name
            for r in self.script.grade_local(
                {"body": "URLError", "no_proxy": "localhost"},
                "SBX 1.0 TCP_DENIED/403 GET http://host.docker.internal:4000/ok",
                4000,
            )
            if not r.ok
        }
        self.assertEqual(
            failed, {"local:loopback-service-reached", "local:bypasses-the-sidecar"}
        )

    def test_container_names(self) -> None:
        docker = '{"Names": "project-sandbox-egress"}\n{"Names": "project-sandbox-x"}\n'
        podman = json.dumps([{"Names": ["project-sandbox-egress"]}])
        apple = json.dumps([{"configuration": {"id": "project-sandbox-egress"}}])
        self.assertEqual(
            self.script.container_names(container_cli.DOCKER, docker),
            ["project-sandbox-egress", "project-sandbox-x"],
        )
        self.assertEqual(
            self.script.container_names(container_cli.PODMAN, podman),
            ["project-sandbox-egress"],
        )
        self.assertEqual(
            self.script.container_names(container_cli.APPLE_CONTAINER, apple),
            ["project-sandbox-egress"],
        )
        self.assertEqual(self.script.container_names(container_cli.DOCKER, ""), [])

    def test_policy_text_sets_the_port(self) -> None:
        text = self.script._policy_text("pypi.org", port=3140)
        self.assertIn('allow = ["pypi.org"]', text)
        self.assertIn("[egress.sidecar]\nport = 3140\n", text)
        self.assertNotIn("forward_ports", text)
        self.assertNotIn("[egress.copilot]", text)

    def test_policy_text_turns_on_the_copilot_profile(self) -> None:
        text = self.script._policy_text("pypi.org", copilot=True)
        self.assertIn("[egress.copilot]\nenabled = true\n", text)
        policy = self.script.egress_policy.parse(
            tomllib.loads(text), path=Path("egress.toml")
        )
        self.assertTrue(policy.copilot)
        self.assertEqual(
            self.script.egress_policy.derive_bump_set(copilot=policy.copilot),
            (self.script.INTERCEPTED_HOST,),
        )

    def test_probes_use_the_proxy_environment(self) -> None:
        for source in (
            self.script.probe_source(),
            self.script.intercept_probe_source(),
            self.script.PARENT_PROBE_SOURCE,
        ):
            self.assertIn('os.environ["HTTPS_PROXY"]', source)
            self.assertNotIn("egress-proxy", source)

    def test_probe_source_is_valid_python(self) -> None:
        compile(self.script.probe_source(), "probe.py", "exec")
        compile(self.script.intercept_probe_source(), "intercept.py", "exec")
        compile(
            self.script.PARENT_PROBE_SOURCE.replace("__ALLOWED__", "pypi.org").replace(
                "__UNLISTED__", "example.com"
            ),
            "parent.py",
            "exec",
        )
        compile(self.script.LOCAL_PROBE_SOURCE, "local.py", "exec")

    def test_probe_targets_bracket_ipv6_literals(self) -> None:
        source = self.script.probe_source()
        self.assertIn("'[fd00::1]:443'", source)
        self.assertIn("'[::1]:443'", source)
        self.assertIn("'10.0.0.1:443'", source)
        self.assertNotIn("__CONFIG__", source)


class ProbeHelperTests(unittest.TestCase):
    """The in-sandbox grading helpers, run on the host with a fake transport."""

    def setUp(self) -> None:
        self.script = _load()
        self.ns = {"PROXY": ("host.docker.internal", 3129), "results": {}}
        # The probe helpers are our own source, run as the sandbox would.
        exec(compile(self.script.PROBE_HELPERS, "helpers", "exec"), self.ns)  # noqa: S102

    def _denied(self, reply=None, error=None) -> dict:
        def fake(_target, timeout=8):
            if error is not None:
                raise error
            return reply

        self.ns["connect_reply"] = fake
        self.ns["denied_connect"]("probe", "example.com:443")
        return self.ns["results"]["probe"]

    def test_connect_denial_passes_on_zero_bytes_then_close_or_reset(self) -> None:
        self.assertTrue(self._denied((b"", "closed"))["ok"])
        self.assertTrue(self._denied((b"", "reset"))["ok"])

    def test_connect_denial_fails_on_any_reply(self) -> None:
        for reply in (
            (b"HTTP/1.1 200 Connection established\r\n\r\n", "reply"),
            (b"HTTP/1.1 403 Forbidden\r\n\r\n", "reply"),
            (b"HTTP/1.1 403", "closed"),
        ):
            self.assertFalse(self._denied(reply)["ok"], reply)

    def test_connect_denial_fails_on_timeout_or_unreachable_sidecar(self) -> None:
        # Regression: these used to pass, so an unreachable sidecar passed
        # every CONNECT denial probe.
        self.assertFalse(self._denied((b"", "timeout"))["ok"])
        result = self._denied(error=ConnectionRefusedError())
        self.assertFalse(result["ok"])
        self.assertIn("could not reach the sidecar", result["detail"])

    def test_labelled_403_requires_the_label(self) -> None:
        import email.message
        import urllib.error

        def page(code, body):
            class Opener:
                def open(self, url, timeout=None):
                    raise urllib.error.HTTPError(
                        url, code, "x", email.message.Message(), io.BytesIO(body)
                    )

            self.ns["_opener"] = Opener
            self.ns["labelled_403"]("probe", "http://example.com/", "host-not-allowed")
            return self.ns["results"]["probe"]["ok"]

        self.assertTrue(page(403, b"<h1>project-sandbox-egress: host-not-allowed</h1>"))
        self.assertFalse(page(403, b"<h1>project-sandbox-egress: ip-literal</h1>"))
        self.assertFalse(page(404, b"project-sandbox-egress: host-not-allowed"))


class _ExitedProc:
    """A launch that already died (e.g. its image build failed)."""

    _next_pid = 90000

    def __init__(self, *_args, output: str = "", **_kwargs) -> None:
        _ExitedProc._next_pid += 1
        self.pid = _ExitedProc._next_pid
        self.returncode = 1
        self.output = output

    def poll(self) -> int:
        return self.returncode

    def communicate(self, timeout=None):
        return self.output, None

    def kill(self) -> None:
        pass


class _RunningProc(_ExitedProc):
    def poll(self):
        return None


class HangRegressionTests(unittest.TestCase):
    """Regression: a failed launch made the shared check wait out --timeout."""

    def setUp(self) -> None:
        self.script = _load()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_wait_files_stops_once_a_launch_exited(self) -> None:
        started = time.monotonic()
        ok = self.script._wait_files(
            [self.tmp / "never"], 60, [_RunningProc(), _ExitedProc()], interval=0.01
        )
        self.assertFalse(ok)
        self.assertLess(time.monotonic() - started, 5)

    def test_wait_files_still_succeeds_when_the_file_landed_before_exit(self) -> None:
        (self.tmp / "ready").write_text("")
        self.assertTrue(
            self.script._wait_files([self.tmp / "ready"], 60, [_ExitedProc()])
        )

    def test_wait_files_times_out_while_launches_run(self) -> None:
        self.assertFalse(
            self.script._wait_files(
                [self.tmp / "never"], 0.05, [_RunningProc()], interval=0.01
            )
        )

    def test_egress_timeout_becomes_a_failed_result(self) -> None:
        args = self.script.parse_args(["--timeout", "7"])
        args.runtime = "docker"
        with mock.patch.object(
            self.script.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["x"], 7),
        ) as run:
            result = self.script.egress(args, "up")
        self.assertEqual(run.call_args.kwargs["timeout"], 7)
        self.assertEqual(result.returncode, 124)
        self.assertIn("project-sandbox-egress up timed out after 7s", result.stderr)

    def test_shared_check_returns_promptly_when_launches_fail(self) -> None:
        args = self.script.parse_args(["--timeout", "600"])
        args.runtime = "apple-container"
        error = 'Error: unknown: "open /tmp/1: read-only file system"'
        egress_calls = []

        def fake_egress(_args, *command):
            egress_calls.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        work = self.tmp / "work"
        work.mkdir()
        started = time.monotonic()
        with (
            mock.patch.dict(
                "os.environ", {"XDG_CONFIG_HOME": str(self.tmp / "config")}
            ),
            mock.patch.object(
                self.script.subprocess,
                "Popen",
                side_effect=lambda *a, **k: _ExitedProc(output=error),
            ),
            mock.patch.object(self.script, "egress", side_effect=fake_egress),
        ):
            results = self.script.check_shared(
                args, container_cli.APPLE_CONTAINER, work
            )
        self.assertLess(time.monotonic() - started, 10)
        by_name = {r.name: r for r in results}
        self.assertFalse(by_name["shared:both-started"].ok)
        self.assertIn("read-only file system", by_name["shared:both-started"].detail)
        self.assertIn("exit 1", by_name["shared:both-started"].detail)
        # The steps that need running sandboxes are skipped, not graded empty.
        self.assertNotIn("shared:allow-add", by_name)
        self.assertEqual(egress_calls, [("up",)])


IFCONFIG = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet6 fe80::1c%en0 prefixlen 64 secured scopeid 0xe
\tinet 192.168.1.134 netmask 0xffffff00 broadcast 192.168.1.255
utun0: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1280
\tinet 100.87.223.113 --> 100.87.223.113 netmask 0xffffffff
bridge100: flags=8a63<UP,BROADCAST,SMART,RUNNING,ALLMULTI,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.64.1 netmask 0xffffff00 broadcast 192.168.64.255
"""
IP_ADDR = """1: lo    inet 127.0.0.1/8 scope host lo
2: eth0    inet 10.0.2.15/24 brd 10.0.2.255 scope global eth0
"""


class LoopbackOnlyTests(unittest.TestCase):
    """Regression: an unresolvable hostname aborted the whole policy check."""

    def setUp(self) -> None:
        self.script = _load()

    def test_addresses_from_ifconfig_and_ip(self) -> None:
        self.assertEqual(
            self.script.host_ipv4_addresses(IFCONFIG),
            ["192.168.1.134", "100.87.223.113", "192.168.64.1"],
        )
        self.assertEqual(self.script.host_ipv4_addresses(IP_ADDR), ["10.0.2.15"])

    def test_does_not_resolve_the_hostname(self) -> None:
        with mock.patch("socket.getaddrinfo", side_effect=OSError(8, "not known")):
            result = self.script._loopback_only(
                3129,
                interfaces_text=lambda: IFCONFIG,
                answers=lambda host, _port: host == "127.0.0.1",
            )
        self.assertTrue(result.ok, result.detail)
        self.assertIn("100.87.223.113", result.detail)

    def test_fails_when_open_on_another_address(self) -> None:
        result = self.script._loopback_only(
            3129,
            interfaces_text=lambda: IFCONFIG,
            answers=lambda host, _port: host in ("127.0.0.1", "192.168.64.1"),
        )
        self.assertFalse(result.ok)
        self.assertIn("192.168.64.1", result.detail)

    def test_reports_instead_of_raising(self) -> None:
        def broken():
            raise OSError("no ifconfig")

        result = self.script._loopback_only(3129, interfaces_text=broken)
        self.assertFalse(result.ok)
        self.assertIn("could not list host addresses", result.detail)
        empty = self.script._loopback_only(3129, interfaces_text=lambda: "")
        self.assertFalse(empty.ok)


class IsolateXdgTests(unittest.TestCase):
    """Regression: a bare temporary XDG_CONFIG_HOME hid podman's machine connection."""

    def setUp(self) -> None:
        self.script = _load()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.real = self.tmp / "real-config"
        (self.real / "containers").mkdir(parents=True)
        (self.real / "containers" / "podman-connections.json").write_text("{}")
        (self.real / "project-sandbox").mkdir()
        (self.real / "project-sandbox" / "egress.toml").write_text("allow = []\n")

    def test_links_containers_config_and_isolates_the_rest(self) -> None:
        environ = {"XDG_CONFIG_HOME": str(self.real), "XDG_STATE_HOME": "/real/state"}
        xdg = self.tmp / "xdg"
        self.script.isolate_xdg(xdg, environ)
        config = Path(environ["XDG_CONFIG_HOME"])
        self.assertEqual(config, xdg / "config")
        self.assertEqual(environ["XDG_STATE_HOME"], str(xdg / "state"))
        self.assertTrue((config / "containers").is_symlink())
        self.assertTrue((config / "containers" / "podman-connections.json").exists())
        self.assertFalse((config / "project-sandbox" / "egress.toml").exists())

    def test_links_named_config_directories(self) -> None:
        (self.real / "gh").mkdir()
        (self.real / "gh" / "hosts.yml").write_text("github.com: {}\n")
        environ = {"XDG_CONFIG_HOME": str(self.real)}
        xdg = self.tmp / "xdg"
        self.script.isolate_xdg(xdg, environ, links=("containers", "gh"))
        config = Path(environ["XDG_CONFIG_HOME"])
        self.assertTrue((config / "gh" / "hosts.yml").exists())
        self.assertTrue((config / "containers").is_symlink())
        self.assertFalse((config / "project-sandbox").exists())

    def test_without_containers_config(self) -> None:
        shutil.rmtree(self.real / "containers")
        environ = {"XDG_CONFIG_HOME": str(self.real)}
        self.script.isolate_xdg(self.tmp / "xdg", environ)
        self.assertFalse((self.tmp / "xdg" / "config" / "containers").exists())

    def test_cleanup_keeps_the_real_containers_config(self) -> None:
        environ = {"XDG_CONFIG_HOME": str(self.real)}
        xdg = self.tmp / "xdg"
        self.script.isolate_xdg(xdg, environ)
        shutil.rmtree(xdg)
        self.assertTrue((self.real / "containers" / "podman-connections.json").exists())


if __name__ == "__main__":
    unittest.main()
