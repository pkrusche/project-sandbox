"""Host tools are found only through trusted PATH entries and run from a neutral cwd.

The call-site tests use real fake executables: a trusted ``bin`` directory
holds recording stubs, and the project (and ``.``) holds look-alikes that must
never run.
"""

import contextlib
import json
import os
import stat
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import (
    agent_proxy,
    config_agents,
    copilot_auth,
    git_identity,
    host_tools,
    oauth_refresh,
)

RECORDER = """#!/bin/sh
printf '%s\\t%s\\n' "$0" "$PWD" >> "{log}"
{body}
"""

TRAP = """#!/bin/sh
touch "{marker}"
"""


def _executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class HostToolsCase(TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.trusted = self.root / "trusted-bin"
        self.trusted.mkdir()
        self.log = self.root / "calls.log"
        self.marker = self.root / "TRAP-RAN"
        host_tools.reset()
        self.addCleanup(host_tools.reset)
        host_tools.add_untrusted_roots(self.project)

    def stub(self, name: str, body: str = "") -> Path:
        path = self.trusted / name
        _executable(path, RECORDER.format(log=self.log, body=body))
        return path

    def trap(self, directory: Path, name: str) -> Path:
        path = directory / name
        _executable(path, TRAP.format(marker=self.marker))
        return path

    def calls(self) -> list[tuple[str, str]]:
        if not self.log.exists():
            return []
        return [
            tuple(line.split("\t", 1))  # type: ignore[misc]
            for line in self.log.read_text(encoding="utf-8").splitlines()
        ]

    @contextlib.contextmanager
    def env(self, *entries: str, cwd: Path | None = None) -> Iterator[None]:
        previous = os.getcwd()
        with patch.dict(os.environ, {"PATH": os.pathsep.join(entries)}):
            os.chdir(cwd or self.project)
            try:
                yield
            finally:
                os.chdir(previous)

    def assert_neutral(self, cwd: str) -> None:
        neutral = host_tools.neutral_dir().resolve()
        self.assertEqual(Path(cwd).resolve(), neutral)


class ResolveTests(HostToolsCase):
    def test_relative_entry_is_skipped(self) -> None:
        self.trap(self.project, "jj")
        stub = self.stub("jj")
        with self.env(".", str(self.trusted)):
            self.assertEqual(host_tools.resolve("jj"), str(stub))

    def test_empty_entry_is_skipped(self) -> None:
        self.trap(self.project, "jj")
        with self.env("", str(self.trusted)):
            self.assertIsNone(host_tools.resolve("jj"))

    def test_tool_only_on_relative_entry_is_missing_and_named(self) -> None:
        self.trap(self.project, "jj")
        with self.env(".", str(self.trusted)):
            self.assertIsNone(host_tools.resolve("jj"))
            message = host_tools.missing_message("jj")
        self.assertIn("jj not found on PATH", message)
        self.assertIn(". (relative)", message)

    def test_path_directory_inside_project_is_skipped(self) -> None:
        project_bin = self.project / "bin"
        self.trap(project_bin, "claude")
        stub = self.stub("claude")
        with self.env(str(project_bin), str(self.trusted)):
            self.assertEqual(host_tools.resolve("claude"), str(stub))
            self.assertIn(
                "inside the project or a session directory",
                host_tools.missing_message("nothing-here")
                + " ".join(host_tools.skipped_matches("claude")),
            )

    def test_symlink_into_project_is_skipped(self) -> None:
        target = self.trap(self.project / "bin", "gh")
        (self.trusted / "gh").symlink_to(target)
        with self.env(str(self.trusted)):
            self.assertIsNone(host_tools.resolve("gh"))

    def test_session_directory_root_is_untrusted(self) -> None:
        sessions = self.root / "project-workspaces"
        self.trap(sessions / "feature" / "bin", "jj")
        host_tools.add_untrusted_roots(sessions)
        with self.env(str(sessions / "feature" / "bin")):
            self.assertIsNone(host_tools.resolve("jj"))

    def test_absolute_name_inside_project_is_refused(self) -> None:
        trap = self.trap(self.project, "tool")
        stub = self.stub("tool")
        with self.env(str(self.trusted)):
            self.assertIsNone(host_tools.resolve(str(trap)))
            self.assertEqual(host_tools.resolve(str(stub)), str(stub))


class RunTests(HostToolsCase):
    def test_run_uses_absolute_program_and_neutral_cwd(self) -> None:
        stub = self.stub("tool")
        self.trap(self.project, "tool")
        with self.env(".", str(self.trusted)):
            host_tools.run(["tool"], check=True)
        [(program, cwd)] = self.calls()
        self.assertEqual(program, str(stub))
        self.assert_neutral(cwd)
        self.assertFalse(self.marker.exists())

    def test_neutral_dir_is_private_empty_and_outside_project(self) -> None:
        neutral = host_tools.neutral_dir()
        self.assertEqual(stat.S_IMODE(neutral.stat().st_mode), 0o700)
        self.assertEqual(list(neutral.iterdir()), [])
        self.assertFalse(neutral.resolve().is_relative_to(self.project))

    def test_missing_tool_raises_file_not_found(self) -> None:
        with self.env(str(self.trusted)), self.assertRaises(FileNotFoundError):
            host_tools.run(["no-such-tool"])

    def test_restricted_path_narrows_and_restores(self) -> None:
        project_bin = self.project / "bin"
        sessions = self.root / "project-workspaces"
        original = os.pathsep.join(
            [".", "", str(project_bin), str(self.trusted), str(sessions / "bin")]
        )
        with patch.dict(os.environ, {"PATH": original}):
            with host_tools.restricted_path():
                self.assertEqual(
                    os.environ["PATH"],
                    os.pathsep.join([str(self.trusted), str(sessions / "bin")]),
                )
                host_tools.add_untrusted_roots(sessions)
                self.assertEqual(os.environ["PATH"], str(self.trusted))
            self.assertEqual(os.environ["PATH"], original)

    def test_launch_scope_forgets_roots(self) -> None:
        with host_tools.launch_scope():
            host_tools.add_untrusted_roots(self.root / "x")
            self.assertEqual(host_tools.untrusted_roots(), ((self.root / "x"),))
        self.assertEqual(host_tools.untrusted_roots(), ())


class CallSiteTests(HostToolsCase):
    """Every host tool call site: absolute argv[0], neutral cwd, no look-alike runs."""

    def setUp(self) -> None:
        super().setUp()
        # Look-alikes in the project and on a relative entry.
        for name in ("claude", "codex", "pass", "gh", "security", "git"):
            self.trap(self.project, name)
            self.trap(self.project / "bin", name)
        self.path = (".", str(self.project / "bin"), str(self.trusted))

    def assert_single_call(self, name: str) -> None:
        calls = self.calls()
        self.assertEqual([program for program, _ in calls], [str(self.trusted / name)])
        self.assert_neutral(calls[0][1])
        self.assertFalse(self.marker.exists())

    def test_token_refresh_in_project_with_hostile_settings(self) -> None:
        # The project's settings would run a marker-writing helper and hook if
        # the CLI loaded them from its working directory.
        settings = {
            "apiKeyHelper": f"touch {self.marker}",
            "env": {"MARKER": str(self.marker)},
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": f"touch {self.marker}"}]}
                ]
            },
        }
        (self.project / ".claude").mkdir()
        (self.project / ".claude" / "settings.json").write_text(json.dumps(settings))
        # The fake claude acts on project settings found in its cwd.
        self.stub(
            "claude",
            body=(f'[ -f .claude/settings.json ] && touch "{self.marker}"\nexit 0'),
        )
        home = self.root / "home"
        (home / ".claude").mkdir(parents=True)
        with self.env(*self.path):
            oauth_refresh.refresh_host_token("claude", home=home)
        self.assert_single_call("claude")

    def test_codex_refresh_ignores_project_config(self) -> None:
        (self.project / ".codex").mkdir()
        (self.project / ".codex" / "config.toml").write_text('model = "x"\n')
        self.stub(
            "codex",
            body=f'[ -f .codex/config.toml ] && touch "{self.marker}"\nexit 0',
        )
        home = self.root / "home"
        (home / ".codex").mkdir(parents=True)
        with self.env(*self.path):
            oauth_refresh.refresh_host_token("codex", home=home)
        self.assert_single_call("codex")

    def test_agent_proxy_pass_lookup(self) -> None:
        self.stub("pass", body="echo gateway-key")
        with self.env(*self.path):
            self.assertEqual(
                agent_proxy.resolve_key("UNSET_ENV_FOR_TEST", None),
                ("gateway-key", "pass"),
            )
        self.assert_single_call("pass")

    def test_copilot_pass_lookup(self) -> None:
        self.stub("pass", body="echo github_pat_" + "a" * 82)
        with self.env(*self.path):
            _token, source = copilot_auth.resolve_token("github.com")
        self.assertEqual(source, "pass")
        self.assert_single_call("pass")

    def test_copilot_gh_fallback(self) -> None:
        self.stub("gh", body="echo gho_" + "b" * 36)
        with self.env(*self.path):
            _token, source = copilot_auth.resolve_token("github.com")
        self.assertEqual(source, "gh")
        self.assert_single_call("gh")

    def test_macos_keychain_lookup(self) -> None:
        self.stub("security", body="exit 44")
        with (
            self.env(*self.path),
            patch.object(config_agents.sys, "platform", "darwin"),
            patch.object(
                config_agents, "_keychain_service_names", return_value=("svc",)
            ),
        ):
            self.assertFalse(
                config_agents._stage_macos_keychain_credentials(self.root / "out")
            )
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        for program, cwd in calls:
            self.assertEqual(program, str(self.trusted / "security"))
            self.assert_neutral(cwd)
        self.assertFalse(self.marker.exists())

    def test_git_identity_lookup(self) -> None:
        self.stub("git", body='echo "Some One"')
        with self.env(*self.path):
            identity = git_identity.read()
        self.assertEqual(identity.name, "Some One")
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        for program, cwd in calls:
            self.assertEqual(program, str(self.trusted / "git"))
            self.assert_neutral(cwd)
        self.assertFalse(self.marker.exists())

    def test_git_identity_without_git(self) -> None:
        with self.env(".", str(self.project / "bin")):
            identity = git_identity.read()
        self.assertEqual((identity.name, identity.email), (None, None))
        self.assertFalse(self.marker.exists())
