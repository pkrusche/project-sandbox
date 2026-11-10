"""Validate security audit controls/reporting without a container runtime."""

import ast
import http.client
import importlib.util
import json
import ssl
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from project_sandbox import container_cli
from project_sandbox.cli import build_parser

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "scripts/security_e2e"


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, AUDIT / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


harness = load("security_e2e_harness", "conftest.py")
origin_module = load("security_e2e_origin", "origin.py")
dns = load("security_e2e_dns", "dns_fixture.py")


class SecurityHarnessTests(unittest.TestCase):
    def test_explicit_cli_arguments_parse_for_each_runtime_and_build_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            for runtime in (
                container_cli.DOCKER,
                container_cli.PODMAN,
                container_cli.APPLE_CONTAINER,
            ):
                directory = Path(tmp) / runtime.name
                directory.mkdir()
                sandbox = harness.Sandbox(directory, runtime)
                project = sandbox.project()
                for build in (False, True):
                    argv = sandbox.argv(project, "printf literal", build=build)
                    parsed = build_parser().parse_args(argv[3:])
                    self.assertEqual(parsed.runtime, runtime.name)
                    self.assertEqual(parsed.project, str(project))
                    self.assertEqual(parsed.no_build, not build)
                    self.assertEqual(parsed.prompt_text, "printf literal")
                    self.assertTrue(parsed.no_forward_credentials)
                self.assertNotEqual(sandbox.env["XDG_STATE_HOME"], str(project))

    def test_strict_coverage_never_turns_skips_or_known_gaps_green(self):
        for stats, expected in (
            ({}, 0),
            ({"passed": [1]}, 0),
            ({"skipped": [1]}, 1),
            ({"xfailed": [1]}, 1),
        ):
            session = SimpleNamespace(
                exitstatus=0,
                config=SimpleNamespace(
                    getoption=lambda name: True,
                    pluginmanager=SimpleNamespace(
                        get_plugin=lambda name, stats=stats: SimpleNamespace(
                            stats=stats
                        )
                    ),
                ),
            )
            harness.pytest_sessionfinish(session, 0)
            self.assertEqual(session.exitstatus, expected)
        session.exitstatus = 2
        stats.clear()
        harness.pytest_sessionfinish(session, 2)
        self.assertEqual(session.exitstatus, 2)

    def test_positive_control_requires_origin_body_not_arbitrary_http_success(self):
        source = ast.parse((AUDIT / "test_network.py").read_text())
        function = next(
            node
            for node in source.body
            if isinstance(node, ast.FunctionDef) and node.name == "reached"
        )
        namespace = {"json": json}
        exec(  # noqa: S102 - execute only the repository-owned grader AST
            compile(ast.Module(body=[function], type_ignores=[]), "<reached>", "exec"),
            namespace,
        )
        reached = namespace["reached"]
        for value in (
            {},
            {"status": 200, "body": "proxy error"},
            {"status": 403, "body": '{"host":"fixture"}'},
            {"error": "timeout"},
        ):
            self.assertFalse(reached(value))
        self.assertTrue(reached({"status": 200, "body": '{"host":"fixture"}'}))

    def test_embedded_container_probes_compile(self):
        for filename, names in (
            ("test_runtime.py", {"HOLD"}),
            ("test_network.py", {"PROBE"}),
            ("test_firewall_network.py", {"PROBE"}),
        ):
            tree = ast.parse((AUDIT / filename).read_text())
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id in names for t in node.targets
                ):
                    compile(ast.literal_eval(node.value), filename, "exec")

    def test_security_runner_is_explicit_and_rejects_no_build(self):
        for argv, status in (
            (["--only", "security", "--runtime", "docker"], "RUN"),
            (["--runtime", "docker"], "SKIP"),
            (["--runtime", "docker", "--with-security"], "RUN"),
            (["--runtime", "docker", "--with-security", "--no-build"], "FAIL"),
            (["--only", "security", "--runtime", "docker", "--no-build"], "FAIL"),
        ):
            result = subprocess.run(
                [ROOT / "scripts/run-e2e-tests.sh", "--plan", *argv],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertRegex(result.stdout, rf"security\s+{status}")


class ControlledOriginTests(unittest.TestCase):
    def test_tls_fronting_and_redirect_are_real_fixture_behaviors(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            origin_module.Origin(Path(tmp)) as origin,
        ):
            context = ssl.create_default_context(cafile=str(origin.ca))
            connection = http.client.HTTPSConnection(
                "127.0.0.1", origin.port, context=context, timeout=5
            )
            connection.set_tunnel("allowed.example.test", 443)
            try:
                connection.request(
                    "GET", "/fronted", headers={"Host": "blocked.example.test"}
                )
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(
                    json.loads(response.read())["host"], "blocked.example.test"
                )
                connection.request("GET", "/copilot_internal/redirect")
                response = connection.getresponse()
                self.assertEqual(response.status, 302)
                self.assertEqual(
                    response.getheader("Location"),
                    "https://blocked.example.test/stolen",
                )
                response.read()
                self.assertEqual(len(origin.requests), 2)
            finally:
                connection.close()

    def query(self, name, kind=1):
        question = (
            b"".join(bytes([len(label)]) + label.encode() for label in name.split("."))
            + b"\0"
            + struct.pack("!HH", kind, 1)
        )
        return struct.pack("!HHHHHH", 123, 0x100, 1, 0, 0, 0) + question

    def test_dns_mixed_answers_and_rebinding_are_distinct_with_zero_ttl(self):
        query = self.query("mixed.example.test")
        mixed = dns.answer(query, "public")
        self.assertEqual(struct.unpack("!H", mixed[6:8])[0], 2)
        self.assertEqual(
            mixed[len(query) : len(query) + 12], b"\xc0\x0c\0\1\0\1\0\0\0\0\0\4"
        )
        self.assertIn(b"\x7f\0\0\1", mixed)
        query = self.query("rebind.example.test")
        self.assertNotEqual(dns.answer(query, "public"), dns.answer(query, "private"))
        self.assertEqual(
            dns.answer(self.query("unknown.example.test"), "public")[6:8], b"\0\0"
        )
