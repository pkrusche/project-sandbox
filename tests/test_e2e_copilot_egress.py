"""Runtime-free tests for scripts/e2e-copilot-egress.py."""

import contextlib
import importlib.util
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import egress_policy


def _load_script():
    path = Path(__file__).parents[1] / "scripts" / "e2e-copilot-egress.py"
    spec = importlib.util.spec_from_file_location("e2e_copilot_egress", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module here
    spec.loader.exec_module(module)
    return module


SAMPLE_LOG = """\
2026/11/04 08:06:42| Accepting SSL bumped HTTP Socket connections at conn7
FIXTURE 1793784672.008 NONE_NONE/0 - error:transaction-end-before-headers sni=- bump=- err=ERR_CLIENT_GONE cause=-
FIXTURE 1793779624.311 NONE_NONE/200 CONNECT api.githubcopilot.com:443 sni=api.githubcopilot.com bump=bump err=- cause=-
FIXTURE 1793779624.340 TCP_MISS/200 POST https://api.githubcopilot.com/chat/completions sni=api.githubcopilot.com bump=bump err=- cause=-
FIXTURE 1793779624.341 TCP_MISS/200 POST https://api.githubcopilot.com/chat/completions sni=api.githubcopilot.com bump=bump err=- cause=-
FIXTURE 1793779624.350 TCP_MISS/200 GET https://api.github.com/copilot_internal/v2/token sni=api.github.com bump=bump err=- cause=-
FIXTURE 1793779624.360 TCP_MISS/200 GET https://api.github.com/repos/o/r/issues/12345 sni=api.github.com bump=bump err=- cause=-
FIXTURE 1793779625.000 TCP_DENIED/200 CONNECT github.com:443 sni=- bump=bump err=TCP_RESET cause=host-not-allowed
FIXTURE 1793779625.100 TCP_DENIED/403 GET https://api.github.com/repos/octocat/Hello-World sni=api.github.com bump=bump err=ERR_ACCESS_DENIED cause=path-not-allowed
FIXTURE 1793779625.200 NONE_NONE/200 CONNECT api.github.com:443 sni=api.github.com bump=bump err=- cause=-
"""


class ArgumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = _load_script()

    def test_defaults(self) -> None:
        args = self.script.parse_args([])
        self.assertEqual(args.runtime, "auto")
        self.assertEqual(args.phases, ("capture", "verify", "block"))
        self.assertEqual(args.variants, ("paths",))
        self.assertEqual(args.extra_host, [])
        self.assertEqual(args.api_path, [])
        self.assertIsNone(args.report_dir)
        self.assertFalse(hasattr(args, "no_build"))

    def test_all_options_parse(self) -> None:
        args = self.script.parse_args(
            [
                "--runtime",
                "docker",
                "--phases",
                "capture,verify",
                "--variants",
                "paths",
                "--extra-host",
                "*.githubassets.com",
                "--api-path",
                "^/meta$",
                "--report-dir",
                "/tmp/r",
                "--model",
                "gpt-5",
                "--timeout",
                "60",
                "--keep",
            ]
        )
        self.assertEqual(args.phases, ("capture", "verify"))
        self.assertEqual(args.variants, ("paths",))
        self.assertEqual(args.extra_host, ["*.githubassets.com"])
        self.assertEqual(args.api_path, ["^/meta$"])
        self.assertEqual(args.report_dir, Path("/tmp/r"))

    def test_invalid_values_are_rejected(self) -> None:
        for argv in (
            ["--phases", "capture,bogus"],
            ["--variants", "host"],
            ["--extra-host", "1.2.3.4"],
            ["--api-path", "^/a b$"],
            ["--api-path", "(unclosed"],
            ["--phases", "capture,block"],
        ):
            with (
                self.subTest(argv=argv),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                self.script.parse_args(argv)


class ProfileAndConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = _load_script()

    def test_deny_variant_excludes_api_github(self) -> None:
        profile = self.script.copilot_profile("deny")
        self.assertNotIn("api.github.com", profile.hosts)
        self.assertEqual(profile.bump_hosts, ())
        for host in egress_policy.COPILOT_HOSTS:
            self.assertIn(host, profile.hosts)

    def test_paths_variant_bumps_only_api_github(self) -> None:
        profile = self.script.copilot_profile("paths", ["*.example.org"], ["^/meta$"])
        self.assertIn("api.github.com", profile.hosts)
        self.assertIn(".example.org", profile.hosts)
        self.assertEqual(profile.bump_hosts, ("api.github.com",))
        self.assertEqual(
            profile.bump_paths, (*egress_policy.COPILOT_API_PATHS, "^/meta$")
        )

    def test_no_repository_hosts_in_profile(self) -> None:
        for variant in ("deny", "paths"):
            hosts = self.script.copilot_profile(variant).hosts
            for host in (
                "github.com",
                "raw.githubusercontent.com",
                "codeload.github.com",
            ):
                self.assertNotIn(host, hosts)

    def test_capture_conf_intercepts_everything(self) -> None:
        conf = self.script.render_squid_conf("capture")
        self.assertIn("http_access allow all", conf)
        self.assertIn("ssl_bump bump all", conf)
        self.assertNotIn("TCP_RESET", conf)
        self.assertIn("logformat fixture FIXTURE", conf)

    def test_verify_conf_deny_variant(self) -> None:
        conf = self.script.render_squid_conf(
            "verify", self.script.copilot_profile("deny")
        )
        self.assertIn("deny_info TCP_RESET rst", conf)
        self.assertIn("http_access deny CONNECT !allowed rst", conf)
        self.assertIn("ssl_bump splice all", conf)
        self.assertNotIn("ssl_bump bump", conf)
        self.assertNotIn("urlpath_regex", conf)

    def test_verify_conf_paths_variant(self) -> None:
        conf = self.script.render_squid_conf(
            "verify", self.script.copilot_profile("paths")
        )
        self.assertIn("acl bumped dstdomain api.github.com", conf)
        self.assertIn(
            "acl bumped_paths urlpath_regex -i "
            + " ".join(egress_policy.COPILOT_API_PATHS),
            conf,
        )
        # Denials precede the bump/splice decisions; bump is only for api.github.com.
        self.assertLess(
            conf.index("http_access deny bumped"), conf.index("ssl_bump peek")
        )
        self.assertIn("ssl_bump bump bumped", conf)


class LogAndReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = _load_script()
        self.entries = self.script.parse_log(SAMPLE_LOG)

    def test_parse_log_ignores_non_access_lines(self) -> None:
        self.assertEqual(len(self.entries), 8)
        connect = self.entries[0]
        self.assertEqual(
            (connect.method, connect.host, connect.path),
            ("CONNECT", "api.githubcopilot.com", ""),
        )
        reset = self.entries[5]
        self.assertTrue(reset.denied)
        self.assertEqual(reset.cause, "host-not-allowed")

    def test_path_template_collapses_ids(self) -> None:
        self.assertEqual(
            self.script.path_template("/repos/o/r/issues/12345"),
            "/repos/o/r/issues/{id}",
        )
        self.assertEqual(
            self.script.path_template("/x/0123456789abcdef/y"), "/x/{id}/y"
        )
        self.assertEqual(
            self.script.path_template("/s/3f2b8c1e-0a4d-4b6e-9f1a-2c3d4e5f6a7b"),
            "/s/{id}",
        )
        self.assertEqual(
            self.script.path_template("/copilot_internal/v2/token"),
            "/copilot_internal/v2/token",
        )

    def test_summarize_groups_requests(self) -> None:
        rows = self.script.summarize_requests(self.entries)
        chat = [r for r in rows if r["path"] == "/chat/completions"]
        self.assertEqual(chat[0]["count"], 2)
        self.assertNotIn("CONNECT", {r["method"] for r in rows})

    def test_denials_carry_cause(self) -> None:
        causes = {d["cause"] for d in self.script.denials(self.entries)}
        self.assertEqual(causes, {"host-not-allowed", "path-not-allowed"})

    def test_expected_denials(self) -> None:
        update_check = {
            "method": "GET",
            "host": "api.github.com",
            "path": "/repos/github/copilot-cli/releases",
        }
        self.assertTrue(self.script.is_expected_denial(update_check))
        self.assertFalse(
            self.script.is_expected_denial({**update_check, "path": "/repos/o/r"})
        )
        self.assertFalse(
            self.script.is_expected_denial({**update_check, "method": "POST"})
        )
        telemetry = {
            "method": "CONNECT",
            "host": "telemetry.individual.githubcopilot.com",
            "path": "",
        }
        self.assertTrue(self.script.is_expected_denial(telemetry))
        self.assertFalse(
            self.script.is_expected_denial(
                {**telemetry, "host": "api.individual.githubcopilot.com"}
            )
        )

    def test_github_leaks(self) -> None:
        leaks = self.script.github_leaks(self.entries)
        # The allowed token exchange and repo GET on api.github.com are leaks for the
        # block phase; denied requests and bumped CONNECT tunnels are not.
        paths = sorted(leak["path"] for leak in leaks)
        self.assertEqual(
            paths, ["/copilot_internal/v2/token", "/repos/o/r/issues/12345"]
        )

    def test_suite_passed_tolerates_earlier_variant_misses(self) -> None:
        Phase = self.script.PhaseResult
        ok = Phase("capture", "x", ok=True)
        miss = Phase("verify", "deny", ok=False)
        hit = Phase("verify", "paths", ok=True)
        self.assertTrue(self.script.suite_passed([ok, miss, hit]))
        self.assertFalse(self.script.suite_passed([ok, miss]))
        self.assertFalse(
            self.script.suite_passed([Phase("capture", "x", ok=False), hit])
        )
        self.assertFalse(self.script.suite_passed([]))

    def test_markdown_marks_expected_denials(self) -> None:
        Phase = self.script.PhaseResult
        denied = [
            {
                "method": "GET",
                "host": "api.github.com",
                "path": "/repos/github/copilot-cli/releases",
                "cause": "path-not-allowed",
                "count": 1,
            },
            {
                "method": "CONNECT",
                "host": "x.example",
                "path": "",
                "cause": "host-not-allowed",
                "count": 2,
            },
        ]
        text = self.script.render_markdown(
            [Phase("verify", "paths", ok=True, denials=denied)], "paths"
        )
        self.assertIn("| path-not-allowed | 1 | yes |", text)
        self.assertIn("| host-not-allowed | 2 | no |", text)

    def test_markdown_report_renders(self) -> None:
        Phase = self.script.PhaseResult
        result = Phase(
            "capture",
            "allow-all",
            ok=True,
            hosts=self.script.hosts_seen(self.entries),
            requests=self.script.summarize_requests(self.entries),
            extra={"hosts_not_in_profile": ["github.com"]},
        )
        text = self.script.render_markdown([result], "deny")
        self.assertIn("Verified profile: **deny**", text)
        self.assertIn(
            "| POST | api.githubcopilot.com | `/chat/completions` | 2 | 200 |", text
        )
        self.assertIn("hosts_not_in_profile: `github.com`", text)


if __name__ == "__main__":
    unittest.main()
