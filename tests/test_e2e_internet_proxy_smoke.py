import contextlib
import importlib.util
import io
import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


def _load_smoke_checker():
    path = Path(__file__).parents[1] / "scripts" / "e2e-internet-proxy-smoke.py"
    spec = importlib.util.spec_from_file_location("e2e_internet_proxy_smoke", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InternetProxySmokeE2ETests(unittest.TestCase):
    def test_missing_listener_prints_note_and_skips_without_side_effects(self) -> None:
        checker = _load_smoke_checker()
        output = io.StringIO()
        with (
            patch.object(
                checker.internet_proxy,
                "preflight",
                side_effect=SystemExit("unavailable"),
            ) as preflight,
            patch.object(checker.tempfile, "mkdtemp") as mkdtemp,
            patch.object(checker, "_agent_proxy_scenario") as gateway,
            patch.object(checker.subprocess, "run") as run,
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(checker.main([]), 0)

        preflight.assert_called_once()
        mkdtemp.assert_not_called()
        gateway.assert_not_called()
        run.assert_not_called()
        self.assertIn("SKIP: Internet proxy is not running", output.getvalue())

    def test_missing_agent_proxy_skips_only_the_combined_scenario(self) -> None:
        checker = _load_smoke_checker()
        args = checker.parser().parse_args([])
        output = io.StringIO()
        with (
            patch.object(
                checker.socket,
                "create_connection",
                side_effect=OSError("refused"),
            ),
            patch.object(checker.agent_proxy, "resolve_key") as resolve_key,
            contextlib.redirect_stdout(output),
        ):
            self.assertIsNone(checker._agent_proxy_scenario(args))

        resolve_key.assert_not_called()
        self.assertIn("running the Internet-proxy-only scenario", output.getvalue())

    def test_missing_gateway_key_skips_only_the_combined_scenario(self) -> None:
        checker = _load_smoke_checker()
        args = checker.parser().parse_args([])
        output = io.StringIO()
        with (
            patch.object(checker.socket, "create_connection"),
            patch.object(
                checker.agent_proxy,
                "resolve_key",
                side_effect=SystemExit("missing key"),
            ),
            patch.object(checker.agent_proxy, "discover_models") as discover_models,
            contextlib.redirect_stdout(output),
        ):
            self.assertIsNone(checker._agent_proxy_scenario(args))

        discover_models.assert_not_called()
        self.assertIn("is unavailable", output.getvalue())

    def test_running_listener_launches_headless_firewall_audit(self) -> None:
        checker = _load_smoke_checker()
        with TemporaryDirectory() as tmp:
            project = Path(tmp) / "internet-proxy-smoke"
            project.mkdir()

            def run(command, **kwargs):
                self.assertIn("--agent", command)
                self.assertEqual(command[command.index("--agent") + 1], "bash")
                self.assertIn("--internet-proxy", command)
                self.assertIn("--image-tag", command)
                self.assertIn("--no-forward-credentials", command)
                self.assertIn("--prompt-text", command)
                self.assertIn("--no-build", command)
                self.assertFalse(kwargs["check"])
                Path(command[-2], "internet-proxy-smoke-result.json").write_text(
                    json.dumps({"ok": True, "failures": []}), encoding="utf-8"
                )
                return subprocess.CompletedProcess(command, 0)

            with (
                patch.object(checker.internet_proxy, "preflight") as preflight,
                patch.object(checker, "_agent_proxy_scenario", return_value=None),
                patch.object(checker.tempfile, "mkdtemp", return_value=str(project)),
                patch.object(checker.subprocess, "run", side_effect=run) as run_mock,
            ):
                self.assertEqual(checker.main(["--no-build"]), 0)

            preflight.assert_called_once()
            run_mock.assert_called_once()
            self.assertFalse(project.exists())

    def test_available_agent_proxy_uses_same_no_credentials_session(self) -> None:
        checker = _load_smoke_checker()
        with TemporaryDirectory() as tmp:
            project = Path(tmp) / "internet-agent-proxy-smoke"
            project.mkdir()

            def run(command, **kwargs):
                self.assertIn("--no-forward-credentials", command)
                self.assertEqual(
                    command[command.index("--internet-proxy") + 1],
                    "http://127.0.0.1:18080",
                )
                self.assertEqual(
                    command[command.index("--agent-proxy") + 1],
                    "http://127.0.0.1:4000/v1",
                )
                self.assertEqual(command[command.index("--model") + 1], "test-model")
                self.assertEqual(kwargs["env"]["TEST_GATEWAY_KEY"], "gateway-secret")
                Path(command[-2], "internet-proxy-smoke-result.json").write_text(
                    json.dumps({"ok": True, "failures": []}), encoding="utf-8"
                )
                return subprocess.CompletedProcess(command, 0)

            with (
                patch.object(checker.internet_proxy, "preflight"),
                patch.object(
                    checker,
                    "_agent_proxy_scenario",
                    return_value=("gateway-secret", "test-model"),
                ),
                patch.object(checker.tempfile, "mkdtemp", return_value=str(project)),
                patch.object(checker.subprocess, "run", side_effect=run),
            ):
                self.assertEqual(
                    checker.main(
                        ["--no-build", "--agent-proxy-key-env", "TEST_GATEWAY_KEY"]
                    ),
                    0,
                )

            self.assertFalse(project.exists())

    def test_audit_checks_proxy_success_and_direct_bypass(self) -> None:
        checker = _load_smoke_checker()
        self.assertIn('"--noproxy", "*"', checker.AUDIT_SOURCE)
        self.assertIn("allowlisted HTTPS failed through", checker.AUDIT_SOURCE)
        self.assertIn("bypassed the sandbox firewall", checker.AUDIT_SOURCE)
        self.assertIn("Agent proxy worked without forwarding", checker.AUDIT_SOURCE)
        self.assertIn("unexpected forwarded credential file", checker.AUDIT_SOURCE)


if __name__ == "__main__":
    unittest.main()
