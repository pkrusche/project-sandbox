import importlib.util
import io
import json
import os
import re
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch


def _load_checker():
    path = Path(__file__).parents[1] / "scripts/e2e-internet-proxy-isolation.py"
    spec = importlib.util.spec_from_file_location("e2e_internet_proxy_isolation", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InternetProxyEndToEndScriptTests(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (
            Path(__file__).parents[1] / "scripts/e2e-internet-proxy-isolation.py"
        ).read_text(encoding="utf-8")

    def test_covers_every_bypass_path(self) -> None:
        for evidence in (
            '"--noproxy", "*"',
            '"HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"',
            "socket.SOCK_STREAM, 443",
            "socket.SOCK_DGRAM, 53",
            '"direct DNS is blocked"',
        ):
            with self.subTest(evidence=evidence):
                self.assertIn(evidence, self.source)

    def test_covers_policy_separation_and_failure_phases(self) -> None:
        for evidence in (
            "recognizable proxy-policy denial",
            "AI completion succeeds through Agentgateway",
            "proxy loss fails closed without direct fallback",
            "ordinary Internet still works while Agentgateway is stopped",
            "for ca_cert in args.ca_cert",
            '["--ca-cert", ca_cert]',
        ):
            with self.subTest(evidence=evidence):
                self.assertIn(evidence, self.source)

    def test_service_control_actions_match_the_service_clis(self) -> None:
        # Both controlled services take up/down/restart and neither has a
        # "stop": `ipl` (internet-proxy-locally) and agentgateway-locally's
        # run.py. Comparing the full set also fails if a retired verb returns.
        actions = set(re.findall(r'control\(\s*args\.(\w+),\s*"(\w+)"', self.source))
        self.assertEqual(
            actions,
            {
                ("internet_proxy_control", "down"),
                ("internet_proxy_control", "restart"),
                ("agentgateway_control", "down"),
                ("agentgateway_control", "restart"),
            },
        )

    def test_uses_current_service_control_commands(self) -> None:
        args = (
            _load_checker()
            .parser()
            .parse_args(
                [
                    "--runtime",
                    "docker",
                    "--blocked-url",
                    "https://blocked.example.test/",
                    "--internet-proxy-dir",
                    "/tmp/internet-proxy-locally",
                    "--agentgateway-dir",
                    "/tmp/agentgateway-locally",
                    "--ca-cert",
                    "/tmp/proxy-ca.pem",
                    "--ca-cert",
                    "/tmp/proxy-intermediate.pem",
                ]
            )
        )

        self.assertEqual(args.internet_proxy_control, "uv run ipl {action}")
        self.assertEqual(args.agentgateway_control, "./run.py {action}")
        self.assertEqual(
            args.ca_cert,
            ["/tmp/proxy-ca.pem", "/tmp/proxy-intermediate.pem"],
        )


class GatewayCompletionProbeTests(TestCase):
    def setUp(self) -> None:
        checker = _load_checker()
        args = checker.parser().parse_args(
            [
                "--runtime",
                "docker",
                "--blocked-url",
                "https://blocked.example.test/",
                "--internet-proxy-dir",
                "/tmp/internet-proxy",
                "--agentgateway-dir",
                "/tmp/agentgateway",
                "--model",
                "gpt-5-mini",
            ]
        )
        source = checker.render_audit(args, "http://gateway.test:4000/v1")
        compile(source, "audit.py", "exec")
        # Execute the actual generated helpers without starting the audit phases.
        self.namespace = {}
        exec(source.split("check(internet_works(),", 1)[0], self.namespace)  # noqa: S102
        self.env = patch.dict(os.environ, {"OPENAI_API_KEY": "test-gateway-secret"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.output = io.StringIO()

    def probe(self, *, response=None, error=None, expect_success=True):
        with patch("urllib.request.urlopen", side_effect=error) as urlopen:
            if response is not None:
                reply = MagicMock(status=200)
                reply.read.return_value = json.dumps(response).encode()
                urlopen.return_value.__enter__.return_value = reply
            with redirect_stdout(self.output):
                result = self.namespace["gateway_request"](expect_success)
        return result, urlopen

    def test_sends_reasoning_compatible_bounded_completion_request(self) -> None:
        result, urlopen = self.probe(
            response={"object": "response", "status": "completed", "output": []}
        )
        self.assertTrue(result)
        request = urlopen.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, "http://gateway.test:4000/v1/responses")
        self.assertEqual(
            request.get_header("Authorization"), "Bearer test-gateway-secret"
        )
        self.assertEqual(body["model"], "gpt-5-mini")
        self.assertEqual(body["max_output_tokens"], 16)
        self.assertEqual(body["input"], "Reply with only OK")
        self.assertNotIn("messages", body)
        self.assertNotIn("max_completion_tokens", body)
        self.assertNotIn("max_tokens", body)
        self.assertEqual(self.output.getvalue(), "")

    def test_http_rejection_reports_status_and_redacted_body(self) -> None:
        error = urllib.error.HTTPError(
            "http://gateway.test",
            400,
            "Bad Request",
            {},
            io.BytesIO(
                b'{"error": "Unsupported parameter max_tokens test-gateway-secret"}'
            ),
        )
        result, _ = self.probe(error=error)
        self.assertFalse(result)
        self.assertIn("HTTP 400", self.output.getvalue())
        self.assertIn("Unsupported parameter max_tokens", self.output.getvalue())
        self.assertIn("[REDACTED]", self.output.getvalue())
        self.assertNotIn("test-gateway-secret", self.output.getvalue())
        self.assertEqual(len(self.namespace["gateway_diagnostics"]), 1)
        self.assertIn(self.namespace["gateway_diagnostics"][0], self.output.getvalue())

    def test_network_failure_reports_reason(self) -> None:
        result, _ = self.probe(error=urllib.error.URLError("Connection refused"))
        self.assertFalse(result)
        self.assertIn("URLError", self.output.getvalue())
        self.assertIn("Connection refused", self.output.getvalue())

    def test_invalid_json_reports_parse_failure(self) -> None:
        result, _ = self.probe(error=json.JSONDecodeError("Invalid JSON", "", 0))
        self.assertFalse(result)
        self.assertIn("JSONDecodeError", self.output.getvalue())

    def test_chat_completions_payload_fails_with_diagnostic(self) -> None:
        result, _ = self.probe(response={"choices": []})
        self.assertFalse(result)
        self.assertIn("unsuccessful Responses payload", self.output.getvalue())

    def test_expected_gateway_outage_passes_without_failure_diagnostic(self) -> None:
        result, _ = self.probe(error=ConnectionRefusedError(), expect_success=False)
        self.assertTrue(result)
        self.assertEqual(self.namespace["gateway_diagnostics"], [])
        self.assertEqual(self.output.getvalue(), "")

    def test_token_budget_exhaustion_still_proves_routing(self) -> None:
        result, _ = self.probe(
            response={
                "object": "response",
                "status": "incomplete",
                "output": [],
                "incomplete_details": {"reason": "max_output_tokens"},
            }
        )
        self.assertTrue(result)
        self.assertEqual(self.output.getvalue(), "")

    def test_unsuccessful_response_states_fail(self) -> None:
        for fields in (
            {"status": "failed", "error": {"message": "upstream error"}},
            {"status": "queued"},
            {"status": "in_progress"},
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "content_filter"},
            },
            {"status": "incomplete", "incomplete_details": None},
            {"status": "completed", "error": {"message": "upstream error"}},
            {"status": "completed", "output": None},
        ):
            with self.subTest(fields=fields):
                result, _ = self.probe(
                    response={"object": "response", "output": [], **fields}
                )
                self.assertFalse(result)

    def test_live_gateway_during_outage_phase_fails_with_diagnostic(self) -> None:
        result, _ = self.probe(
            response={"object": "response", "status": "completed", "output": []},
            expect_success=False,
        )
        self.assertFalse(result)
        self.assertIn("should be stopped", self.output.getvalue())
