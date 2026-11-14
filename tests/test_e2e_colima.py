"""Runtime-free tests for scripts/e2e-colima.py (its graders and refusals)."""

import importlib.util
import io
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest import TestCase, mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _load():
    path = Path(__file__).parents[1] / "scripts" / "e2e-colima.py"
    spec = importlib.util.spec_from_file_location("e2e_colima", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


e2e = _load()
PROXY = "http://egress:3128"

GOOD_PROBE: dict[str, Any] = {
    "env": {
        "HTTP_PROXY": PROXY,
        "HTTPS_PROXY": PROXY,
        "http_proxy": PROXY,
        "https_proxy": PROXY,
    },
    "interfaces": ["eth0"],
    "default_route": False,
    "egress_address": "172.19.0.2",
    "allowed_https": 404,
    "denied_http": 403,
    "denied_https": "URLError",
    "direct_https_denied": "URLError",
    "extra_https": "URLError",
    "public_tcp": False,
    "public_ipv6_tcp": False,
    "public_dns_udp": False,
    "resolves_public_name": False,
    "vm_gateway_tcp": False,
    "own_gateway_tcp": False,
    "uplink_tcp": False,
    "host_docker_internal": False,
}


def failed(results) -> list[str]:
    return [r.name for r in results if not r.ok]


class GradeLaunchTests(TestCase):
    def test_a_correct_probe_passes_every_check(self) -> None:
        results = e2e.grade_launch(GOOD_PROBE)
        self.assertEqual(failed(results), [])
        self.assertGreaterEqual(len(results), 14)

    def test_each_reachable_bypass_is_detected_by_its_own_check(self) -> None:
        for key in (
            "public_tcp",
            "public_ipv6_tcp",
            "public_dns_udp",
            "resolves_public_name",
            "vm_gateway_tcp",
            "own_gateway_tcp",
            "uplink_tcp",
            "host_docker_internal",
        ):
            with self.subTest(key=key):
                self.assertEqual(
                    failed(e2e.grade_launch({**GOOD_PROBE, key: True})),
                    [f"launch:{key}-fails"],
                )

    def test_an_unanswered_probe_is_not_a_pass(self) -> None:
        # A missing key (the probe died early) must not read as "blocked".
        probe = {k: v for k, v in GOOD_PROBE.items() if k != "public_tcp"}
        self.assertIn("launch:public_tcp-fails", failed(e2e.grade_launch(probe)))
        self.assertTrue(failed(e2e.grade_launch({})))

    def test_wrong_proxy_variables_fail(self) -> None:
        env = {**GOOD_PROBE["env"], "https_proxy": "http://host.docker.internal:3129"}
        self.assertEqual(
            failed(e2e.grade_launch({**GOOD_PROBE, "env": env})),
            ["launch:proxy-variables"],
        )
        self.assertIn(
            "launch:proxy-variables",
            failed(e2e.grade_launch({**GOOD_PROBE, "env": {}})),
        )

    def test_a_second_interface_or_a_default_route_fails(self) -> None:
        for change in ({"interfaces": ["eth0", "eth1"]}, {"default_route": True}):
            with self.subTest(change=change):
                self.assertEqual(
                    failed(e2e.grade_launch({**GOOD_PROBE, **change})),
                    ["launch:one-interface-no-default-route"],
                )

    def test_policy_outcomes_through_the_proxy(self) -> None:
        cases = {
            "launch:allowed-host-through-squid": {"allowed_https": "URLError"},
            "launch:unlisted-http-is-403": {"denied_http": 200},
            "launch:unlisted-https-is-not-served": {"denied_https": 200},
            "launch:proxy-ignored-is-denied": {"direct_https_denied": 200},
        }
        for name, change in cases.items():
            with self.subTest(name=name):
                self.assertEqual(
                    failed(e2e.grade_launch({**GOOD_PROBE, **change})), [name]
                )


GOOD_SIDECAR = {
    "NetworkSettings": {
        "Ports": {},
        "Networks": {
            "psbx-egress-uplink": {"IPAddress": "172.18.0.2", "Aliases": []},
            "psbx-sandbox": {"IPAddress": "172.19.0.2", "Aliases": ["egress"]},
        },
    },
    "HostConfig": {"PortBindings": {}},
}

PROJECT_NETWORKS = ["psbx-egress-uplink", "psbx-sandbox"]


class GradeSidecarTests(TestCase):
    def grade(self, container=None, *, ip_forward="0\n", networks=None):
        return e2e.grade_sidecar(
            container or GOOD_SIDECAR,
            ip_forward=ip_forward,
            networks=networks or PROJECT_NETWORKS,
        )

    def test_the_expected_sidecar_passes(self) -> None:
        self.assertEqual(failed(self.grade()), [])

    def test_published_ports_are_detected(self) -> None:
        bad = {
            **GOOD_SIDECAR,
            "NetworkSettings": {
                **GOOD_SIDECAR["NetworkSettings"],
                "Ports": {"3128/tcp": [{"HostPort": "3129"}]},
            },
        }
        self.assertEqual(failed(self.grade(bad)), ["sidecar:publishes-nothing"])

    def test_ip_forwarding_is_detected(self) -> None:
        self.assertEqual(
            failed(self.grade(ip_forward="1\n")), ["sidecar:does-not-forward-ip"]
        )

    def test_a_missing_alias_or_attachment_is_detected(self) -> None:
        bad = {
            **GOOD_SIDECAR,
            "NetworkSettings": {
                "Ports": {},
                "Networks": {
                    "psbx-egress-uplink": {"IPAddress": "172.18.0.2", "Aliases": []}
                },
            },
        }
        self.assertEqual(failed(self.grade(bad)), ["sidecar:on-both-networks"])

    def test_extra_or_missing_project_networks_are_detected(self) -> None:
        for networks in (PROJECT_NETWORKS + ["psbx-extra"], ["psbx-sandbox"]):
            with self.subTest(networks=networks):
                self.assertEqual(
                    failed(self.grade(networks=networks)),
                    ["sidecar:exactly-two-project-networks"],
                )


class GradeDenialAndFirewallTests(TestCase):
    def test_a_logged_and_attributed_denial_passes(self) -> None:
        log = "2026-11-14T23:35:56  172.19.0.3  example.com  host-not-allowed"
        self.assertEqual(failed(e2e.grade_denials(log, "172.19.0.")), [])

    def test_a_missing_or_misattributed_denial_fails(self) -> None:
        self.assertEqual(len(failed(e2e.grade_denials("", "172.19.0."))), 2)
        wrong = "2026  10.0.0.9  example.com  host-not-allowed"
        self.assertEqual(
            failed(e2e.grade_denials(wrong, "172.19.0.")),
            ["launch:denial-attributed-to-the-sandbox-address"],
        )

    def test_firewall_mode_probe(self) -> None:
        probe = {
            "env": {k: None for k in GOOD_PROBE["env"]},
            "egress_address": None,
            "extra_https": 200,
            "direct_https_denied": "URLError",
            "public_dns_udp": False,
        }
        self.assertEqual(failed(e2e.grade_firewall(probe)), [])
        self.assertEqual(
            failed(e2e.grade_firewall({**probe, "extra_https": "URLError"})),
            ["firewall:extra-domain-reachable"],
        )
        self.assertEqual(
            failed(e2e.grade_firewall({**probe, "egress_address": "172.19.0.2"})),
            ["firewall:no-private-network-pieces"],
        )


class ProbeAndMainTests(TestCase):
    def test_the_probe_source_is_valid_python_with_its_config(self) -> None:
        config = {
            "proxy": PROXY,
            "allowed": "api.anthropic.com",
            "denied": "example.com",
            "extra": "pypi.org",
            "direct": False,
            "vm_gateway": ("172.17.0.1", 80),
            "own_gateway": ("172.19.0.1", 80),
            "uplink": (None, 3128),
        }
        source = e2e.probe_source(config)
        compile(source, "probe.py", "exec")
        self.assertIn("172.17.0.1", source)
        self.assertNotIn("__CONFIG__", source)

    def test_unknown_checks_are_refused_before_any_docker_call(self) -> None:
        with (
            mock.patch("subprocess.run", side_effect=AssertionError("ran")),
            mock.patch("sys.stderr", new_callable=io.StringIO),
        ):
            self.assertEqual(e2e.main(["--checks", "launch,nope"]), 2)

    def test_an_existing_sidecar_is_not_replaced_without_consent(self) -> None:
        def run(argv, *_a, **_k):
            return subprocess.CompletedProcess(argv, 0, "[{}]", "")

        with (
            mock.patch.object(e2e.container_cli, "verify_colima_profile"),
            mock.patch("subprocess.run", side_effect=run),
            mock.patch("sys.stderr", new_callable=io.StringIO) as err,
        ):
            self.assertEqual(e2e.main([]), 2)
        self.assertIn("--replace-existing", err.getvalue())

    def test_a_stopped_profile_is_refused(self) -> None:
        with (
            mock.patch.object(
                e2e.container_cli,
                "verify_colima_profile",
                side_effect=SystemExit("stopped"),
            ),
            mock.patch("sys.stderr", new_callable=io.StringIO),
        ):
            self.assertEqual(e2e.main([]), 2)
