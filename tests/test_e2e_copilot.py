import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TOKEN = "github_pat_11E2ESECRET0value"


def _load_script():
    path = Path(__file__).parents[1] / "scripts" / "e2e-copilot.py"
    spec = importlib.util.spec_from_file_location("e2e_copilot", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve their module through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class E2ECopilotArgumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = _load_script()

    def test_defaults(self) -> None:
        args = self.script.parse_args([])
        self.assertEqual(args.runtime, "auto")
        self.assertEqual(args.base_image, "python:3.12-slim")
        self.assertIsNone(args.copilot_host)
        self.assertIsNone(args.capture_dir)
        self.assertEqual(args.output_tail, 40)
        self.assertFalse(args.no_build)

    def test_all_options_parse(self) -> None:
        args = self.script.parse_args(
            [
                "--runtime",
                "podman",
                "--base-image",
                "debian:bookworm",
                "--copilot-host",
                "Acme.GHE.com",
                "--capture-dir",
                "/tmp/caps",
                "--output-tail",
                "5",
                "--check",
                "tenant",
                "--check",
                "headless-ok",
            ]
        )
        self.assertEqual(args.runtime, "podman")
        self.assertEqual(args.base_image, "debian:bookworm")
        self.assertEqual(args.copilot_host, "acme.ghe.com")
        self.assertEqual(args.capture_dir, "/tmp/caps")
        self.assertEqual(args.output_tail, 5)

    def test_rejects_non_tenant_hosts_and_bad_values(self) -> None:
        for argv in (
            ["--copilot-host", "github.com"],
            ["--copilot-host", "ghe.corp.example"],
            ["--output-tail", "-1"],
            ["--check", "nope"],
            ["--runtime", "chroot"],
        ):
            with self.subTest(argv=argv):
                with (
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit),
                ):
                    self.script.parse_args(argv)


class E2ECopilotSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = _load_script()

    def test_personal_run_skips_tenant(self) -> None:
        args = self.script.parse_args([])
        plan = self.script.selected_checks(args)
        self.assertEqual(
            [name for name, skip in plan if skip is None],
            ["headless-ok", "headless-fail", "trusted-folder", "isolation"],
        )
        self.assertEqual(dict(plan)["tenant"], "pass --copilot-host <tenant>.ghe.com")
        self.assertEqual(self.script.hosts_needed(plan, args), ["github.com"])

    def test_tenant_run_includes_tenant_check(self) -> None:
        args = self.script.parse_args(["--copilot-host", "acme.ghe.com"])
        plan = self.script.selected_checks(args)
        self.assertTrue(all(skip is None for _name, skip in plan))
        self.assertEqual(
            self.script.hosts_needed(plan, args), ["github.com", "acme.ghe.com"]
        )

    def test_check_filter_keeps_execution_order(self) -> None:
        args = self.script.parse_args(
            ["--check", "isolation", "--check", "headless-ok"]
        )
        self.assertEqual(
            self.script.selected_checks(args),
            [("headless-ok", None), ("isolation", None)],
        )

    def test_tenant_only_resolves_only_the_tenant_token(self) -> None:
        args = self.script.parse_args(
            ["--check", "tenant", "--copilot-host", "acme.ghe.com"]
        )
        plan = self.script.selected_checks(args)
        self.assertEqual(self.script.hosts_needed(plan, args), ["acme.ghe.com"])

    def test_only_skipped_checks_do_no_lookup_or_container_work(self) -> None:
        output = io.StringIO()
        with (
            patch.object(self.script.copilot_auth, "resolve_token") as resolve,
            patch.object(self.script.subprocess, "run") as run,
            patch.object(self.script.tempfile, "mkdtemp") as mkdtemp,
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(self.script.main(["--check", "tenant"]), 0)
        resolve.assert_not_called()
        run.assert_not_called()
        mkdtemp.assert_not_called()
        self.assertIn("SKIP: tenant", output.getvalue())


class E2ECopilotRedactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = _load_script()

    def test_redact_and_tail(self) -> None:
        text = f"a\ntoken={TOKEN}\nb\nc"
        self.assertEqual(self.script.redact(text, [TOKEN]), "a\ntoken=[REDACTED]\nb\nc")
        self.assertEqual(self.script.tail("1\n2\n3", 2), "2\n3")
        self.assertEqual(self.script.tail("1\n2\n3", 0), "")

    def test_build_command_never_contains_the_token(self) -> None:
        args = self.script.parse_args(["--no-build", "--runtime", "docker"])
        with tempfile.TemporaryDirectory() as tmp:
            command = self.script.build_command(
                args,
                Path(tmp),
                agent="copilot",
                prompt="hi",
                log_path=Path(tmp) / "x.log",
                host="acme.ghe.com",
                model="gpt-5-mini",
            )
        self.assertNotIn(TOKEN, " ".join(command))
        self.assertEqual(command[1:3], ["-m", "project_sandbox"])
        for flag, value in (
            ("--agent", "copilot"),
            ("--copilot-host", "acme.ghe.com"),
            ("--model", "gpt-5-mini"),
            ("--runtime", "docker"),
        ):
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertIn("--no-build", command)

    def test_build_command_omits_default_host(self) -> None:
        args = self.script.parse_args([])
        command = self.script.build_command(
            args, Path("/p"), agent="copilot", prompt="hi", log_path=Path("/p/x.log")
        )
        self.assertNotIn("--copilot-host", command)
        self.assertNotIn("--model", command)

    def test_capture_writes_redacted_jsonl_and_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root / "headless-ok.log"
            log.write_text(
                "firewall ready\n"
                + json.dumps({"type": "assistant.message", "data": {"content": "hi"}})
                + "\n"
                + json.dumps({"type": "session.error", "data": {"message": TOKEN}})
                + "\n{broken\n",
                encoding="utf-8",
            )
            run = self.script.Run(1, f"out {TOKEN}", log)
            self.script.write_capture(root / "caps", "headless-ok", run, [TOKEN])
            jsonl = (root / "caps" / "headless-ok.jsonl").read_text()
            exit_info = json.loads(
                (root / "caps" / "headless-ok.exit.json").read_text()
            )
        self.assertNotIn(TOKEN, jsonl)
        self.assertEqual(len(jsonl.splitlines()), 2)
        self.assertEqual(exit_info["exit_code"], 1)
        self.assertEqual(exit_info["records"], 2)
        self.assertEqual(
            exit_info["final_record"],
            {"type": "session.error", "data": {"message": "[REDACTED]"}},
        )

    def test_audit_source_is_valid_python(self) -> None:
        compile(self.script.AUDIT_SOURCE, "copilot-audit.py", "exec")


if __name__ == "__main__":
    unittest.main()


class E2ECopilotRunnerTests(unittest.TestCase):
    """run-e2e-tests.sh: `copilot` is opt-in and never looks up a token unasked."""

    ROOT = Path(__file__).resolve().parents[1]

    def _sandboxed_runner(self, tmp: Path) -> tuple[Path, Path, dict[str, str]]:
        """Copy the runner with every suite script stubbed out; fake uv/pass/gh."""
        import os
        import re
        import shutil

        scripts = tmp / "repo" / "scripts"
        scripts.mkdir(parents=True)
        source = (self.ROOT / "scripts" / "run-e2e-tests.sh").read_text()
        runner = scripts / "run-e2e-tests.sh"
        runner.write_text(source)
        for name in set(re.findall(r"\$ROOT/scripts/([\w.-]+)", source)):
            if name != "run-e2e-tests.sh":
                stub = scripts / name
                stub.write_text("#!/bin/sh\nexit 0\n")
                stub.chmod(0o755)
        bindir = tmp / "bin"
        bindir.mkdir()
        calls = tmp / "calls"
        for tool in ("uv", "pass", "gh"):
            fake = bindir / tool
            fake.write_text(
                f'#!/bin/sh\nprintf "{tool} %s\\n" "$*" >> "$E2E_CALL_LOG"\n'
                + ("exit 0\n" if tool == "uv" else "exit 1\n")
            )
            fake.chmod(0o755)
        for real in ("bash", "sed", "mktemp", "uname", "rm", "rmdir", "tail", "cat"):
            found = shutil.which(real)
            if found and not (bindir / real).exists():
                (bindir / real).symlink_to(found)
        env = {
            "PATH": str(bindir),
            "E2E_CALL_LOG": str(calls),
            "TMPDIR": str(tmp),
            "HOME": os.environ.get("HOME", str(tmp)),
        }
        return runner, calls, env

    def _run(self, *args: str) -> tuple[str, list[str]]:
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            runner, calls, env = self._sandboxed_runner(Path(tmp))
            result = subprocess.run(
                [env["PATH"] + "/bash", str(runner), "--runtime", "docker", *args],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            lines = calls.read_text().splitlines() if calls.exists() else []
        return result.stdout + result.stderr, lines

    def test_list_includes_copilot(self) -> None:
        output, _calls = self._run("--list")
        self.assertIn("copilot", output.splitlines())

    def test_default_run_skips_copilot_without_any_token_lookup(self) -> None:
        output, calls = self._run()
        self.assertRegex(output, r"copilot\s+SKIP \(pass --with-copilot")
        self.assertFalse([c for c in calls if "e2e-copilot" in c], calls)
        self.assertFalse([c for c in calls if c.startswith(("pass", "gh"))], calls)

    def test_with_copilot_dispatches_the_script(self) -> None:
        for flags in (
            ["--with-copilot"],
            ["--only", "copilot"],
        ):
            with self.subTest(flags=flags):
                output, calls = self._run(
                    *flags,
                    "--copilot-host",
                    "acme.ghe.com",
                    "--copilot-capture-dir",
                    "/tmp/caps",
                )
                dispatched = [c for c in calls if "e2e-copilot.py" in c]
                self.assertEqual(len(dispatched), 1, output)
                self.assertIn(
                    "--runtime docker --base-image python:3.12-slim "
                    "--copilot-host acme.ghe.com --capture-dir /tmp/caps",
                    dispatched[0],
                )
                self.assertRegex(output, r"copilot\s+PASS")
