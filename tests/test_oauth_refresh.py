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
