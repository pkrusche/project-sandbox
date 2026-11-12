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
import unittest.mock
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from project_sandbox import container_cli
from project_sandbox.cli import build_parser

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "scripts/security_e2e"


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, AUDIT / file)
    assert spec is not None and spec.loader is not None
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
        namespace: dict[str, Any] = {"json": json}
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
        lookups = []

        def runtime_host(runtime, image):
            lookups.append((runtime, image))
            return firewall.fixture_network.FixtureHost("192.168.65.254", "127.0.0.1")

        with patch.object(firewall.fixture_network, "runtime_host", runtime_host):
            self.assertEqual(
                firewall.default_fixture_host("podman"), "host.containers.internal"
            )
            docker_host = firewall.default_fixture_host("docker")
            self.assertEqual(firewall.fixture_ip("docker"), docker_host)
            apple_host = firewall.fixture_ip("apple-container")
        socket.inet_aton(docker_host)
        # The pin test needs an address: the alias address, never the LAN one,
        # looked up once per session in the image sessions already use.
        self.assertEqual(docker_host, "192.168.65.254")
        self.assertEqual(lookups, [("docker", "python:3.12-slim")])
        self.assertEqual(
            apple_host, firewall.local_service_network.APPLE_LOCALHOST_ADDRESS
        )
        self.assertEqual(
            firewall.BASE_IMAGE, harness.Sandbox.argv.__kwdefaults__["base"]
        )


class SharedImageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.shared = harness.SharedImages(container_cli.APPLE_CONTAINER)
        self.sandbox = harness.Sandbox(
            self.directory, container_cli.APPLE_CONTAINER, self.shared
        )
        self.project = self.sandbox.project()

    def parsed(self, argv):
        return build_parser().parse_args(argv[3:])

    def render(self, firewall="iptables -P OUTPUT DROP\n"):
        context = self.project / ".project-sandbox"
        context.mkdir(exist_ok=True)
        (context / "Dockerfile").write_text("FROM python:3.12-slim\n")
        (context / "init-firewall.sh").write_text(firewall)
        return context

    def launch(self, argv, code=0):
        """Run ``argv`` through start/finish with a scripted exit code."""
        process = SimpleNamespace(
            wait=lambda timeout: code, audit_log=self.directory / "audit.log"
        )
        with unittest.mock.patch.object(
            harness.subprocess, "Popen", return_value=process
        ):
            proc = self.sandbox.start(argv)
        return self.sandbox.finish(proc, ok=False)

    def test_launch_keys_follow_the_egress_mode_and_image_options(self):
        cases = (
            ((), harness.DEFAULT_BASE, "default"),
            (
                ("--vcs-access", "hidden", "--branch", "x"),
                harness.DEFAULT_BASE,
                "default",
            ),
            (("--egress", "sidecar"), harness.DEFAULT_BASE, "egress-sidecar"),
            (("--rust-cargo",), harness.DEFAULT_BASE, None),
            (("--extra-domain", "a.example"), harness.DEFAULT_BASE, None),
            (("--dockerfile", "Dockerfile"), None, None),
            ((), "debian:stable-slim", None),
        )
        for extra, base, key in cases:
            with self.subTest(extra=extra, base=base):
                self.assertEqual(harness.shared_key(extra, base), key)

    def test_only_the_first_launch_per_mode_builds_into_the_run_wide_tag(self):
        first = self.sandbox.argv(self.project, build=True)
        self.assertEqual(self.parsed(first).image_tag, self.shared.tag("default"))
        self.assertFalse(self.parsed(first).no_build)
        self.render()
        self.assertEqual(self.launch(first), 0)
        again = self.sandbox.argv(self.project, build=True)
        self.assertTrue(self.parsed(again).no_build)
        # The other mode has not been built yet, so its first launch builds.
        sidecar = self.sandbox.argv(
            self.project, build=True, extra=["--egress", "sidecar"]
        )
        self.assertEqual(
            self.parsed(sidecar).image_tag, self.shared.tag("egress-sidecar")
        )
        self.assertFalse(self.parsed(sidecar).no_build)
        # Direct runtime commands use the shared default image.
        self.assertEqual(self.sandbox.image, self.shared.tag("default"))

    def test_image_changing_launches_keep_the_tests_own_image(self):
        argv = self.sandbox.argv(
            self.project, build=True, extra=["--rust-cargo"], base=None
        )
        self.assertEqual(self.parsed(argv).image_tag, self.sandbox.own_image)
        self.assertFalse(self.parsed(argv).no_build)

    def test_failed_first_build_is_not_recorded(self):
        self.render()
        self.assertEqual(
            self.launch(self.sandbox.argv(self.project, build=True), code=1), 1
        )
        self.assertEqual(self.shared.inputs, {})
        self.assertFalse(
            self.parsed(self.sandbox.argv(self.project, build=True)).no_build
        )

    def test_a_launch_rendering_different_inputs_fails_loudly(self):
        self.render()
        self.launch(self.sandbox.argv(self.project, build=True))
        self.render(firewall="iptables -P OUTPUT ACCEPT\n")
        with self.assertRaisesRegex(AssertionError, "differ from the shared 'default'"):
            self.launch(self.sandbox.argv(self.project))

    def test_unshared_sandboxes_and_session_teardown_keep_their_own_tags(self):
        (self.directory / "plain").mkdir()
        plain = harness.Sandbox(self.directory / "plain", container_cli.DOCKER)
        self.assertEqual(plain.image, plain.own_image)
        self.shared.inputs.update(default="a", **{"egress-sidecar": "b"})
        calls = []
        with unittest.mock.patch.object(
            harness, "run", lambda argv, **_kwargs: calls.append(argv)
        ):
            self.sandbox.close()
            # Per-test teardown never deletes a shared tag.
            self.assertFalse(any(self.shared.prefix in str(c) for c in calls))
            self.shared.close()
        self.assertEqual(
            [c[-1] for c in calls[-2:]],
            [self.shared.tag("default"), self.shared.tag("egress-sidecar")],
        )


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
