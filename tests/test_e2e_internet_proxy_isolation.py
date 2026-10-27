import errno
import importlib.util
import io
import json
import os
import socket
import subprocess
import tempfile
import urllib.error
from contextlib import chdir, redirect_stderr, redirect_stdout
from email.message import Message
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, call, patch

ROOT = Path(__file__).parents[1]


def _load_checker():
    path = Path(__file__).parents[1] / "scripts/e2e-internet-proxy-isolation.py"
    spec = importlib.util.spec_from_file_location("e2e_internet_proxy_isolation", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InternetProxyEndToEndScriptTests(TestCase):
    def test_blocked_url_defaults_to_example_com(self) -> None:
        args = (
            _load_checker()
            .parser()
            .parse_args(
                [
                    "--runtime",
                    "docker",
                    "--internet-proxy-dir",
                    "/unused",
                    "--agentgateway-dir",
                    "/unused",
                ]
            )
        )
        self.assertEqual(args.blocked_url, "https://example.com")

    def test_runner_forwards_the_default_blocked_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_uv = root / "uv"
            # The runner's startup orphan reap also goes through uv; ignore it.
            fake_uv.write_text(
                "#!/bin/sh\n"
                'case "$*" in *e2e-reap-orphans*) exit 0 ;; esac\n'
                'printf "%s\\n" "$@" > "$AUDIT_ARGUMENT_LOG"\n'
            )
            fake_uv.chmod(0o755)
            arguments = root / "arguments"
            result = subprocess.run(
                [
                    "bash",
                    str(ROOT / "scripts/run-e2e-tests.sh"),
                    "--only",
                    "internet-proxy-isolation",
                    "--runtime",
                    "docker",
                    "--internet-proxy-dir",
                    str(root),
                    "--agentgateway-dir",
                    str(root),
                ],
                env={
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "AUDIT_ARGUMENT_LOG": str(arguments),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            forwarded = arguments.read_text().splitlines()
        index = forwarded.index("--blocked-url")
        self.assertEqual(forwarded[index + 1], "https://example.com")

    def test_every_audit_failure_changes_the_result(self) -> None:
        checker = _load_checker()
        args = checker.parser().parse_args(
            [
                "--runtime",
                "docker",
                "--blocked-url",
                "https://blocked.example.test/",
                "--internet-proxy-dir",
                "/unused",
                "--agentgateway-dir",
                "/unused",
                "--timeout",
                "0",
            ]
        )
        source = checker.render_audit(args, "http://gateway.test:4000/v1")
        # Independent low-level probe outcomes, in their expected call order.
        curl_names = [
            "allow",
            "denial",
            "noproxy",
            "unset",
            "proxy_down",
            "proxy_restart",
            "gateway_down_internet",
        ]
        socket_names = ["tcp", "udp", "dns"]
        gateway_names = [
            "gateway",
            "proxy_down_ai",
            "proxy_restart_ai",
            "gateway_down_ai",
        ]
        markers = ["proxy-stopped.go", "proxy-restarted.go", "gateway-stopped.go"]
        failures = {
            "allow": "allowlisted HTTPS succeeds through the Internet proxy",
            "denial": "blocked destination returns a recognizable proxy-policy denial",
            "denial_unrecognized": "blocked destination returns a recognizable proxy-policy denial",
            "noproxy": "curl --noproxy cannot bypass the sandbox firewall",
            "unset": "Internet fails with every proxy variable unset",
            "tcp": "raw public TCP/443 is blocked",
            "udp": "raw public UDP is blocked",
            "dns": "direct DNS is blocked",
            "gateway": "AI completion succeeds through Agentgateway",
            "proxy_down": "proxy loss fails closed without direct fallback",
            "proxy_down_ai": "AI still works while the Internet proxy is stopped",
            "proxy_restart": "proxy restart restores service at the stable endpoint",
            "proxy_restart_ai": "Agentgateway remains independent after proxy restart",
            "gateway_down_internet": "ordinary Internet still works while Agentgateway is stopped",
            "gateway_down_ai": "AI fails when Agentgateway is stopped",
            **{marker: "host signalled " + marker for marker in markers},
        }
        for failed in (None, *failures):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as tmp:
                project = Path(tmp)
                for marker in markers:
                    if marker != failed:
                        (project / marker).touch()
                curl_results = []
                for name in curl_names:
                    rc = (
                        22
                        if name in ("denial", "noproxy", "unset", "proxy_down")
                        else 0
                    )
                    if name == failed:
                        rc = 0 if rc else 1
                    body = "Forbidden by policy" if name == "denial" else ""
                    if failed == "denial_unrecognized" and name == "denial":
                        body = "Connection refused"
                    curl_results.append(
                        MagicMock(returncode=rc, stdout=body, stderr="")
                    )
                sockets = []
                for name in socket_names:
                    sock = MagicMock()
                    sock.connect.side_effect = (
                        None if name == failed else OSError("blocked")
                    )
                    sockets.append(sock)
                gateways = []
                for name in gateway_names:
                    success = name != "gateway_down_ai"
                    if name == failed:
                        success = not success
                    response = MagicMock()
                    response.__enter__.return_value.status = 200
                    response.__enter__.return_value.read.return_value = json.dumps(
                        {
                            "object": "response",
                            "status": "completed",
                            "output": [],
                        }
                    ).encode()
                    gateways.append(response if success else ConnectionRefusedError())
                proxy_env = {
                    name: "http://proxy:18080"
                    for name in (
                        "HTTP_PROXY",
                        "HTTPS_PROXY",
                        "http_proxy",
                        "https_proxy",
                        "ALL_PROXY",
                        "all_proxy",
                    )
                }
                with (
                    chdir(project),
                    patch.dict(
                        os.environ, {**proxy_env, "OPENAI_API_KEY": "test-secret"}
                    ),
                    patch("subprocess.run", side_effect=curl_results) as run,
                    patch("socket.socket", side_effect=sockets) as socket_factory,
                    patch("urllib.request.urlopen", side_effect=gateways) as urlopen,
                    redirect_stdout(io.StringIO()),
                    self.assertRaises(SystemExit) as raised,
                ):
                    exec(compile(source, "audit.py", "exec"), {})  # noqa: S102 — execute generated fixture  # noqa: S102
                result = json.loads((project / "result.json").read_text())
                self.assertEqual(raised.exception.code, 0 if failed is None else 1)
                self.assertEqual(result["ok"], failed is None)
                self.assertEqual(
                    result["failures"], [] if failed is None else [failures[failed]]
                )
                self.assertEqual(run.call_count, len(curl_names))
                self.assertEqual(urlopen.call_count, len(gateway_names))
                self.assertEqual(
                    run.call_args_list[2].args[0][-3:],
                    ["--noproxy", "*", args.allowed_url],
                )
                unset_env = run.call_args_list[3].kwargs["env"]
                self.assertTrue(all(name not in unset_env for name in proxy_env))
                self.assertEqual(
                    socket_factory.call_args_list,
                    [
                        call(socket.AF_INET, socket.SOCK_STREAM),
                        call(socket.AF_INET, socket.SOCK_DGRAM),
                        call(socket.AF_INET, socket.SOCK_STREAM),
                    ],
                )
                for sock, address in zip(
                    sockets,
                    [(args.public_ip, 443), (args.dns_ip, 53), (args.dns_ip, 53)],
                ):
                    sock.connect.assert_called_once_with(address)
                    sock.close.assert_called_once()
                for marker in (
                    "baseline.ready",
                    "proxy-stopped.done",
                    "proxy-restarted.done",
                    "gateway-stopped.done",
                ):
                    self.assertTrue((project / marker).exists())

    def test_host_orchestration_controls_services_and_propagates_results(self) -> None:
        for returncode, result, proxy_was_up in (
            (0, {"ok": True}, True),
            (1, {"ok": True}, True),
            (0, {"ok": False, "failures": ["probe failed"]}, True),
            (0, None, True),
            (0, {"ok": True}, False),
            (1, {"ok": True}, False),
        ):
            with (
                self.subTest(
                    returncode=returncode, result=result, proxy_was_up=proxy_was_up
                ),
                tempfile.TemporaryDirectory() as tmp,
            ):
                checker = _load_checker()
                root = Path(tmp)
                project = root / "project"
                project.mkdir()
                if result is not None:
                    (project / "result.json").write_text(json.dumps(result))
                process = MagicMock()
                process.wait.return_value = returncode
                process.poll.return_value = returncode
                with (
                    patch.object(
                        checker, "__file__", str(root / "scripts" / "checker.py")
                    ),
                    patch.object(
                        checker.tempfile, "mkdtemp", return_value=str(project)
                    ),
                    patch.object(checker.internet_proxy, "preflight") as preflight,
                    patch.object(checker, "listener_up", return_value=proxy_was_up),
                    patch.object(
                        checker.dockerfile, "read_ca_certificates", return_value=()
                    ),
                    patch.object(
                        checker.agent_proxy,
                        "resolve_key",
                        return_value=("test-key", "test"),
                    ),
                    patch.object(
                        checker.subprocess, "Popen", return_value=process
                    ) as popen,
                    patch.object(checker.subprocess, "run") as run,
                    patch.object(checker, "wait_for") as wait_for,
                    redirect_stdout(io.StringIO()),
                    redirect_stderr(io.StringIO()),
                ):
                    rc = checker.main(
                        [
                            "--runtime",
                            "docker",
                            "--blocked-url",
                            "https://blocked.example.test/",
                            "--internet-proxy-dir",
                            str(root),
                            "--agentgateway-dir",
                            str(root),
                            "--ca-cert",
                            "/test/root.pem",
                            "--ca-cert",
                            "/test/intermediate.pem",
                        ]
                    )
                self.assertEqual(
                    rc, 0 if returncode == 0 and result and result["ok"] else 1
                )
                phases = [
                    call(["uv", "run", "ipl", "down"], cwd=root, check=True),
                    call(["uv", "run", "ipl", "restart"], cwd=root, check=True),
                    call(["./run.py", "down"], cwd=root, check=True),
                    call(["./run.py", "restart"], cwd=root, check=True),
                ]
                if not proxy_was_up:
                    # Started before the preflight, stopped again at the end.
                    phases = [
                        call(["uv", "run", "ipl", "setup"], cwd=root, check=True),
                        call(["uv", "run", "ipl", "up"], cwd=root, check=True),
                        *phases,
                        call(["uv", "run", "ipl", "down"], cwd=root, check=True),
                    ]
                self.assertEqual(run.call_args_list, phases)
                self.assertTrue(preflight.called)
                self.assertEqual(
                    [c.args[0].name for c in wait_for.call_args_list],
                    [
                        "baseline.ready",
                        "proxy-stopped.done",
                        "proxy-restarted.done",
                        "gateway-stopped.done",
                    ],
                )
                command = popen.call_args.args[0]
                ca_index = command.index("--ca-cert")
                self.assertEqual(
                    command[ca_index : ca_index + 4],
                    [
                        "--ca-cert",
                        "/test/root.pem",
                        "--ca-cert",
                        "/test/intermediate.pem",
                    ],
                )
                self.assertFalse(project.exists())

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
            Message(),
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


class RemoteIsolationAuditTests(TestCase):
    def test_both_stop_orders_and_cross_endpoint_failures(self):
        for order in ("proxy-first", "gateway-first"):
            for cross_error in (
                errno.EHOSTUNREACH,
                errno.ECONNREFUSED,
                errno.ETIMEDOUT,
                None,
            ):
                with (
                    self.subTest(order=order, cross_error=cross_error),
                    tempfile.TemporaryDirectory() as tmp,
                ):
                    checker = _load_checker()
                    args = checker.parser().parse_args(
                        [
                            "--runtime",
                            "docker",
                            "--blocked-url",
                            "https://blocked.test",
                            "--internet-proxy-dir",
                            "/unused",
                            "--agentgateway-dir",
                            "/unused",
                            "--internet-proxy",
                            "https://proxy.test:8443",
                            "--gateway",
                            "https://gateway.test/v1",
                            "--stop-order",
                            order,
                        ]
                    )
                    source = checker.render_audit(args, "https://gateway.test:443/v1")
                    root = Path(tmp)
                    for phase in checker.phases(args):
                        (root / (phase + ".go")).touch()
                    curl_codes = [0, 22, 1, 1] + (
                        [1, 0, 0] if order == "proxy-first" else [0, 0, 1]
                    )
                    curl_replies = [
                        MagicMock(returncode=code, stdout="denied", stderr="")
                        for code in curl_codes
                    ]
                    successes = (
                        [True, True, True, False]
                        if order == "proxy-first"
                        else [True, False, True, True]
                    )
                    replies = []
                    for success in successes:
                        response = MagicMock()
                        response.__enter__.return_value.status = 200
                        response.__enter__.return_value.read.return_value = json.dumps(
                            {"object": "response", "status": "completed", "output": []}
                        ).encode()
                        replies.append(
                            response if success else ConnectionRefusedError()
                        )
                    sockets = []
                    for index in range(5):
                        sock = MagicMock()
                        sock.__enter__.return_value = sock
                        error = cross_error if index >= 3 else errno.EHOSTUNREACH
                        sock.connect.side_effect = (
                            OSError(error, "fixture") if error else None
                        )
                        sockets.append(sock)

                    def lookup(host, port, **kwargs):
                        return [
                            (
                                socket.AF_INET,
                                socket.SOCK_STREAM,
                                0,
                                "",
                                (
                                    "10.0.0.1" if host == "proxy.test" else "10.0.0.2",
                                    port,
                                ),
                            )
                        ]

                    with (
                        chdir(root),
                        patch.dict(os.environ, {"OPENAI_API_KEY": "secret"}),
                        patch("subprocess.run", side_effect=curl_replies),
                        patch("urllib.request.urlopen", side_effect=replies),
                        patch("socket.socket", side_effect=sockets),
                        patch("socket.getaddrinfo", side_effect=lookup),
                        redirect_stdout(io.StringIO()),
                        self.assertRaises(SystemExit) as result,
                    ):
                        exec(compile(source, "audit.py", "exec"), {})  # noqa: S102 — execute generated fixture
                    self.assertEqual(
                        result.exception.code, int(cross_error != errno.EHOSTUNREACH)
                    )
                    sockets[3].connect.assert_called_once_with(("10.0.0.1", 443))
                    sockets[4].connect.assert_called_once_with(("10.0.0.2", 8443))
                    for phase in checker.phases(args):
                        self.assertTrue((root / (phase + ".done")).exists())
