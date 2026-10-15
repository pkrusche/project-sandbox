"""Execute generated resolution and rule selection without modifying host policy."""

import contextlib
import io
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from project_sandbox import agent_proxy, firewall


class EndpointFirewallTests(unittest.TestCase):
    def render(self, root, destinations, **kwargs):
        return firewall.render(
            root,
            extra_domains=[],
            policy=firewall.INTERNET_PROXY,
            local_destinations=destinations,
            **kwargs,
        ).read_text()

    def resolve(self, root, destinations, answers, ipv6=True):
        text = self.render(root, destinations)
        source = text.split("<<'PY_ENDPOINTS'\n", 1)[1].split("\nPY_ENDPOINTS", 1)[0]
        hosts = root / "hosts"
        hosts.write_text("127.0.0.1 localhost\n10.0.0.9 proxy.example unrelated\n")
        source = source.replace('Path("/etc/hosts")', f"Path({str(hosts)!r})")
        output = io.StringIO()

        def lookup(host, *args, **kwargs):
            value = answers[host]
            if isinstance(value, Exception):
                raise value
            return [(0, 0, 0, "", (address, 0)) for address in value]

        with (
            patch("socket.getaddrinfo", side_effect=lookup) as dns,
            patch("sys.argv", ["-", str(int(ipv6))]),
            contextlib.redirect_stdout(output),
        ):
            exec(compile(source, "<rendered resolution>", "exec"), {})  # noqa: S102 — execute generated fixture
        return text, hosts.read_text(), output.getvalue(), dns.call_count

    def test_multiple_answers_deduplication_and_exact_pair_rules(self):
        destinations = [
            firewall.TcpDestination(
                "Internet proxy", "proxy.example", 8443, local=False
            ),
            firewall.TcpDestination("Agent proxy", "models.example", 443, local=False),
            firewall.TcpDestination("Other", "proxy.example", 8080, local=False),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            text, hosts, assignments, calls = self.resolve(
                Path(tmp),
                destinations,
                {
                    "proxy.example": ["10.0.0.1", "2001:db8::1", "10.0.0.1"],
                    "models.example": ["10.0.0.2"],
                },
            )
            self.assertEqual(calls, 2)
            self.assertEqual(hosts.count("10.0.0.1\tproxy.example"), 1)
            self.assertIn("2001:db8::1\tproxy.example", hosts)
            self.assertIn("10.0.0.9\tunrelated", hosts)
            self.assertNotIn("10.0.0.9 proxy.example", hosts)
            # Run the rendered loops with fake iptables, recording actual argv.
            rules = text.split("# Internet proxy: exact", 1)[1].split(
                "iptables -P INPUT DROP", 1
            )[0]
            rules = "# Internet proxy: exact" + rules
            result = subprocess.run(
                [
                    "bash",
                    "-eu",
                    "-c",
                    'iptables() { echo "v4 $*"; }; ip6tables() { echo "v6 $*"; };\n'
                    + assignments
                    + rules,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("v4 -A OUTPUT -d 10.0.0.1 -p tcp --dport 8443", result.stdout)
            self.assertIn(
                "v6 -A OUTPUT -d 2001:db8::1 -p tcp --dport 8443", result.stdout
            )
            self.assertIn("v4 -A OUTPUT -d 10.0.0.2 -p tcp --dport 443", result.stdout)
            self.assertNotIn("-d 10.0.0.1 -p tcp --dport 443", result.stdout)
            self.assertNotIn("-d 10.0.0.2 -p tcp --dport 8443", result.stdout)

    def test_bad_resolution_fails_before_pinning(self):
        for addresses in (
            ["127.0.0.1"],
            ["::ffff:127.0.0.1"],
            ["0.0.0.0"],
            ["224.1.1.1"],
            ["ff02::1"],
            ["::"],
            ["fe80::1%eth0"],
            ["10.0.0.1", "127.0.0.1"],
            [],
            socket.gaierror(),
        ):
            with (
                self.subTest(addresses=addresses),
                tempfile.TemporaryDirectory() as tmp,
            ):
                with self.assertRaises(SystemExit):
                    self.resolve(
                        Path(tmp),
                        [
                            firewall.TcpDestination(
                                "Internet proxy", "proxy.example", 443, local=False
                            )
                        ],
                        {"proxy.example": addresses},
                    )
                self.assertEqual(
                    (Path(tmp) / "hosts").read_text(),
                    "127.0.0.1 localhost\n10.0.0.9 proxy.example unrelated\n",
                )

    def test_literals_and_ipv6_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, hosts, assignments, calls = self.resolve(
                root,
                [
                    firewall.TcpDestination(
                        "Internet proxy", "10.2.3.4", 443, local=False
                    )
                ],
                {},
            )
            self.assertEqual(calls, 0)
            self.assertNotIn("10.2.3.4", hosts)
            self.assertIn("10.2.3.4", assignments)
            _, hosts, assignments, _ = self.resolve(
                root,
                [
                    firewall.TcpDestination(
                        "Agent proxy", "models.example", 443, local=False
                    )
                ],
                {"models.example": ["10.0.0.1", "2001:db8::1"]},
                ipv6=False,
            )
            self.assertNotIn("2001:db8::1", hosts + assignments)
            with self.assertRaisesRegex(SystemExit, "No enforceable address family"):
                self.resolve(
                    root,
                    [
                        firewall.TcpDestination(
                            "Agent proxy", "models.example", 443, local=False
                        )
                    ],
                    {"models.example": ["2001:db8::1"]},
                    ipv6=False,
                )

    def test_mapped_ipv4_uses_ipv4_policy_without_ipv6(self):
        for host, answers in (
            ("::ffff:10.2.3.4", {}),
            ("proxy.example", {"proxy.example": ["::ffff:10.2.3.4"]}),
        ):
            with self.subTest(host=host), tempfile.TemporaryDirectory() as tmp:
                _, hosts, assignments, _ = self.resolve(
                    Path(tmp),
                    [firewall.TcpDestination("Internet proxy", host, 443, local=False)],
                    answers,
                    ipv6=False,
                )
                self.assertEqual(assignments, "ENDPOINT_0=(10.2.3.4)\n")
                self.assertNotIn("::ffff:", hosts)
                if answers:
                    self.assertIn("10.2.3.4\tproxy.example", hosts)

    def test_final_probes_and_policy_for_both_variants(self):
        endpoint = agent_proxy.parse("https://models.example/api/v1")
        destinations = [
            firewall.TcpDestination(
                "Internet proxy", "proxy.example", 8443, "https", local=False
            ),
            firewall.TcpDestination.from_endpoint("Agent proxy", endpoint),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.render(
                root,
                destinations,
                agent_proxy_port=443,
                agent_proxy_hostname=endpoint.host,
                agent_proxy_url=endpoint.sandbox_url,
            )
            for file in root.glob("*.sh"):
                text = file.read_text()
                result = subprocess.run(
                    ["bash", "-n", str(file)],
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                probe = "https://models.example:443/api/v1; then"
                self.assertGreater(
                    text.index(probe), text.index("iptables -A OUTPUT -j REJECT")
                )
                self.assertIn("--noproxy '*'", text)
                self.assertIn("ssl.create_default_context().wrap_socket", text)
                self.assertIn("requires enforceable IPv6 firewall policy", text)
                for forbidden in (
                    "api.openai.com",
                    "api.github.com",
                    "Allowing host gateway",
                    "sudo container",
                    "dig +",
                ):
                    self.assertNotIn(forbidden, text)

    def test_internet_ipv6_enforcement_failure_stops_before_resolution_or_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = self.render(
                Path(tmp),
                [
                    firewall.TcpDestination(
                        "Internet proxy", "proxy.example", 443, local=False
                    )
                ],
            )
            prefix = text.split("# Resolve every endpoint", 1)[0]
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    "ip6tables() { return 1; };\n" + prefix + "\necho AGENT_STARTED",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("requires enforceable IPv6 firewall policy", result.stderr)
            self.assertNotIn("AGENT_STARTED", result.stdout)

    def test_remote_alias_spelling_does_not_trigger_local_remediation(self):
        endpoint = agent_proxy.parse("https://host.docker.internal/v1")
        with tempfile.TemporaryDirectory() as tmp:
            text = self.render(
                Path(tmp),
                [firewall.TcpDestination.from_endpoint("Agent proxy", endpoint)],
                agent_proxy_port=443,
                agent_proxy_hostname=endpoint.host,
                agent_proxy_url=endpoint.sandbox_url,
            )
            self.assertNotIn("sudo container", text)
            self.assertNotIn("listening on 127.0.0.1", text)
