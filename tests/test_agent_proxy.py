import importlib.util
import io
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from project_sandbox import agent_proxy, config_agents


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

    @patch("project_sandbox.agent_proxy.urllib.request.urlopen")
    def test_discovery_preserves_order_and_deduplicates(self, urlopen: Mock) -> None:
        response = Mock()
        response.__enter__ = Mock(
            return_value=io.StringIO(
                json.dumps({"data": [{"id": "b"}, {"id": "a"}, {"id": "b"}]})
            )
        )
        response.__exit__ = Mock(return_value=False)
        urlopen.return_value = response
        self.assertEqual(
            agent_proxy.discover_models("http://127.0.0.1:4000/v1", "secret"),
            ["b", "a"],
        )
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")

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


if __name__ == "__main__":
    unittest.main()
