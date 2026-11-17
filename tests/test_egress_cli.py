"""project-sandbox-egress: parser, subcommands and exit codes (tasks 7.1-7.3)."""

import contextlib
import io
import json
import os
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from test_egress_sidecar import MUTATING, NOW, FakeRelay, FakeRuntime

from project_sandbox import egress_cli, egress_sidecar
from project_sandbox.container_cli import DOCKER

IMAGE = "project-sandbox-egress-sidecar:0123456789abcdef"


class ParserTests(TestCase):
    """Task 7.1."""

    def test_help_lists_every_subcommand(self) -> None:
        text = egress_cli.build_parser().format_help()
        for name in (
            "up",
            "down",
            "restart",
            "status",
            "reload",
            "allow",
            "log",
        ):
            self.assertIn(f"    {name} ", text)
        self.assertNotIn("forward", text)

    def test_usage_errors_exit_2(self) -> None:
        for argv in (
            ["bogus"],
            [],
            ["allow"],
            ["allow", "bogus"],
            ["forward", "add", "8787"],  # the squid-sidecar line's forwarders
            ["up", "--nope"],
        ):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    egress_cli.main(argv)
                self.assertEqual(ctx.exception.code, 2)

    def test_every_subcommand_takes_runtime(self) -> None:
        for argv in (
            ["up"],
            ["status", "--json"],
            ["allow", "list"],
            ["log", "--denied"],
        ):
            args = egress_cli.build_parser().parse_args(
                [*argv[:1], "--runtime", "docker", *argv[1:]]
            )
            self.assertEqual(args.runtime, "docker")

    def test_runtime_after_a_nested_action(self) -> None:
        parse = egress_cli.build_parser().parse_args
        for argv in (
            ["allow", "add", "pypi.org", "--runtime", "docker"],
            ["allow", "--runtime", "docker", "add", "pypi.org"],
            ["allow", "list", "--runtime", "docker"],
            ["allow", "remove", "--runtime", "docker", "pypi.org"],
        ):
            with self.subTest(argv=argv):
                self.assertEqual(parse(argv).runtime, "docker")
        self.assertEqual(parse(["allow", "list"]).runtime, "auto")

    def test_console_script_is_registered(self) -> None:
        pyproject = (Path(__file__).parents[1] / "pyproject.toml").read_text()
        self.assertIn(
            'project-sandbox-egress = "project_sandbox.egress_cli:main"', pyproject
        )


class CommandTests(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        self.config = self.tmp / "config"
        self.policy_path = self.config / "project-sandbox" / "egress.toml"
        self.policy_path.parent.mkdir(parents=True)
        self.policy_path.write_text('# mine\n[egress]\nallow = ["pypi.org"] # py\n')
        self._env = patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": str(self.config),
                "XDG_STATE_HOME": str(self.tmp / "state"),
            },
        )
        self._env.start()
        self.fake = FakeRuntime(DOCKER)
        self.relay = FakeRelay()
        self.taken: set[int] = set()
        self.lines: list[str] = []

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def context(self, runtime):
        deps = egress_sidecar.Collaborators(
            runner=self.fake,
            relay=self.relay,
            now=lambda: NOW,
            sleep=lambda _s: None,
            readiness_timeout=0.0,
            port_free=lambda port, _address: port not in self.taken,
            port_release_timeout=0.0,
            host_probe=lambda _port, _address: True,
            strategy=lambda _rt: egress_sidecar.Strategy(),
        )
        ctx = egress_cli.Context(
            runtime,
            deps=deps,
            root=self.tmp / "egress",
            out=self.lines.append,
            build_image=lambda _rt: IMAGE,
            installed_image=IMAGE,
        )
        return ctx

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        self.lines.clear()
        err = io.StringIO()
        with (
            patch.object(
                egress_cli.container_cli, "select_runtime", return_value=DOCKER
            ),
            contextlib.redirect_stderr(err),
        ):
            rc = egress_cli.main(list(argv), context=self.context)
        return rc, "\n".join(self.lines), err.getvalue()

    # --- 7.2 ----------------------------------------------------------------

    def test_up_then_already_up(self) -> None:
        rc, out, _ = self.run_cli("up")
        self.assertEqual(rc, 0)
        self.assertIn("Started project-sandbox-egress (docker) on 127.0.0.1:3129", out)
        self.fake.calls.clear()
        rc, out, _ = self.run_cli("up")
        self.assertEqual(rc, 0)
        self.assertIn("already up (docker, 127.0.0.1:3129)", out)
        self.assertFalse(MUTATING & set(self.fake.kinds()))

    def test_up_on_a_taken_port_exits_1_naming_the_key(self) -> None:
        self.taken = {3129}
        rc, _, err = self.run_cli("up")
        self.assertEqual(rc, 1)
        self.assertIn("[egress.sidecar] port", err)
        self.assertNotIn("run", self.fake.kinds())

    def test_up_readiness_timeout_exits_1_with_log_tail(self) -> None:
        self.fake.ready = False
        rc, _, err = self.run_cli("up")
        self.assertEqual(rc, 1)
        self.assertIn("not ready", err)
        self.assertIn("squid log line", err)

    def test_status_issues_no_mutating_argv(self) -> None:
        rc, out, _ = self.run_cli("status")
        self.assertEqual(rc, 0)
        self.assertIn("State: not running", out)
        self.run_cli("up")
        self.fake.calls.clear()
        rc, out, _ = self.run_cli("status")
        self.assertEqual(rc, 0)
        self.assertIn("Endpoint: 127.0.0.1:3129 (host loopback)", out)
        rc, out, _ = self.run_cli("status", "--json")
        self.assertEqual(rc, 0)
        report = json.loads(out)
        self.assertTrue(report["running"])
        self.assertEqual(report["endpoint"], "127.0.0.1:3129")
        self.assertFalse(MUTATING & set(self.fake.kinds()), self.fake.kinds())

    def test_status_reports_reload_and_port_drift(self) -> None:
        self.run_cli("up")
        self.policy_path.write_text(
            '[egress]\nallow = ["pypi.org", "npmjs.org"]\n'
            "[egress.sidecar]\nport = 3140\n"
        )
        rc, out, _ = self.run_cli("status")
        self.assertEqual(rc, 0)
        self.assertIn("allowlist in egress.toml differs", out)
        self.assertIn("project-sandbox-egress reload", out)
        self.assertIn("Drift (restart needed): [egress.sidecar] port", out)

    def test_down_removes_the_sidecar(self) -> None:
        self.run_cli("up")
        rc, out, _ = self.run_cli("down")
        self.assertEqual(rc, 0)
        self.assertIn("have lost egress", out)
        self.assertIsNone(self.fake.container)
        lay = egress_sidecar.layout(DOCKER, self.tmp / "egress")
        self.assertFalse((lay.sidecar / "ca-key.pem").exists())
        rc, out, _ = self.run_cli("down")
        self.assertEqual(rc, 0)
        self.assertIn("No docker egress sidecar was running", out)

    def test_restart_new_ca(self) -> None:
        self.run_cli("up")
        lay = egress_sidecar.layout(DOCKER, self.tmp / "egress")
        first = (lay.sidecar / "ca.pem").read_bytes()
        self.run_cli("restart")
        self.assertEqual((lay.sidecar / "ca.pem").read_bytes(), first)
        rc, out, _ = self.run_cli("restart", "--new-ca")
        self.assertEqual(rc, 0)
        self.assertNotEqual((lay.sidecar / "ca.pem").read_bytes(), first)
        self.assertIn(
            "Restarted project-sandbox-egress (docker) on 127.0.0.1:3129", out
        )

    # --- 7.3 ----------------------------------------------------------------

    def test_allow_add_edits_and_reloads_live(self) -> None:
        self.run_cli("up")
        self.fake.calls.clear()
        rc, out, _ = self.run_cli("allow", "add", "registry.npmjs.org")
        self.assertEqual(rc, 0)
        self.assertIn("Allowed: registry.npmjs.org", out)
        text = self.policy_path.read_text()
        self.assertIn("registry.npmjs.org", text)
        self.assertIn("# mine", text)
        self.assertEqual(
            [c for c in self.fake.calls if self.fake.kind(c) in MUTATING],
            [["docker", "exec", "project-sandbox-egress", "sbx-reload"]],
        )
        lay = egress_sidecar.layout(DOCKER, self.tmp / "egress")
        self.assertIn("registry.npmjs.org", (lay.sidecar / "squid.conf").read_text())

    def test_allow_add_when_not_running_only_edits(self) -> None:
        rc, out, _ = self.run_cli("allow", "add", "example.org")
        self.assertEqual(rc, 0)
        self.assertIn("applies when it next starts", out)
        self.assertNotIn("reload", self.fake.kinds())

    def test_allow_rejections_leave_the_file_unchanged(self) -> None:
        before = self.policy_path.read_bytes()
        for host, message in (
            ("github.com", "GitHub repository access"),
            ("*.githubusercontent.com", "raw.githubusercontent.com"),
            ("10.0.0.1", "IP literals are not allowed"),
        ):
            with self.subTest(host=host):
                rc, _, err = self.run_cli("allow", "add", host)
                self.assertEqual(rc, 1)
                self.assertIn(message, err)
                self.assertEqual(self.policy_path.read_bytes(), before)

    def test_allow_list_marks_providers(self) -> None:
        rc, out, _ = self.run_cli("allow", "list")
        self.assertEqual(rc, 0)
        self.assertIn("pypi.org\n", out + "\n")
        self.assertIn("api.anthropic.com  (agent provider, always allowed)", out)
        self.assertIn(f"# policy: {self.policy_path}", out)

    def test_allow_remove(self) -> None:
        rc, _, _ = self.run_cli("allow", "remove", "pypi.org")
        self.assertEqual(rc, 0)
        self.assertNotIn(
            "pypi.org", self.policy_path.read_text().split("# py")[0].split("allow")[1]
        )
        rc, _, err = self.run_cli("allow", "remove", "api.anthropic.com")
        self.assertEqual(rc, 1)
        self.assertIn("always allowed", err)

    def test_reload_when_not_running_exits_1(self) -> None:
        rc, _, err = self.run_cli("reload")
        self.assertEqual(rc, 1)
        self.assertIn("project-sandbox-egress up", err)

    def test_reload_applies_a_hand_edit(self) -> None:
        self.run_cli("up")
        self.policy_path.write_text('[egress]\nallow = ["pypi.org", "npmjs.org"]\n')
        self.fake.calls.clear()
        rc, out, _ = self.run_cli("reload")
        self.assertEqual(rc, 0)
        self.assertIn("reloaded, no restart", out)
        self.assertEqual(
            [
                self.fake.kind(c)
                for c in self.fake.calls
                if self.fake.kind(c) in MUTATING
            ],
            ["reload"],
        )

    # --- 7.3 log --------------------------------------------------------------

    LOG = (
        "2026/11/06 10:00:00| cache.log line\n"
        "SBX 1000.000 TCP_TUNNEL/200 CONNECT ok.example:443 sni=ok.example "
        "bump=splice err=- cause=- peer=1.2.3.4 client=172.18.0.3\n"
        "SBX 1001.000 TCP_DENIED/200 CONNECT bad.example:443 sni=- bump=bump "
        "err=TCP_RESET cause=host-not-allowed peer=- client=172.18.0.4\n"
        "SBX 2000.000 TCP_DENIED/200 CONNECT late.example:443 sni=- bump=bump "
        "err=TCP_RESET cause=host-not-allowed peer=- client=172.18.0.4"
    )

    def _log(self, *argv: str):
        self.run_cli("up")
        self.fake.log_text = self.LOG
        return self.run_cli("log", *argv)

    def test_log_prints_access_lines_only(self) -> None:
        rc, out, _ = self._log()
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.splitlines()), 3)
        self.assertNotIn("cache.log", out)

    def test_log_denied_since(self) -> None:
        with patch.object(egress_cli.time, "time", return_value=2100.0):
            rc, out, _ = self._log("--denied", "--since", "10m")
        self.assertEqual(rc, 0)
        lines = out.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("late.example  host-not-allowed", lines[0])
        self.assertIn("172.18.0.4", lines[0])
        logs = next(c for c in self.fake.calls if c[1] == "logs")
        self.assertEqual(logs[logs.index("--since") + 1], str(2100 - 600 - 60))

    def test_log_since_iso_and_invalid(self) -> None:
        rc, out, _ = self._log("--denied", "--since", "1970-01-01T00:16:40+00:00")
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.splitlines()), 2)  # 1001 and 2000, not 1000
        rc, _, err = self.run_cli("log", "--since", "yesterday")
        self.assertEqual(rc, 1)
        self.assertIn("--since", err)

    def test_log_follow_streams_through_popen(self) -> None:
        self.run_cli("up")
        seen = {}

        class FakeProcess:
            def __init__(self, argv, **_kw):
                seen["argv"] = argv
                self.stdout = io.StringIO(CommandTests.LOG + "\n")

            def terminate(self):
                seen["terminated"] = True

        ctx_factory = self.context

        def context(runtime):
            ctx = ctx_factory(runtime)
            ctx.popen = FakeProcess
            return ctx

        self.lines.clear()
        with patch.object(
            egress_cli.container_cli, "select_runtime", return_value=DOCKER
        ):
            rc = egress_cli.main(["log", "--follow", "--denied"], context=context)
        self.assertEqual(rc, 0)
        self.assertIn("--follow", seen["argv"])
        self.assertTrue(seen["terminated"])
        self.assertEqual(len(self.lines), 2)


class ServiceCommandTests(TestCase):
    """isolated-sidecar-network task 4.3: `service add|remove|list`, applied live."""

    def setUp(self) -> None:
        from test_egress_sidecar_colima import PRIVATE, RUNTIME, ColimaFake

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        self.config = self.tmp / "config"
        self.policy_path = self.config / "project-sandbox" / "egress.toml"
        self.policy_path.parent.mkdir(parents=True)
        self.policy_path.write_text('# mine\n[egress]\nallow = ["pypi.org"] # py\n')
        self._env = patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": str(self.config),
                "XDG_STATE_HOME": str(self.tmp / "state"),
            },
        )
        self._env.start()
        self.colima = RUNTIME
        self.private = PRIVATE
        self.fake = ColimaFake()
        self.docker = FakeRuntime(DOCKER)
        self.lines: list[str] = []

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def context(self, runtime):
        colima = runtime.name == "colima"
        deps = egress_sidecar.Collaborators(
            runner=self.fake if colima else self.docker,
            relay=FakeRelay(),
            now=lambda: NOW,
            sleep=lambda _s: None,
            readiness_timeout=0.0,
            port_free=lambda _port, _address: True,
            port_release_timeout=0.0,
            host_probe=lambda _port, _address: True,
            strategy=lambda _rt: self.private if colima else egress_sidecar.Strategy(),
            mount_check=lambda _rt, _paths: None,
        )
        return egress_cli.Context(
            runtime,
            deps=deps,
            root=self.tmp / "egress",
            out=self.lines.append,
            build_image=lambda _rt: IMAGE,
            installed_image=IMAGE,
        )

    def run_cli(self, *argv: str, runtime=None) -> tuple[int, str, str]:
        self.lines.clear()
        err = io.StringIO()
        with (
            patch.object(
                egress_cli.container_cli,
                "select_runtime",
                return_value=runtime or self.colima,
            ),
            patch.object(egress_cli.container_cli, "verify_colima_profile"),
            contextlib.redirect_stderr(err),
        ):
            rc = egress_cli.main(list(argv), context=self.context)
        return rc, "\n".join(self.lines), err.getvalue()

    def conf(self) -> str:
        lay = egress_sidecar.layout(self.colima, self.tmp / "egress")
        return (lay.sidecar / egress_sidecar.CONF_NAME).read_text()

    def mutating(self) -> list[list[str]]:
        return [c for c in self.fake.calls if self.fake.kind(c) in MUTATING]

    def test_parser_takes_the_service_actions(self) -> None:
        parse = egress_cli.build_parser().parse_args
        args = parse(["service", "add", "ollama", "11434", "--runtime", "colima"])
        self.assertEqual((args.action, args.name, args.port), ("add", "ollama", 11434))
        self.assertEqual(parse(["service", "remove", "ollama"]).name, "ollama")
        self.assertEqual(parse(["service", "list"]).runtime, "auto")
        self.assertIn("    service ", egress_cli.build_parser().format_help())
        for argv in (
            ["service"],
            ["service", "add", "ollama"],
            ["service", "add", "x", "y"],
        ):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    parse(argv)
                self.assertEqual(ctx.exception.code, 2)

    def test_add_reloads_the_running_sidecar_without_a_restart(self) -> None:
        self.run_cli("up")
        self.fake.calls.clear()
        rc, out, _ = self.run_cli("service", "add", "ollama", "11434")
        self.assertEqual(rc, 0, out)
        self.assertIn("Declared service ollama on port 11434.", out)
        self.assertIn("reloaded, no restart", out)
        self.assertEqual(
            self.mutating(),
            [egress_sidecar.reload_argv(self.colima)],
        )
        self.assertNotIn("create", self.fake.kinds())
        self.assertIn("parent 11434 0 no-query no-digest originserver", self.conf())
        text = self.policy_path.read_text()
        self.assertIn("# mine", text)
        self.assertIn("# py", text)
        self.assertIn("ollama", text)

    def test_remove_reloads_and_warns_about_requests_in_flight(self) -> None:
        self.run_cli("service", "add", "ollama", "11434")
        self.run_cli("up")
        self.fake.calls.clear()
        rc, out, _ = self.run_cli("service", "remove", "ollama")
        self.assertEqual(rc, 0, out)
        self.assertIn("Removed service ollama (port 11434)", out)
        self.assertIn("Requests already in flight may complete", out)
        self.assertEqual(self.mutating(), [egress_sidecar.reload_argv(self.colima)])
        self.assertNotIn("accel", self.conf())

    def test_add_when_not_running_only_edits(self) -> None:
        rc, out, _ = self.run_cli("service", "add", "ollama", "11434")
        self.assertEqual(rc, 0)
        self.assertIn("applies when it next starts", out)
        self.assertEqual(self.mutating(), [])

    def test_repeated_and_moved_declarations(self) -> None:
        self.run_cli("service", "add", "ollama", "11434")
        _, out, _ = self.run_cli("service", "add", "ollama", "11434")
        self.assertIn("already declared on port 11434", out)
        _, out, _ = self.run_cli("service", "add", "ollama", "11435")
        self.assertIn("Moved service ollama from port 11434 to 11435", out)

    def test_an_invalid_edit_exits_1_and_leaves_the_file_byte_identical(self) -> None:
        self.run_cli("service", "add", "ollama", "11434")
        before = self.policy_path.read_bytes()
        for argv, message in (
            (("service", "add", "other", "3128"), "reserved"),
            (("service", "add", "other", "11434"), "unique"),
            (("service", "add", "Bad_Name", "4000"), "Invalid service name"),
            (("service", "add", "low", "80"), "out of range"),
            (("service", "remove", "missing"), "not declared"),
        ):
            with self.subTest(argv=argv):
                rc, _, err = self.run_cli(*argv)
                self.assertEqual(rc, 1)
                self.assertIn(message, err)
                self.assertEqual(self.policy_path.read_bytes(), before)

    def test_list_on_colima_names_the_sidecar_urls(self) -> None:
        self.run_cli("service", "add", "ollama", "11434")
        self.run_cli("service", "add", "agent-proxy", "4000")
        rc, out, _ = self.run_cli("service", "list")
        self.assertEqual(rc, 0)
        self.assertIn("agent-proxy 4000  (http://egress:4000/)", out)
        self.assertIn("ollama 11434  (http://egress:11434/)", out)
        self.assertLess(out.index("agent-proxy"), out.index("ollama"))
        self.assertNotIn("exposed only", out)
        self.assertIn(f"# policy: {self.policy_path}", out)

    def test_list_on_a_runtime_without_services_says_where_they_apply(self) -> None:
        self.run_cli("service", "add", "ollama", "11434")
        rc, out, _ = self.run_cli("service", "list", runtime=DOCKER)
        self.assertEqual(rc, 0)
        self.assertIn("ollama 11434", out)
        self.assertNotIn("egress:11434", out)
        self.assertIn("exposed only in Colima sidecar mode", out)

    def test_an_edit_on_another_runtime_does_not_touch_its_sidecar(self) -> None:
        rc, out, _ = self.run_cli("service", "add", "ollama", "11434", runtime=DOCKER)
        self.assertEqual(rc, 0)
        self.assertIn("exposed only in Colima sidecar mode", out)
        self.assertIn("reload --runtime colima", out)
        self.assertEqual(self.docker.calls, [])

    def test_empty_list(self) -> None:
        rc, out, _ = self.run_cli("service", "list")
        self.assertEqual(rc, 0)
        self.assertIn("No services declared.", out)

    def test_status_reports_services_and_the_internal_address(self) -> None:
        self.run_cli("service", "add", "ollama", "11434")
        self.run_cli("up")
        rc, out, _ = self.run_cli("status")
        self.assertEqual(rc, 0)
        self.assertIn(
            "Networks: psbx-egress-uplink (outbound), psbx-sandbox (internal, as "
            "egress at 172.19.0.2)",
            out,
        )
        self.assertIn("Proxy: egress:3128 on psbx-sandbox (no published port)", out)
        self.assertIn("Services: ollama on egress:11434", out)
        self.assertIn("Mac host address: 192.168.5.2 (host.lima.internal)", out)
        self.assertNotIn("Endpoint:", out)
        rc, out, _ = self.run_cli("status", "--json")
        report = json.loads(out)
        self.assertEqual(report["services"], [{"name": "ollama", "port": 11434}])
        self.assertTrue(report["services_exposed"])
        self.assertEqual(report["internal_address"], "172.19.0.2")
        self.assertEqual(
            report["networks"],
            {"uplink": "psbx-egress-uplink", "sandbox": "psbx-sandbox"},
        )
        self.assertEqual(report["host_address"], "192.168.5.2")

    def test_status_elsewhere_lists_services_as_colima_only(self) -> None:
        self.run_cli("service", "add", "ollama", "11434")
        rc, out, _ = self.run_cli("status", runtime=DOCKER)
        self.assertEqual(rc, 0)
        self.assertIn(
            "Services: ollama (11434); exposed only in Colima sidecar mode", out
        )
        rc, out, _ = self.run_cli("status", "--json", runtime=DOCKER)
        report = json.loads(out)
        self.assertEqual(report["services"], [{"name": "ollama", "port": 11434}])
        self.assertFalse(report["services_exposed"])
        self.assertNotIn("internal_address", report)
