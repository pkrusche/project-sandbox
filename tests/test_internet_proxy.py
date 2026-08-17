import socket
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from project_sandbox import cli, firewall, internet_proxy


class InternetProxyTests(TestCase):
    def test_accepts_loopback_http_with_explicit_port(self) -> None:
        config = internet_proxy.parse("http://127.0.0.1:18080")
        self.assertEqual(config.port, 18080)
        self.assertEqual(config.forwarded_url, "http://host.docker.internal:18080")

    def test_accepts_ipv6_loopback(self) -> None:
        self.assertEqual(internet_proxy.parse("http://[::1]:8080").host, "::1")

    def test_rejects_unsafe_or_ambiguous_urls(self) -> None:
        invalid = (
            "https://127.0.0.1:8080",
            "http://0.0.0.0:8080",
            "http://example.com:8080",
            "http://user:pass@127.0.0.1:8080",
            "http://127.0.0.1",
            "http://127.0.0.1:8080/path",
            "http://127.0.0.1:8080?x=1",
            "http://127.0.0.1:8080#fragment",
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(SystemExit):
                internet_proxy.parse(value)

    def test_environment_has_case_pairs_and_one_shared_bypass(self) -> None:
        config = internet_proxy.parse("http://localhost:18080")
        env = internet_proxy.environment(config, bypass_local_services=True)
        self.assertEqual(env["HTTP_PROXY"], env["http_proxy"])
        self.assertEqual(env["HTTPS_PROXY"], env["https_proxy"])
        self.assertEqual(env["NO_PROXY"], env["no_proxy"])
        self.assertEqual(env["NO_PROXY"].count("host.docker.internal"), 1)

    def test_preflight_is_bounded_and_actionable(self) -> None:
        config = internet_proxy.parse("http://127.0.0.1:18080")
        with patch.object(
            socket, "create_connection", side_effect=OSError("refused")
        ) as connect:
            with self.assertRaisesRegex(SystemExit, "internet-proxy-locally"):
                internet_proxy.preflight(config)
        connect.assert_called_once_with(("127.0.0.1", 18080), timeout=2.0)

    def test_cli_rejects_firewall_and_destination_policy_conflicts(self) -> None:
        base = ["--internet-proxy", "http://127.0.0.1:18080"]
        for conflict in (["--no-firewall"], ["--allow-github"], ["--extra-domain", "x.test"]):
            args = cli.build_parser().parse_args([*base, *conflict, "project"])
            with self.subTest(conflict=conflict), self.assertRaises(SystemExit):
                cli._validate_internet_proxy_args(args)

    def test_absent_option_is_no_op(self) -> None:
        args = cli.build_parser().parse_args(["project"])
        self.assertIsNone(cli._validate_internet_proxy_args(args))

    def test_proxy_firewall_has_only_exact_local_ports(self) -> None:
        with TemporaryDirectory() as tmp:
            firewall.render(
                Path(tmp),
                extra_domains=[],
                policy=firewall.INTERNET_PROXY,
                local_destinations=[
                    firewall.LocalTcpDestination(
                        "Internet proxy", "host.docker.internal", 18080
                    ),
                    firewall.LocalTcpDestination(
                        "Ollama", "host.docker.internal", 11434
                    ),
                ],
            )
            text = (Path(tmp) / "init-firewall.sh").read_text()
        self.assertIn("--dport 18080", text)
        self.assertIn("--dport 11434", text)
        self.assertNotIn("ipset create allowed-ipv4", text)
        self.assertNotIn('"api.openai.com"', text)
        self.assertNotIn("api.github.com/meta", text)
        self.assertIn("requires enforceable IPv6 firewall policy", text)
        self.assertIn("--dport 53 -j DROP", text)
