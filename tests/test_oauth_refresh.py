import contextlib
import io
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import oauth_refresh


def _home_with(agent_dir: str) -> tempfile.TemporaryDirectory:
    tmp = tempfile.TemporaryDirectory()
    (Path(tmp.name) / agent_dir).mkdir(parents=True)
    return tmp


class OAuthRefreshTests(TestCase):
    def test_dry_run_does_nothing(self) -> None:
        with patch.object(oauth_refresh.subprocess, "run") as run:
            oauth_refresh.refresh_host_token("claude", home=Path("/nope"), dry_run=True)
        run.assert_not_called()

    def test_unknown_agent_is_noop(self) -> None:
        with patch.object(oauth_refresh.subprocess, "run") as run:
            oauth_refresh.refresh_host_token("opencode", home=Path("/nope"))
        run.assert_not_called()

    def test_missing_cli_is_noop(self) -> None:
        with _home_with(".claude") as tmp:
            with (
                patch.object(oauth_refresh.shutil, "which", return_value=None),
                patch.object(oauth_refresh.subprocess, "run") as run,
            ):
                oauth_refresh.refresh_host_token("claude", home=Path(tmp))
        run.assert_not_called()

    def test_missing_config_dir_is_noop(self) -> None:
        # claude CLI present but the host has no ~/.claude — nothing to refresh.
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(oauth_refresh.shutil, "which", return_value="/usr/bin/claude"),
                patch.object(oauth_refresh.subprocess, "run") as run,
            ):
                oauth_refresh.refresh_host_token("claude", home=Path(tmp))
        run.assert_not_called()

    def test_claude_delegates_to_claude_auth_status(self) -> None:
        with _home_with(".claude") as tmp:
            with (
                patch.object(oauth_refresh.shutil, "which", return_value="/usr/bin/claude"),
                patch.object(oauth_refresh.subprocess, "run") as run,
            ):
                oauth_refresh.refresh_host_token("claude", home=Path(tmp))
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["claude", "auth", "status"])

    def test_codex_delegates_to_codex_login_status(self) -> None:
        with _home_with(".codex") as tmp:
            with (
                patch.object(oauth_refresh.shutil, "which", return_value="/usr/bin/codex"),
                patch.object(oauth_refresh.subprocess, "run") as run,
            ):
                oauth_refresh.refresh_host_token("codex", home=Path(tmp))
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["codex", "login", "status"])

    def test_subprocess_failure_is_swallowed(self) -> None:
        with _home_with(".claude") as tmp:
            with (
                patch.object(oauth_refresh.shutil, "which", return_value="/usr/bin/claude"),
                patch.object(
                    oauth_refresh.subprocess,
                    "run",
                    side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=30),
                ),
                contextlib.redirect_stdout(io.StringIO()) as out,
            ):
                # Must not raise — a refresh problem can never block a launch.
                oauth_refresh.refresh_host_token("claude", home=Path(tmp))
        self.assertIn("refresh skipped", out.getvalue())

    def test_not_logged_in_stays_silent_even_when_verbose(self) -> None:
        # The ordinary "not logged in" outcome (non-zero exit, unrelated stderr)
        # must remain fully silent, verbose or not -- only an unrecognized
        # subcommand should trigger the new diagnostic.
        not_logged_in = subprocess.CompletedProcess(
            args=["claude", "auth", "status"],
            returncode=1,
            stdout=b"",
            stderr=b"Not logged in. Run `claude login` to authenticate.\n",
        )
        with _home_with(".claude") as tmp:
            with (
                patch.object(oauth_refresh.shutil, "which", return_value="/usr/bin/claude"),
                patch.object(oauth_refresh.subprocess, "run", return_value=not_logged_in),
                contextlib.redirect_stdout(io.StringIO()) as out,
            ):
                oauth_refresh.refresh_host_token("claude", home=Path(tmp), verbose=True)
        self.assertEqual(out.getvalue(), "")

    def test_unrecognized_subcommand_is_silent_without_verbose(self) -> None:
        unknown_command = subprocess.CompletedProcess(
            args=["claude", "auth", "status"],
            returncode=2,
            stdout=b"",
            stderr=b"error: unknown command 'status'\n",
        )
        with _home_with(".claude") as tmp:
            with (
                patch.object(oauth_refresh.shutil, "which", return_value="/usr/bin/claude"),
                patch.object(oauth_refresh.subprocess, "run", return_value=unknown_command),
                contextlib.redirect_stdout(io.StringIO()) as out,
            ):
                oauth_refresh.refresh_host_token("claude", home=Path(tmp), verbose=False)
        self.assertEqual(out.getvalue(), "")

    def test_unrecognized_subcommand_surfaces_diagnostic_when_verbose(self) -> None:
        # Simulate a pinned CLI version where `claude auth status` was renamed or
        # removed upstream: non-zero exit plus stderr calling out the unknown
        # subcommand. This must be surfaced (not silently swallowed like a
        # regular "not logged in" result) so a broken delegate is noticeable.
        unknown_command = subprocess.CompletedProcess(
            args=["claude", "auth", "status"],
            returncode=2,
            stdout=b"",
            stderr=b"error: unknown command 'status' for 'claude auth'\n",
        )
        with _home_with(".claude") as tmp:
            with (
                patch.object(oauth_refresh.shutil, "which", return_value="/usr/bin/claude"),
                patch.object(oauth_refresh.subprocess, "run", return_value=unknown_command),
                contextlib.redirect_stdout(io.StringIO()) as out,
            ):
                oauth_refresh.refresh_host_token("claude", home=Path(tmp), verbose=True)
        output = out.getvalue()
        self.assertIn("claude auth status", output)
        self.assertIn("not recognized", output)

    def test_missing_binary_after_which_check_surfaces_diagnostic_when_verbose(self) -> None:
        # shutil.which() reported the binary present, but the exec itself failed
        # (e.g. a broken symlink or a race with uninstallation) -- this is a
        # broken-delegate signal distinct from the generic swallowed-exception
        # path, so it should be surfaced under --verbose too.
        with _home_with(".claude") as tmp:
            with (
                patch.object(oauth_refresh.shutil, "which", return_value="/usr/bin/claude"),
                patch.object(
                    oauth_refresh.subprocess,
                    "run",
                    side_effect=FileNotFoundError("[Errno 2] No such file or directory: 'claude'"),
                ),
                contextlib.redirect_stdout(io.StringIO()) as out,
            ):
                oauth_refresh.refresh_host_token("claude", home=Path(tmp), verbose=True)
        self.assertIn("disappeared", out.getvalue())

    def test_missing_binary_after_which_check_is_silent_without_verbose(self) -> None:
        with _home_with(".claude") as tmp:
            with (
                patch.object(oauth_refresh.shutil, "which", return_value="/usr/bin/claude"),
                patch.object(
                    oauth_refresh.subprocess,
                    "run",
                    side_effect=FileNotFoundError("[Errno 2] No such file or directory: 'claude'"),
                ),
                contextlib.redirect_stdout(io.StringIO()) as out,
            ):
                oauth_refresh.refresh_host_token("claude", home=Path(tmp), verbose=False)
        self.assertEqual(out.getvalue(), "")
