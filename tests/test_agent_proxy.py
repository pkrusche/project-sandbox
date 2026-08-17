import contextlib
import importlib.util
import io
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from project_sandbox import agent_proxy, cli, config_agents
from project_sandbox.git_identity import GitIdentity


def _load_checker():
    path = Path(__file__).parents[1] / "scripts" / "check-agent-proxy.py"
    spec = importlib.util.spec_from_file_location("check_agent_proxy", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AgentProxyTests(unittest.TestCase):
    def test_url_validation_and_rewrite_preserve_port_and_path(self) -> None:
        self.assertEqual(
            agent_proxy.forwarded_url("http://127.0.0.1:4567/v1"),
            "http://agent-proxy.project-sandbox.internal:4567/v1",
        )
        self.assertEqual(
            agent_proxy.forwarded_url(
                "http://127.0.0.1:4567/v1", hostname="host.docker.internal"
            ),
            "http://host.docker.internal:4567/v1",
        )
        for value in (
            "https://127.0.0.1:4000/v1",
            "http://0.0.0.0:4000/v1",
            "http://example.com:4000/v1",
            "http://127.0.0.1/v1",
        ):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                agent_proxy.validate_url(value)

    @patch("project_sandbox.agent_proxy.subprocess.run")
    def test_key_resolution_is_pass_first_then_environment_then_raw(
        self, run: Mock
    ) -> None:
        run.return_value = Mock(returncode=0, stdout="pass-key\n")
        with patch.dict(os.environ, {"TEST_GATEWAY_KEY": "env-key"}):
            self.assertEqual(
                agent_proxy.resolve_key("TEST_GATEWAY_KEY", "raw-key"),
                ("pass-key", "pass"),
            )
        run.return_value = Mock(returncode=1, stdout="")
        with patch.dict(os.environ, {"TEST_GATEWAY_KEY": "env-key"}):
            self.assertEqual(
                agent_proxy.resolve_key("TEST_GATEWAY_KEY", "raw-key"),
                ("env-key", "environment"),
            )
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                agent_proxy.resolve_key("TEST_GATEWAY_KEY", "raw-key"),
                ("raw-key", "command line"),
            )

    @patch("project_sandbox.agent_proxy.urllib.request.build_opener")
    def test_discovery_preserves_order_and_deduplicates(
        self, build_opener: Mock
    ) -> None:
        response = Mock()
        response.__enter__ = Mock(
            return_value=io.StringIO(
                json.dumps({"data": [{"id": "b"}, {"id": "a"}, {"id": "b"}]})
            )
        )
        response.__exit__ = Mock(return_value=False)
        opener = build_opener.return_value
        opener.open.return_value = response
        self.assertEqual(
            agent_proxy.discover_models("http://127.0.0.1:4000/v1", "secret"),
            ["b", "a"],
        )
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        self.assertIsInstance(
            build_opener.call_args.args[0], agent_proxy._NoRedirectHandler
        )

    def test_discovery_does_not_forward_credentials_across_redirects(self) -> None:
        handler = agent_proxy._NoRedirectHandler()
        self.assertIsNone(
            handler.redirect_request(
                Mock(),
                Mock(),
                302,
                "Found",
                {},
                "http://example.com/models",
            )
        )

    def test_agent_renderers_include_complete_catalog_and_key(self) -> None:
        for selected in ("pi", "opencode"):
            with self.subTest(selected=selected), TemporaryDirectory() as tmp:
                root = Path(tmp)
                paths = config_agents.render(
                    root,
                    agent_proxy=(
                        "http://proxy:4000/v1",
                        ["b", "a"],
                        "gateway-key",
                        selected,
                    ),
                )
                config = (
                    root
                    / selected
                    / ("models.json" if selected == "pi" else "opencode.json")
                ).read_text()
                self.assertIn("gateway-key", config)
                self.assertLess(config.index('"b"'), config.index('"a"'))
                self.assertEqual(paths[selected].parent, root / selected)

    def test_render_removes_stale_proxy_configs_and_only_stages_selected_agent(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pi_proxy = root / "pi" / "models.json"
            opencode_proxy = root / "opencode" / "opencode.json"

            config_agents.render(
                root,
                agent_proxy=("http://proxy:4000/v1", ["model"], "old-key", "pi"),
            )
            self.assertTrue(pi_proxy.exists())

            config_agents.render(
                root,
                agent_proxy=(
                    "http://proxy:4000/v1",
                    ["model"],
                    "new-key",
                    "opencode",
                ),
            )
            self.assertFalse(pi_proxy.exists())
            self.assertTrue(opencode_proxy.exists())

            config_agents.render(root)
            self.assertFalse(pi_proxy.exists())
            self.assertFalse(opencode_proxy.exists())

    def test_proxy_key_is_staged_only_after_image_build(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            context = project / ".project-sandbox"
            secret_file = context / "pi" / "models.json"

            def build_image(**_kwargs) -> int:
                self.assertFalse(secret_file.exists())
                return 0

            def run(_command, **_kwargs) -> int:
                self.assertIn("gateway-key", secret_file.read_text(encoding="utf-8"))
                self.assertIn("PROJECT_SANDBOX_MODEL=agent-proxy/model", _command)
                return 0

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
                    cli.container_cli, "ensure_system_started", return_value=0
                ),
                patch.object(cli.container_cli, "build_image", side_effect=build_image),
                patch.object(cli.container_cli, "run", side_effect=run),
                patch.object(
                    cli.ollama_network,
                    "prepare",
                    return_value=cli.ollama_network.ForwardingPlan(
                        "docker-desktop-host-alias"
                    ),
                ),
                patch.object(
                    cli.agent_proxy,
                    "resolve_key",
                    return_value=("gateway-key", "environment"),
                ),
                patch.object(
                    cli.agent_proxy, "discover_models", return_value=["model"]
                ),
            ):
                self.assertEqual(
                    cli.main(
                        [
                            "--agent",
                            "pi",
                            "--agent-proxy",
                            "http://127.0.0.1:4000/v1",
                            "--model",
                            "model",
                            str(project),
                            "python:3.12-slim",
                        ]
                    ),
                    0,
                )
            self.assertFalse(secret_file.exists())

    def test_bash_proxy_environment_is_available_interactively_and_headless(
        self,
    ) -> None:
        for headless in (False, True):
            with self.subTest(headless=headless), TemporaryDirectory() as tmp:
                project = Path(tmp)
                captured: dict[str, object] = {}

                def run(command, _captured=captured, _project=project, **kwargs) -> int:
                    _captured["command"] = command
                    _captured["env"] = kwargs.get("env")
                    pi_models = json.loads(
                        (
                            _project / ".project-sandbox" / "pi" / "models.json"
                        ).read_text()
                    )
                    pi_settings = json.loads(
                        (
                            _project / ".project-sandbox" / "pi" / "settings.json"
                        ).read_text()
                    )
                    opencode = json.loads(
                        (
                            _project / ".project-sandbox" / "opencode" / "opencode.json"
                        ).read_text()
                    )
                    self.assertEqual(
                        pi_models["providers"]["agent-proxy"]["apiKey"],
                        "gateway-key",
                    )
                    self.assertEqual(pi_settings["defaultModel"], "model")
                    self.assertEqual(
                        opencode["provider"]["agent-proxy"]["options"]["apiKey"],
                        "gateway-key",
                    )
                    self.assertEqual(opencode["model"], "agent-proxy/model")
                    return 0

                args = [
                    "--agent",
                    "bash",
                    "--agent-proxy",
                    "http://127.0.0.1:4000/v1",
                    "--model",
                    "model",
                    "--no-build",
                    "--verbose",
                ]
                if headless:
                    args += ["--prompt-text", "true"]
                args += [str(project), "python:3.12-slim"]

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
                        cli.container_cli, "ensure_system_started", return_value=0
                    ),
                    patch.object(cli.container_cli, "image_exists", return_value=True),
                    patch.object(cli.container_cli, "run", side_effect=run),
                    patch.object(cli.session, "run", side_effect=run),
                    patch.object(
                        cli.ollama_network,
                        "prepare",
                        return_value=cli.ollama_network.ForwardingPlan(
                            "docker-desktop-host-alias"
                        ),
                    ),
                    patch.object(
                        cli.agent_proxy,
                        "resolve_key",
                        return_value=("gateway-key", "environment"),
                    ),
                    patch.object(
                        cli.agent_proxy, "discover_models", return_value=["model"]
                    ),
                ):
                    self.assertEqual(cli.main(args), 0)

                self.assertEqual(
                    captured["env"],
                    {
                        "OPENAI_BASE_URL": ("http://host.docker.internal:4000/v1"),
                        "OPENAI_API_KEY": "gateway-key",
                        "OPENAI_MODEL": "model",
                    },
                )
                command = captured["command"]
                self.assertIsInstance(command, list)
                self.assertNotIn("gateway-key", command)
                for name in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL"):
                    self.assertIn(name, command)
                self.assertIn("target=/project-sandbox-config/pi", " ".join(command))
                self.assertIn(
                    "target=/project-sandbox-config/opencode", " ".join(command)
                )
                self.assertFalse(
                    (project / ".project-sandbox" / "pi" / "models.json").exists()
                )
                self.assertFalse(
                    (
                        project / ".project-sandbox" / "opencode" / "opencode.json"
                    ).exists()
                )

    def test_bash_proxy_renderer_preconfigures_pi_and_opencode(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_agents.render(
                root,
                agent_proxy=(
                    "http://proxy:4000/v1",
                    ["other", "selected"],
                    "gateway-key",
                    "bash",
                ),
                agent_proxy_model="selected",
            )

            pi_models = json.loads((root / "pi" / "models.json").read_text())
            pi_settings = json.loads((root / "pi" / "settings.json").read_text())
            opencode = json.loads((root / "opencode" / "opencode.json").read_text())
            self.assertEqual(
                pi_models["providers"]["agent-proxy"]["apiKey"], "gateway-key"
            )
            self.assertEqual(pi_settings["defaultProvider"], "agent-proxy")
            self.assertEqual(pi_settings["defaultModel"], "selected")
            self.assertEqual(
                opencode["provider"]["agent-proxy"]["options"]["apiKey"],
                "gateway-key",
            )
            self.assertEqual(opencode["model"], "agent-proxy/selected")

    def test_bash_proxy_dry_run_previews_only_redacted_environment_names(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            output = io.StringIO()
            with (
                patch.object(
                    cli,
                    "read_identity",
                    return_value=GitIdentity("Ada", "ada@example.com"),
                ),
                patch.object(cli.agent_proxy, "resolve_key") as resolve_key,
                patch.object(cli.agent_proxy, "discover_models") as discover_models,
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(
                    cli.main(
                        [
                            "--dry-run",
                            "--no-build",
                            "--runtime",
                            "docker",
                            "--agent",
                            "bash",
                            "--agent-proxy",
                            "http://127.0.0.1:4000/v1",
                            "--agent-proxy-key",
                            "raw-secret",
                            "--model",
                            "model",
                            str(project),
                            "python:3.12-slim",
                        ]
                    ),
                    0,
                )
            text = output.getvalue()
            self.assertIn("OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_MODEL", text)
            self.assertNotIn("raw-secret", text)
            resolve_key.assert_not_called()
            discover_models.assert_not_called()

    def test_bash_proxy_apple_env_file_is_private_and_removed(self) -> None:
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            env_file = project / ".project-sandbox" / "api-keys.env"

            def run(command, **_kwargs) -> int:
                self.assertIn("--env-file", command)
                self.assertEqual(
                    Path(command[command.index("--env-file") + 1]).resolve(),
                    env_file.resolve(),
                )
                self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)
                self.assertIn("OPENAI_API_KEY=gateway-key", env_file.read_text())
                self.assertNotIn("gateway-key", command)
                return 0

            with (
                patch.object(
                    cli,
                    "read_identity",
                    return_value=GitIdentity("Ada", "ada@example.com"),
                ),
                patch.object(
                    cli.container_cli,
                    "select_runtime",
                    return_value=cli.container_cli.APPLE_CONTAINER,
                ),
                patch.object(
                    cli.container_cli, "ensure_system_started", return_value=0
                ),
                patch.object(cli.container_cli, "image_exists", return_value=True),
                patch.object(cli.container_cli, "run", side_effect=run),
                patch.object(
                    cli.ollama_network,
                    "prepare",
                    return_value=cli.ollama_network.ForwardingPlan(
                        "apple-configured-host-alias", label="Agent proxy"
                    ),
                ),
                patch.object(
                    cli.agent_proxy,
                    "resolve_key",
                    return_value=("gateway-key", "environment"),
                ),
                patch.object(
                    cli.agent_proxy, "discover_models", return_value=["model"]
                ),
            ):
                self.assertEqual(
                    cli.main(
                        [
                            "--agent",
                            "bash",
                            "--agent-proxy",
                            "http://127.0.0.1:4000/v1",
                            "--model",
                            "model",
                            "--no-build",
                            str(project),
                            "python:3.12-slim",
                        ]
                    ),
                    0,
                )
            self.assertFalse(env_file.exists())

    def test_checker_runs_both_agents_and_requires_both_to_pass(self) -> None:
        checker = _load_checker()
        with (
            patch.object(
                checker.agent_proxy,
                "resolve_key",
                return_value=("secret", "environment"),
            ),
            patch.object(
                checker.agent_proxy, "discover_models", return_value=["gpt-5-mini"]
            ),
            patch.object(checker, "run_agent", side_effect=[True, True]) as run,
        ):
            self.assertEqual(checker.main([]), 0)
            self.assertEqual(
                [call.args[1] for call in run.call_args_list], ["pi", "opencode"]
            )
        with (
            patch.object(
                checker.agent_proxy,
                "resolve_key",
                return_value=("secret", "environment"),
            ),
            patch.object(
                checker.agent_proxy, "discover_models", return_value=["gpt-5-mini"]
            ),
            patch.object(checker, "run_agent", side_effect=[True, False]),
        ):
            self.assertEqual(checker.main([]), 1)

    @patch("subprocess.run")
    def test_checker_propagates_custom_key_environment_name(self, run: Mock) -> None:
        checker = _load_checker()
        run.return_value = Mock(
            returncode=0,
            stdout="CUSTOM_MARKER\n",
            stderr="",
        )
        args = checker.parser().parse_args(["--key-env", "CUSTOM_GATEWAY_KEY"])
        env = {"CUSTOM_GATEWAY_KEY": "secret"}

        self.assertTrue(
            checker.run_agent(
                Path("/tmp/project"),
                "pi",
                "model",
                "CUSTOM_MARKER",
                args,
                env,
            )
        )
        command = run.call_args.args[0]
        self.assertEqual(
            command[command.index("--agent-proxy-key-env") + 1],
            "CUSTOM_GATEWAY_KEY",
        )


if __name__ == "__main__":
    unittest.main()
