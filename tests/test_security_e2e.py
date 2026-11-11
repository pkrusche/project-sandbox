"""Validate security audit controls/reporting without a container runtime."""

import ast
import http.client
import importlib.util
import json
import os
import socket
import ssl
import struct
import subprocess
import sys
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


def load_network():
    # test_network.py imports its siblings by their pytest rootdir names.
    saved = {name: sys.modules.get(name) for name in ("conftest", "origin")}
    sys.modules.update(conftest=harness, origin=origin_module)
    try:
        return load("security_e2e_network", "test_network.py")
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


network = load_network()


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

    def _security_pytest_argv(self, *extra):
        """argv the runner hands to `uv run pytest` for the security suite."""
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp)
            calls = bindir / "calls"
            uv = bindir / "uv"
            uv.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >>{calls}\nexit 0\n")
            uv.chmod(0o755)
            env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
            subprocess.run(
                [ROOT / "scripts/run-e2e-tests.sh", "--only", "security"]
                + ["--runtime", "docker", *extra],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )
            lines = calls.read_text().splitlines()
        return next(line for line in lines if "security_e2e" in line)

    def test_known_limitations_and_skips_pass_unless_strict_is_requested(self):
        self.assertNotIn("--strict-coverage", self._security_pytest_argv())
        self.assertIn(
            "--strict-coverage",
            self._security_pytest_argv("--strict-security-coverage"),
        )

    def test_podman_fixture_host_uses_the_host_alias(self):
        from unittest.mock import patch

        # The audit modules import the audit conftest, not tests/conftest.py.
        with patch.dict(sys.modules, {"conftest": harness}):
            firewall = load("security_e2e_firewall", "test_firewall_network.py")
        self.assertEqual(
            firewall.default_fixture_host("podman"), "host.containers.internal"
        )
        docker_host = firewall.default_fixture_host("docker")
        self.assertNotEqual(docker_host, "host.containers.internal")
        socket.inet_aton(docker_host)


class FakeProbeSandbox:
    """Scripted CLI launches: each outcome is (exit code, session log text)."""

    def __init__(self, directory, outcomes):
        self.directory, self.outcomes = directory, list(outcomes)
        self.launches = []

    def project(self):
        path = self.directory / "project"
        path.mkdir()
        return path

    def argv(self, project, prompt, *, build=False, extra=()):
        return ["--no-firewall", *(["--build"] if build else [])]

    def start(self, argv):
        self.launches.append(argv)
        code, text = self.outcomes.pop(0)
        project = self.directory / "project"
        sessions = project / ".project-sandbox/sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / f"bash-{len(self.launches)}.log").write_text(text)
        if code == 0:
            (project / "probes.json").write_text('[{"status": 200}]')
        audit = self.directory / f"session-{len(self.launches)}.log"
        audit.write_text(f"launch {len(self.launches)} exited {code}\n")
        return SimpleNamespace(code=code, audit_log=audit)

    def finish(self, proc, *, ok=True, timeout=600):
        return proc.code


class SidecarProbeRetryTests(unittest.TestCase):
    UNREACHABLE = (
        1,
        (
            "Internet proxy connection failed\nERROR: Internet proxy is unreachable "
            "from inside the sandbox at host.docker.internal:3129.\n"
        ),
    )

    def probe(self, outcomes, *, clock_step=1.0):
        now = [0.0]
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        def monotonic():
            now[0] += clock_step
            return now[0]

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sandbox = FakeProbeSandbox(Path(tmp.name), outcomes)
        sidecar = network.Sidecar(sandbox, origin=None)
        clock = SimpleNamespace(monotonic=monotonic, sleep=sleep)
        original, network.time = network.time, clock
        self.addCleanup(setattr, network, "time", original)
        return sidecar, sandbox, sleeps

    def test_relaunches_until_the_sandbox_reaches_the_sidecar(self):
        sidecar, sandbox, sleeps = self.probe(
            [self.UNREACHABLE, self.UNREACHABLE, (0, "ok\n")]
        )
        self.assertEqual(sidecar.probe([{}]), [{"status": 200}])
        self.assertEqual(len(sandbox.launches), 3)
        # Only the first launch builds; the image exists before the preflight.
        self.assertEqual(
            ["--build" in argv for argv in sandbox.launches], [True, False, False]
        )
        self.assertTrue(all("--no-firewall" not in a for a in sandbox.launches))
        self.assertEqual(sleeps, [network.PROXY_RETRY_INTERVAL] * 2)
        self.assertTrue(sidecar.built)

    def test_other_failures_are_not_retried(self):
        sidecar, sandbox, sleeps = self.probe(
            [(1, "ERROR: Agent proxy is unreachable from inside the sandbox\n")]
        )
        with self.assertRaisesRegex(AssertionError, "launch 1 exited 1"):
            sidecar.probe([{}])
        self.assertEqual((len(sandbox.launches), sleeps), (1, []))

    def test_a_persistently_unreachable_sidecar_fails_after_the_deadline(self):
        outcomes = [self.UNREACHABLE] * 100
        sidecar, sandbox, sleeps = self.probe(outcomes, clock_step=10.0)
        with self.assertRaises(AssertionError):
            sidecar.probe([{}])
        launches = len(sandbox.launches)
        self.assertGreater(launches, 1)
        self.assertLess(launches, 100)
        self.assertEqual(len(sleeps), launches - 1)

    def test_an_earlier_unreachable_log_does_not_excuse_a_new_failure(self):
        sidecar, sandbox, _ = self.probe(
            [self.UNREACHABLE, (1, "Traceback: probe.py crashed\n")]
        )
        with self.assertRaisesRegex(AssertionError, "launch 2 exited 1"):
            sidecar.probe([{}])
        self.assertEqual(len(sandbox.launches), 2)


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
