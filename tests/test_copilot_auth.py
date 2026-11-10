import os
import stat
import subprocess
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from project_sandbox import config_agents, copilot_auth

PAT = "github_pat_11AAAAAAA0abcdefghijklmnopqrstuvwxyz"
OAUTH = "gho_abcdefghijklmnopqrstuvwxyz0123456789"


class FakeRunner:
    """Record argv and answer pass/gh calls from a script of outcomes.

    Each outcome is a CompletedProcess-like (returncode, stdout) tuple or an
    exception instance to raise.
    """

    def __init__(self, *, pass_result=None, gh_result=None):
        self.pass_result = pass_result
        self.gh_result = gh_result
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        assert kwargs.get("timeout"), "every lookup must be bounded by a timeout"
        assert kwargs.get("capture_output") is True
        result = self.pass_result if Path(argv[0]).name == "pass" else self.gh_result
        if result is None:
            raise FileNotFoundError(argv[0])
        if isinstance(result, BaseException):
            raise result
        code, stdout = result
        return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr="")

    def tools(self) -> list[str]:
        return [call[0] for call in self.calls]


class HostValidationTests(TestCase):
    def test_accepts_github_com_and_single_label_tenants(self) -> None:
        for value, expected in (
            ("github.com", "github.com"),
            ("acme.ghe.com", "acme.ghe.com"),
            ("a-1.ghe.com", "a-1.ghe.com"),
            ("  Acme.GHE.com ", "acme.ghe.com"),
            ("GitHub.com", "github.com"),
            ("acme.ghe.com.", "acme.ghe.com"),
        ):
            with self.subTest(value=value):
                self.assertEqual(copilot_auth.validate_host(value), expected)

    def test_rejects_ghes_multi_label_and_foreign_hosts(self) -> None:
        for value in (
            "ghe.corp.example",
            "a.b.ghe.com",
            "ghe.com",
            "-acme.ghe.com",
            "acme-.ghe.com",
            "acme.ghe.com.evil.example",
            "api.github.com",
            "github.com.evil.example",
            "acme.ghe.com/path",
            "acme_x.ghe.com",
            "",
        ):
            with self.subTest(value=value):
                with self.assertRaises(SystemExit) as ctx:
                    copilot_auth.validate_host(value)
                message = str(ctx.exception)
                self.assertIn("github.com", message)
                self.assertIn("<tenant>.ghe.com", message)
                self.assertIn("Enterprise Server is not supported", message)

    def test_default_pass_entry_is_per_host(self) -> None:
        self.assertEqual(
            copilot_auth.default_pass_entry("github.com"), "copilot-github-token"
        )
        self.assertEqual(
            copilot_auth.default_pass_entry("acme.ghe.com"),
            "copilot-github-token/acme.ghe.com",
        )

    def test_tenant_hosts_only_for_tenants(self) -> None:
        self.assertEqual(copilot_auth.tenant_hosts("github.com"), ())
        self.assertEqual(
            copilot_auth.tenant_hosts("acme.ghe.com"),
            (
                "acme.ghe.com",
                "api.acme.ghe.com",
                "copilot-api.acme.ghe.com",
                "copilot-telemetry-service.acme.ghe.com",
            ),
        )

    def test_container_env_sets_gh_host_only_for_tenants(self) -> None:
        self.assertEqual(
            copilot_auth.container_env("github.com"),
            ["COPILOT_HOME=/home/agent/.copilot", "COPILOT_AUTO_UPDATE=false"],
        )
        self.assertIn(
            "COPILOT_GH_HOST=acme.ghe.com", copilot_auth.container_env("acme.ghe.com")
        )


class ResolveTokenTests(TestCase):
    def test_pass_default_entry_wins_and_gh_is_never_called(self) -> None:
        runner = FakeRunner(
            pass_result=(0, PAT + "\nlogin: me\n"), gh_result=(0, OAUTH)
        )
        token, source = copilot_auth.resolve_token("github.com", run=runner)
        self.assertEqual((token, source), (PAT, "pass"))
        self.assertEqual(runner.calls, [["pass", "show", "copilot-github-token"]])

    def test_tenant_uses_tenant_entry_and_hostname(self) -> None:
        runner = FakeRunner(pass_result=(1, ""), gh_result=(0, OAUTH + "\n"))
        token, source = copilot_auth.resolve_token("acme.ghe.com", run=runner)
        self.assertEqual((token, source), (OAUTH, "gh"))
        self.assertEqual(
            runner.calls,
            [
                ["pass", "show", "copilot-github-token/acme.ghe.com"],
                ["gh", "auth", "token", "--hostname", "acme.ghe.com"],
            ],
        )

    def test_default_entry_falls_back_to_gh_quietly(self) -> None:
        for pass_result in (None, (1, ""), (0, ""), (0, "\n")):
            with self.subTest(pass_result=pass_result):
                runner = FakeRunner(pass_result=pass_result, gh_result=(0, OAUTH))
                self.assertEqual(
                    copilot_auth.resolve_token("github.com", run=runner),
                    (OAUTH, "gh"),
                )
                self.assertEqual(runner.tools(), ["pass", "gh"])

    def test_explicit_entry_is_used_verbatim(self) -> None:
        runner = FakeRunner(pass_result=(0, PAT))
        self.assertEqual(
            copilot_auth.resolve_token("github.com", "work/copilot", run=runner),
            (PAT, "pass"),
        )
        self.assertEqual(runner.calls, [["pass", "show", "work/copilot"]])

    def test_explicit_entry_failure_never_falls_back_to_gh(self) -> None:
        for pass_result in (None, (1, ""), (0, "")):
            with self.subTest(pass_result=pass_result):
                runner = FakeRunner(pass_result=pass_result, gh_result=(0, OAUTH))
                with self.assertRaises(SystemExit) as ctx:
                    copilot_auth.resolve_token("github.com", "work/copilot", run=runner)
                self.assertIn("work/copilot", str(ctx.exception))
                self.assertEqual(runner.tools(), ["pass"])

    def test_no_source_names_both_remedies(self) -> None:
        for gh_result in (
            None,
            (1, ""),
            (0, ""),
            subprocess.TimeoutExpired("gh", 15),
        ):
            with self.subTest(gh_result=gh_result):
                runner = FakeRunner(pass_result=(1, ""), gh_result=gh_result)
                with self.assertRaises(SystemExit) as ctx:
                    copilot_auth.resolve_token("acme.ghe.com", run=runner)
                message = str(ctx.exception)
                self.assertIn("pass insert copilot-github-token/acme.ghe.com", message)
                self.assertIn("gh auth login --hostname acme.ghe.com", message)

    def test_pass_timeout_is_an_error_not_a_silent_gh_fallback(self) -> None:
        runner = FakeRunner(
            pass_result=subprocess.TimeoutExpired("pass", 120), gh_result=(0, OAUTH)
        )
        with self.assertRaises(SystemExit) as ctx:
            copilot_auth.resolve_token("github.com", run=runner)
        self.assertIn("Timed out", str(ctx.exception))
        self.assertEqual(runner.tools(), ["pass"])

    def test_classic_pat_rejected_from_either_source(self) -> None:
        classic = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
        for runner in (
            FakeRunner(pass_result=(0, classic)),
            FakeRunner(pass_result=(1, ""), gh_result=(0, classic)),
        ):
            with self.subTest(tools=runner.pass_result):
                with self.assertRaises(SystemExit) as ctx:
                    copilot_auth.resolve_token("github.com", run=runner)
                self.assertIn("classic", str(ctx.exception))
                self.assertNotIn(classic, str(ctx.exception))

    def test_default_runner_is_looked_up_at_call_time(self) -> None:
        runner = FakeRunner(pass_result=(0, PAT))
        with (
            patch.object(
                copilot_auth.host_tools,
                "resolve",
                side_effect=lambda n: f"/usr/bin/{n}",
            ),
            patch.object(copilot_auth.subprocess, "run", side_effect=runner) as run,
        ):
            self.assertEqual(copilot_auth.resolve_token("github.com"), (PAT, "pass"))
        # The default runner is host_tools.run: absolute pass, neutral cwd.
        self.assertEqual(run.call_args.args[0][0], "/usr/bin/pass")
        self.assertEqual(
            run.call_args.kwargs["cwd"], copilot_auth.host_tools.neutral_dir()
        )

    def test_scope_warning_names_entry_and_security_doc(self) -> None:
        warning = copilot_auth.scope_warning("copilot-github-token")
        self.assertTrue(warning.startswith("[W]"))
        self.assertIn("docs/security.md", warning)
        self.assertIn("pass insert copilot-github-token", warning)
        self.assertIn("Copilot Requests", warning)


class StageTokenTests(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        patcher = patch.object(config_agents, "CREDENTIALS_ROOT", self.root / "tmp")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        self.context = self.root / "project" / ".project-sandbox"
        self.context.mkdir(parents=True)

    def test_stage_writes_private_file_and_rewrites_each_launch(self) -> None:
        directory = copilot_auth.stage_token(self.context, PAT)
        token_file = directory / "token"
        self.assertEqual(token_file.read_text(encoding="utf-8"), PAT + "\n")
        self.assertEqual(stat.S_IMODE(token_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(
            directory, config_agents.credentials_dir(self.context, "copilot")
        )
        self.assertFalse(directory.is_relative_to(self.context.parent))

        copilot_auth.stage_token(self.context, OAUTH)
        self.assertEqual(token_file.read_text(encoding="utf-8"), OAUTH + "\n")

    def test_stage_replaces_a_planted_symlink_instead_of_following_it(self) -> None:
        directory = copilot_auth.staged_token_dir(self.context)
        config_agents.ensure_private_dir(directory)
        victim = self.root / "victim"
        victim.write_text("keep\n", encoding="utf-8")
        os.symlink(victim, directory / "token")
        copilot_auth.stage_token(self.context, PAT)
        self.assertEqual(victim.read_text(encoding="utf-8"), "keep\n")
        self.assertFalse((directory / "token").is_symlink())

    def test_remove_is_best_effort(self) -> None:
        copilot_auth.remove_staged_token(self.context)  # nothing staged yet
        directory = copilot_auth.stage_token(self.context, PAT)
        copilot_auth.remove_staged_token(self.context)
        self.assertFalse((directory / "token").exists())
