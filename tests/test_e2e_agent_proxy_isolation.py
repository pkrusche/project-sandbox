import importlib.util
import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


def _load_isolation_checker():
    path = Path(__file__).parents[1] / "scripts" / "e2e-agent-proxy-isolation.py"
    spec = importlib.util.spec_from_file_location("e2e_agent_proxy_isolation", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AgentProxyIsolationE2ETests(unittest.TestCase):
    def test_runs_headless_bash_audit_without_putting_key_in_argv(self) -> None:
        checker = _load_isolation_checker()
        with TemporaryDirectory() as tmp:
            project = Path(tmp) / "audit-project"
            project.mkdir()

            def run(command, **kwargs):
                self.assertIn("--agent", command)
                self.assertEqual(command[command.index("--agent") + 1], "bash")
                self.assertIn("--agent-proxy", command)
                self.assertIn("--prompt-text", command)
                self.assertNotIn("gateway-secret", command)
                self.assertEqual(kwargs["env"]["TEST_GATEWAY_KEY"], "gateway-secret")
                Path(command[-2], "proxy-isolation-result.json").write_text(
                    json.dumps({"ok": True, "failures": []}), encoding="utf-8"
                )
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                patch.object(
                    checker.agent_proxy,
                    "resolve_key",
                    return_value=("gateway-secret", "environment"),
                ),
                patch.object(
                    checker.agent_proxy,
                    "discover_models",
                    return_value=["test-model"],
                ),
                patch.object(checker.tempfile, "mkdtemp", return_value=str(project)),
                patch.object(checker.subprocess, "run", side_effect=run) as run_mock,
            ):
                self.assertEqual(
                    checker.main(
                        [
                            "--runtime",
                            "docker",
                            "--model",
                            "test-model",
                            "--key-env",
                            "TEST_GATEWAY_KEY",
                            "--no-build",
                        ]
                    ),
                    0,
                )

            run_mock.assert_called_once()
            self.assertFalse(project.exists())

    def test_external_http_response_is_treated_as_connectivity(self) -> None:
        checker = _load_isolation_checker()
        http_error_handler = checker.AUDIT_SOURCE.index(
            "except urllib.error.HTTPError as exc:"
        )
        blocked_handler = checker.AUDIT_SOURCE.index(
            "except (urllib.error.URLError, TimeoutError, OSError):"
        )
        self.assertLess(http_error_handler, blocked_handler)
        self.assertIn("unexpected external connectivity", checker.AUDIT_SOURCE)


if __name__ == "__main__":
    unittest.main()
