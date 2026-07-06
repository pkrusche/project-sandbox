import contextlib
import datetime as dt
import io
import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import cli

_EXPIRY = dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)


def _warn(*, forward=True, expiry=_EXPIRY, remaining=3600, run_mode_agent="claude"):
    out = io.StringIO()
    with (
        patch.object(
            cli.token_expiry, "staged_token_expiry", return_value=expiry
        ) as staged,
        patch.object(
            cli.token_expiry, "remaining", return_value=dt.timedelta(seconds=remaining)
        ),
        contextlib.redirect_stdout(out),
    ):
        cli._warn_forwarded_credential_lifetime(
            run_mode_agent=run_mode_agent,
            credential_dirs={"claude": Path("/staged/claude")},
            forward_credentials=forward,
        )
    return out.getvalue(), staged


class CredentialLifetimeWarningTests(TestCase):
    def test_warns_with_remaining_lifetime(self) -> None:
        out, _ = _warn(remaining=3600)
        self.assertIn("valid for ~1h00m", out)
        self.assertIn("re-authenticate on the", out)

    def test_expired_credentials_warn(self) -> None:
        out, _ = _warn(remaining=0)
        self.assertIn("already expired", out)

    def test_no_forward_credentials_is_silent(self) -> None:
        out, staged = _warn(forward=False)
        self.assertEqual(out, "")
        staged.assert_not_called()

    def test_unknown_expiry_is_silent(self) -> None:
        out, _ = _warn(expiry=None)
        self.assertEqual(out, "")
