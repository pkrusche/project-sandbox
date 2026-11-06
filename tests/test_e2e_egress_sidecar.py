"""Runtime-free tests for scripts/e2e-egress-sidecar.py."""

import contextlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

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
    "SBX 1.1 TCP_DENIED/200 CONNECT 1.1.1.1:443 sni=- bump=bump err=TCP_RESET cause=ip-literal peer=-",
    "SBX 1.2 TCP_DENIED/200 CONNECT 169.254.169.254:443 sni=- bump=bump err=TCP_RESET cause=ip-literal peer=-",
    "SBX 1.3 TCP_DENIED/200 CONNECT pypi.org:8443 sni=- bump=bump err=TCP_RESET cause=port-not-allowed peer=-",
    "SBX 1.4 TCP_DENIED/200 CONNECT 169.254.169.254.nip.io:443 sni=169.254.169.254.nip.io bump=bump err=TCP_RESET cause=private-address peer=-",
    "SBX 1.5 NONE_NONE/200 CONNECT pypi.org:443 sni=files.pythonhosted.org bump=terminate err=- cause=- peer=-",
    "SBX 1.6 NONE_NONE/200 CONNECT pypi.org:443 sni=- bump=peek err=ERR_PROTOCOL_UNKNOWN cause=- peer=-",
    "SBX 1.7 TCP_DENIED/200 CONNECT github.com:443 sni=- bump=bump err=TCP_RESET cause=host-not-allowed peer=-",
    "SBX 1.8 TCP_TUNNEL/200 CONNECT pypi.org:443 sni=pypi.org bump=splice err=- cause=- peer=151.101.0.223",
)
GOOD_LOG = "\n".join(GOOD_LOG_LINES)


class ArgumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = _load()

    def test_defaults(self) -> None:
        args = self.script.parse_args([])
        self.assertEqual(args.runtime, "auto")
        self.assertEqual(args.checks, ("policy", "shared", "parent", "forward"))
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
        self.assertEqual(len(results), 15)

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

    def test_session_id_from_output(self) -> None:
        self.assertEqual(
            self.script.session_id_from(
                "x\nSession: 20261105T181615-f066e939087e\nContainer: y\n"
            ),
            "20261105T181615-f066e939087e",
        )
        self.assertIsNone(self.script.session_id_from("no session"))

    def test_default_network_ip(self) -> None:
        docker = json.dumps(
            [
                {
                    "NetworkSettings": {
                        "Networks": {
                            "bridge": {"IPAddress": "172.17.0.2"},
                            "sbx-egress-x": {"IPAddress": "172.18.0.2"},
                        }
                    }
                }
            ]
        )
        apple = json.dumps(
            [
                {
                    "status": {
                        "networks": [
                            {"network": "default", "ipv4Address": "192.168.64.5/24"},
                            {
                                "network": "sbx-egress-x",
                                "ipv4Address": "192.168.128.2/24",
                            },
                        ]
                    }
                }
            ]
        )
        self.assertEqual(
            self.script.default_network_ip(
                container_cli.DOCKER, docker, "sbx-egress-x"
            ),
            "172.17.0.2",
        )
        self.assertEqual(
            self.script.default_network_ip(
                container_cli.APPLE_CONTAINER, apple, "sbx-egress-x"
            ),
            "192.168.64.5",
        )
        self.assertIsNone(
            self.script.default_network_ip(container_cli.DOCKER, "nope", "x")
        )

    def test_probe_source_is_valid_python(self) -> None:
        hosts = (
            "pypi.org",
            "files.pythonhosted.org",
            "example.com",
            "169.254.169.254.nip.io",
        )
        compile(
            self.script.PROBE_SOURCE.replace("__HOSTS__", repr(hosts)),
            "probe.py",
            "exec",
        )
        compile(
            self.script.PARENT_PROBE_SOURCE.replace("__ALLOWED__", "pypi.org").replace(
                "__UNLISTED__", "example.com"
            ),
            "parent.py",
            "exec",
        )


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
