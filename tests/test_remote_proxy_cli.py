import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from project_sandbox import (
    agent_proxy,
    cli,
    container_cli,
    internet_proxy,
    local_service_network,
)
from project_sandbox.git_identity import GitIdentity


class RemoteProxyCliTests(unittest.TestCase):
    def preview(self, root, options, runtime="apple-container"):
        output = io.StringIO()
        with (
            patch.object(
                cli,
                "read_identity",
                return_value=GitIdentity("Test", "test@example.com"),
            ),
            patch("socket.getaddrinfo", side_effect=AssertionError("DNS in dry-run")),
            patch.object(
                agent_proxy, "resolve_key", side_effect=AssertionError("secret lookup")
            ),
            patch.object(
                agent_proxy,
                "discover_models",
                side_effect=AssertionError("model discovery"),
            ),
            patch.object(
                internet_proxy, "preflight", side_effect=AssertionError("preflight")
            ),
            patch.object(
                local_service_network.ForwardingPlan,
                "start",
                side_effect=AssertionError("forwarding start"),
            ),
            patch.object(container_cli, "run", wraps=container_cli.run) as run,
            contextlib.redirect_stdout(output),
        ):
            try:
                result = cli.main(
                    [
                        "--dry-run",
                        "--no-build",
                        "--runtime",
                        runtime,
                        "--agent",
                        "bash",
                        *options,
                        str(root),
                        "python:3.12-slim",
                    ]
                )
            finally:
                self.assertTrue(
                    all(call.kwargs.get("dry_run") for call in run.call_args_list)
                )
                self.assertEqual(list(root.iterdir()), [])
        self.assertEqual(result, 0)
        return output.getvalue()

    def test_remote_only_independent_and_combined_dry_runs(self):
        gateway = ["--agent-proxy", "https://models.example/v1", "--model", "test"]
        proxy = ["--internet-proxy", "https://proxy.example"]
        ca = ["--ca-cert", str(Path(__file__).parent / "fixtures/test-ca.pem")]
        for options in (
            [],
            gateway,
            proxy,
            gateway + proxy,
            gateway + ca,
            proxy + ca,
            gateway + proxy + ca,
        ):
            for runtime in ("docker", "podman", "apple-container"):
                with (
                    self.subTest(options=options, runtime=runtime),
                    tempfile.TemporaryDirectory() as tmp,
                ):
                    text = self.preview(Path(tmp), options, runtime)
                    relayed = proxy[0] in options and runtime == "apple-container"
                    self.assertEqual("sudo container" in text, relayed)
                    self.assertNotIn("--add-host", text)
                    if gateway[0] in options:
                        self.assertIn("https://models.example:443/v1 (direct)", text)
                    if relayed:
                        # Apple container cannot route to VPN-only proxies, so
                        # a host-loopback relay carries the traffic instead.
                        port = internet_proxy.relay_port(internet_proxy.parse(proxy[1]))
                        self.assertIn(
                            f"http://host.docker.internal:{port} (forwarded)", text
                        )
                        self.assertIn(
                            f"Would relay the Internet proxy from 127.0.0.1:{port} "
                            "to https://proxy.example",
                            text,
                        )
                    elif proxy[0] in options:
                        self.assertIn("https://proxy.example:443 (direct)", text)
                        self.assertNotIn("host.docker.internal", text)

    def test_mixed_endpoints_and_local_collision(self):
        for local_option, remote_option in (
            ("--agent-proxy", "--internet-proxy"),
            ("--internet-proxy", "--agent-proxy"),
        ):
            with self.subTest(local=local_option), tempfile.TemporaryDirectory() as tmp:
                text = self.preview(
                    Path(tmp),
                    [
                        local_option,
                        "http://127.0.0.1:4000",
                        remote_option,
                        "https://remote.example:4000",
                        "--model",
                        "test",
                    ],
                )
                self.assertIn("host.docker.internal:4000", text)
                self.assertIn("https://remote.example:4000", text)
                self.assertIn("sudo container", text)
        with (
            tempfile.TemporaryDirectory() as tmp,
            self.assertRaisesRegex(SystemExit, "Duplicate local-service port"),
        ):
            self.preview(
                Path(tmp),
                [
                    "--agent-proxy",
                    "http://127.0.0.1:4000",
                    "--internet-proxy",
                    "http://localhost:4000",
                    "--model",
                    "test",
                ],
            )

    def test_invalid_urls_fail_before_secret_network_or_writes(self):
        for option in ("--agent-proxy", "--internet-proxy"):
            for url in (
                "https://LOCALHOST.",
                "http://@host",
                "http://host:0",
                "http://[::]",
            ):
                with (
                    self.subTest(option=option, url=url),
                    tempfile.TemporaryDirectory() as tmp,
                ):
                    with self.assertRaises(SystemExit):
                        self.preview(Path(tmp), [option, url, "--model", "test"])

    def test_remote_ollama_bypass_and_destination_planning(self):
        config = internet_proxy.parse("https://proxy.example")
        services = cli._local_services(config, proxy_port=None, pi_ollama_enabled=True)
        self.assertEqual([(s.label, s.port) for s in services], [("Ollama", 11434)])
        destinations = cli._local_firewall_destinations(
            config, proxy_port=None, pi_ollama_enabled=True
        )
        self.assertEqual(
            [(d.hostname, d.port) for d in destinations],
            [("proxy.example", 443), ("host.docker.internal", 11434)],
        )
        env = internet_proxy.environment(config, bypass_local_services=True)
        self.assertEqual(env["NO_PROXY"].count("host.docker.internal"), 1)

    def test_no_firewall_remote_does_not_warn_about_local_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = self.preview(
                Path(tmp),
                [
                    "--agent-proxy",
                    "https://models.example/v1",
                    "--model",
                    "test",
                    "--no-firewall",
                ],
            )
            self.assertNotIn("internal hostname may not resolve", text)

    def test_real_session_planning_preserves_remote_agent_configs_and_bash_env(self):
        for agent, headless in (
            ("pi", False),
            ("opencode", False),
            ("bash", False),
            ("bash", True),
        ):
            with (
                self.subTest(agent=agent, headless=headless),
                tempfile.TemporaryDirectory() as tmp,
            ):
                root = Path(tmp)
                url = "https://models.example:8443/api/v1"

                def launch(command, *, root=root, agent=agent, url=url, **kwargs):
                    self.assertNotIn("gateway-secret", " ".join(command))
                    self.assertNotIn("--add-host", command)
                    dev_env = json.loads(
                        (root / ".devcontainer/devcontainer.json").read_text()
                    )["containerEnv"]
                    self.assertEqual(
                        dev_env["HTTPS_PROXY"], "https://proxy.example:443"
                    )
                    self.assertNotIn("gateway-secret", str(dev_env))
                    for configured in (
                        ("pi", "opencode") if agent == "bash" else (agent,)
                    ):
                        name = "models.json" if configured == "pi" else "opencode.json"
                        text = (
                            root / ".project-sandbox" / configured / name
                        ).read_text()
                        self.assertIn(url, text)
                        self.assertIn("gateway-secret", text)
                        self.assertIn("test-model", text)
                    if agent == "bash":
                        self.assertEqual(kwargs["env"]["OPENAI_BASE_URL"], url)
                        self.assertEqual(
                            kwargs["env"]["OPENAI_API_KEY"], "gateway-secret"
                        )
                    self.assertEqual(
                        kwargs["env"]["NO_PROXY"],
                        "localhost,127.0.0.1,::1,models.example,[::1]",
                    )
                    self.assertEqual(
                        kwargs["env"]["HTTPS_PROXY"], "https://proxy.example:443"
                    )
                    return 0

                with (
                    patch.object(
                        cli,
                        "read_identity",
                        return_value=GitIdentity("Test", "test@example.com"),
                    ),
                    patch.object(
                        container_cli,
                        "select_runtime",
                        return_value=container_cli.DOCKER,
                    ),
                    patch.object(
                        container_cli, "ensure_system_started", return_value=0
                    ),
                    patch.object(container_cli, "image_exists", return_value=True),
                    patch.object(container_cli, "run", side_effect=launch),
                    patch.object(cli.session, "run", side_effect=launch),
                    patch.object(
                        agent_proxy,
                        "resolve_key",
                        return_value=("gateway-secret", "environment"),
                    ),
                    patch.object(
                        agent_proxy, "discover_models", return_value=["test-model"]
                    ),
                    patch.object(internet_proxy, "preflight"),
                    patch.object(
                        local_service_network,
                        "prepare_services",
                        side_effect=AssertionError("remote forwarding"),
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    options = ["--prompt-text", "true"] if headless else []
                    self.assertEqual(
                        cli.main(
                            [
                                "--no-build",
                                "--agent",
                                agent,
                                "--agent-proxy",
                                url,
                                "--internet-proxy",
                                "https://proxy.example",
                                "--model",
                                ("agent-proxy/" if agent == "opencode" else "")
                                + "test-model",
                                *options,
                                str(root),
                                "python:3.12-slim",
                            ]
                        ),
                        0,
                    )

    def test_internet_only_keeps_ordinary_credentials_without_gateway_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(
                    cli,
                    "read_identity",
                    return_value=GitIdentity("Test", "test@example.com"),
                ),
                patch.object(
                    container_cli, "select_runtime", return_value=container_cli.DOCKER
                ),
                patch.object(container_cli, "ensure_system_started", return_value=0),
                patch.object(container_cli, "image_exists", return_value=True),
                patch.object(container_cli, "run", return_value=0) as run,
                patch.object(
                    cli,
                    "_api_key_env_values",
                    return_value={"OPENAI_API_KEY": "provider-secret"},
                ),
                patch.object(
                    agent_proxy,
                    "resolve_key",
                    side_effect=AssertionError("gateway key lookup"),
                ),
                patch.object(
                    agent_proxy,
                    "discover_models",
                    side_effect=AssertionError("gateway discovery"),
                ),
                patch.object(internet_proxy, "preflight"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    cli.main(
                        [
                            "--no-build",
                            "--agent",
                            "bash",
                            "--internet-proxy",
                            "https://proxy.example",
                            str(root),
                            "python:3.12-slim",
                        ]
                    ),
                    0,
                )
            self.assertEqual(
                run.call_args.kwargs["env"]["OPENAI_API_KEY"], "provider-secret"
            )
            self.assertEqual(
                run.call_args.kwargs["env"]["HTTPS_PROXY"], "https://proxy.example:443"
            )
