import contextlib
import io
import socket
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from project_sandbox import cli, firewall, internet_proxy
from project_sandbox.git_identity import GitIdentity


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
            "http://*:8080",
            "http://example.com:8080",
            "http://user:pass@127.0.0.1:8080",
            "http://127.0.0.1",
            "http://127.0.0.1:0",
            "http://127.0.0.1:65536",
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

    def test_proxy_environment_overrides_injected_dotenv_routing_values(self) -> None:
        config = internet_proxy.parse("http://localhost:18080")
        injected = {
            "API_TOKEN": "secret",
            "HTTP_PROXY": "http://attacker.invalid:3128",
            "NO_PROXY": "*",
        }

        merged = internet_proxy.merge_environment(
            injected, config, bypass_local_services=True
        )

        self.assertEqual(merged["API_TOKEN"], "secret")
        self.assertEqual(merged["HTTP_PROXY"], config.forwarded_url)
        self.assertNotEqual(merged["NO_PROXY"], "*")

    def test_agent_proxy_ipv6_listener_is_preserved_in_service_plan(self) -> None:
        services = cli._local_services(
            None,
            proxy_port=4000,
            proxy_loopback_host="::1",
            pi_ollama_enabled=False,
        )

        self.assertEqual(services[0].loopback_host, "::1")

    def test_preflight_is_bounded_and_actionable(self) -> None:
        config = internet_proxy.parse("http://127.0.0.1:18080")
        with patch.object(
            socket, "create_connection", side_effect=OSError("refused")
        ) as connect:
            with self.assertRaisesRegex(SystemExit, "configured proxy"):
                internet_proxy.preflight(config)
        connect.assert_called_once_with(("127.0.0.1", 18080), timeout=2.0)

    def test_cli_rejects_firewall_and_destination_policy_conflicts(self) -> None:
        base = ["--internet-proxy", "http://127.0.0.1:18080"]
        for conflict, explanation in (
            (["--no-firewall"], "bypassable without firewall enforcement"),
            (["--allow-github"], "external proxy"),
            (["--extra-domain", "x.test"], "external proxy"),
        ):
            args = cli.build_parser().parse_args([*base, *conflict, "project"])
            with (
                self.subTest(conflict=conflict),
                self.assertRaisesRegex(SystemExit, explanation),
            ):
                cli._validate_internet_proxy_args(args)

    def test_cli_rejects_chroot_without_an_isolated_firewall(self) -> None:
        args = cli.build_parser().parse_args(
            [
                "--runtime",
                "chroot",
                "--internet-proxy",
                "http://127.0.0.1:18080",
                "project",
            ]
        )
        with self.assertRaisesRegex(SystemExit, "cannot enforce an isolated firewall"):
            cli._validate_internet_proxy_args(args)

    def test_absent_option_is_no_op(self) -> None:
        args = cli.build_parser().parse_args(["project"])
        self.assertIsNone(cli._validate_internet_proxy_args(args))

    def test_dry_run_does_not_read_secrets_or_preflight_network(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            dotenv = project / "secrets.env"
            dotenv.write_text("API_TOKEN=must-not-be-read\n", encoding="utf-8")
            output = io.StringIO()
            with (
                patch.object(
                    cli,
                    "read_identity",
                    return_value=GitIdentity("Ada", "ada@example.com"),
                ),
                patch.object(
                    cli.container_cli,
                    "select_runtime",
                    return_value=cli.container_cli.DOCKER,
                ),
                patch.object(
                    cli,
                    "_api_key_env_values",
                    side_effect=AssertionError("dry-run read secrets"),
                ),
                patch.object(internet_proxy, "preflight") as preflight,
                contextlib.redirect_stdout(output),
            ):
                result = cli.main(
                    [
                        "--dry-run",
                        "--runtime",
                        "docker",
                        "--agent",
                        "bash",
                        "--no-forward-credentials",
                        "--api-key-env-file",
                        str(dotenv),
                        "--internet-proxy",
                        "http://127.0.0.1:18080",
                        str(project),
                        "python:3.12-slim",
                    ]
                )

        self.assertEqual(result, 0)
        preflight.assert_not_called()
        self.assertIn("without reading them during dry-run", output.getvalue())
        self.assertIn("HTTP_PROXY", output.getvalue())

    def test_failed_preflight_precedes_secret_and_filesystem_work(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            with (
                patch.object(
                    cli,
                    "read_identity",
                    return_value=GitIdentity("Ada", "ada@example.com"),
                ),
                patch.object(
                    cli.container_cli,
                    "select_runtime",
                    return_value=cli.container_cli.DOCKER,
                ),
                patch.object(
                    internet_proxy,
                    "preflight",
                    side_effect=SystemExit("listener unavailable"),
                ) as preflight,
                patch.object(
                    cli,
                    "_api_key_env_values",
                    side_effect=AssertionError("read secrets before preflight"),
                ),
                patch.object(
                    cli.dockerfile,
                    "render",
                    side_effect=AssertionError("rendered before preflight"),
                ),
                self.assertRaisesRegex(SystemExit, "listener unavailable"),
            ):
                cli.main(
                    [
                        "--runtime",
                        "docker",
                        "--agent",
                        "bash",
                        "--internet-proxy",
                        "http://127.0.0.1:18080",
                        str(project),
                        "python:3.12-slim",
                    ]
                )

        preflight.assert_called_once()
        self.assertFalse((project / ".project-sandbox").exists())

    def test_successful_preflight_continues_to_sandbox_preparation(self) -> None:
        events: list[str] = []
        with TemporaryDirectory() as tmp:
            project = Path(tmp)

            def preflight(_config) -> None:
                events.append("preflight")

            def begin_filesystem_work(_path: Path) -> Path:
                events.append("filesystem")
                raise SystemExit("stop after ordering check")

            with (
                patch.object(
                    cli,
                    "read_identity",
                    return_value=GitIdentity("Ada", "ada@example.com"),
                ),
                patch.object(
                    cli.container_cli,
                    "select_runtime",
                    return_value=cli.container_cli.DOCKER,
                ),
                patch.object(internet_proxy, "preflight", side_effect=preflight),
                patch.object(cli, "ensure_dir", side_effect=begin_filesystem_work),
                self.assertRaisesRegex(SystemExit, "stop after ordering check"),
            ):
                cli.main(
                    [
                        "--runtime",
                        "docker",
                        "--agent",
                        "bash",
                        "--internet-proxy",
                        "http://127.0.0.1:18080",
                        str(project),
                        "python:3.12-slim",
                    ]
                )

        self.assertEqual(events, ["preflight", "filesystem"])

    def test_proxy_firewall_has_only_exact_local_ports(self) -> None:
        with TemporaryDirectory() as tmp:
            firewall.render(
                Path(tmp),
                extra_domains=[],
                policy=firewall.INTERNET_PROXY,
                pi_ollama=True,
                local_destinations=[
                    firewall.LocalTcpDestination(
                        "Internet proxy", "host.docker.internal", 18080
                    ),
                    firewall.LocalTcpDestination(
                        "Ollama", "host.docker.internal", 11434
                    ),
                ],
            )
            rendered = [
                (Path(tmp) / name).read_text()
                for name in ("init-firewall.sh", "init-firewall-devcontainer.sh")
            ]
        for text in rendered:
            with self.subTest(script=text.splitlines()[-1]):
                self.assertIn("--dport 18080", text)
                self.assertIn("--dport 11434", text)
                self.assertNotIn("ipset create allowed-ipv4", text)
                self.assertNotIn('"api.openai.com"', text)
                self.assertNotIn("api.github.com/meta", text)
                self.assertNotIn("Allowing host gateway", text)
                self.assertIn("requires enforceable IPv6 firewall policy", text)
                self.assertIn("--dport 53 -j DROP", text)
                self.assertIn("exec 3<>/dev/tcp/host.docker.internal/18080", text)
                self.assertIn(
                    "configure the localhost domain and then restart runtime networking",
                    text,
                )
                self.assertIn("final firewall cannot reach loopback-bound Ollama", text)
