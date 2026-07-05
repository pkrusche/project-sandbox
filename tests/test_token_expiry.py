import base64
import datetime as dt
import json
import sys
import tempfile
from pathlib import Path
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import token_expiry


def _ms(when: dt.datetime) -> int:
    return int(when.timestamp() * 1000)


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _jwt(exp: int) -> str:
    def part(obj: dict) -> str:
        raw = json.dumps(obj).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return f"{part({'alg': 'none'})}.{part({'exp': exp})}.sig"


class TokenExpiryTests(TestCase):
    def test_claude_expiry_parsed_from_credentials(self) -> None:
        when = dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            claude_dir = Path(tmp) / "claude"
            _write(
                claude_dir / ".credentials.json",
                {"claudeAiOauth": {"accessToken": "sk", "expiresAt": _ms(when)}},
            )
            expiry = token_expiry.staged_token_expiry({"claude": claude_dir}, "claude")
        self.assertEqual(expiry, when)

    def test_headless_agent_name_is_stripped(self) -> None:
        when = dt.datetime(2030, 6, 1, tzinfo=dt.timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            claude_dir = Path(tmp) / "claude"
            _write(
                claude_dir / ".credentials.json",
                {"claudeAiOauth": {"accessToken": "sk", "expiresAt": _ms(when)}},
            )
            expiry = token_expiry.staged_token_expiry(
                {"claude": claude_dir}, "claude-headless"
            )
        self.assertEqual(expiry, when)

    def test_codex_expiry_decoded_from_jwt(self) -> None:
        exp = int(dt.datetime(2031, 2, 3, tzinfo=dt.timezone.utc).timestamp())
        with tempfile.TemporaryDirectory() as tmp:
            codex_dir = Path(tmp) / "codex"
            _write(codex_dir / "auth.json", {"tokens": {"access_token": _jwt(exp)}})
            expiry = token_expiry.staged_token_expiry({"codex": codex_dir}, "codex")
        self.assertEqual(expiry, dt.datetime.fromtimestamp(exp, tz=dt.timezone.utc))

    def test_bash_falls_back_to_claude_token(self) -> None:
        when = dt.datetime(2030, 3, 3, tzinfo=dt.timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            claude_dir = Path(tmp) / "claude"
            _write(
                claude_dir / ".credentials.json",
                {"claudeAiOauth": {"accessToken": "sk", "expiresAt": _ms(when)}},
            )
            expiry = token_expiry.staged_token_expiry({"claude": claude_dir}, "bash")
        self.assertEqual(expiry, when)

    def test_opencode_missing_auth_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(
                token_expiry.staged_token_expiry({"opencode": Path(tmp)}, "opencode")
            )

    def test_opencode_reports_soonest_oauth_provider_expiry(self) -> None:
        sooner = dt.datetime(2030, 5, 1, tzinfo=dt.timezone.utc)
        later = dt.datetime(2030, 9, 1, tzinfo=dt.timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            cred_dir = Path(tmp) / "opencode"
            auth = cred_dir / ".local" / "share" / "opencode" / "auth.json"
            _write(
                auth,
                {
                    "openai": {"type": "oauth", "access": "a", "expires": _ms(later)},
                    "anthropic": {
                        "type": "oauth",
                        "access": "a",
                        "expires": _ms(sooner),
                    },
                    "some-api-key-provider": {"type": "api", "key": "sk-..."},
                },
            )
            expiry = token_expiry.staged_token_expiry(
                {"opencode": cred_dir}, "opencode-headless"
            )
        self.assertEqual(expiry, sooner)

    def test_opencode_long_lived_provider_has_no_expiry(self) -> None:
        # github-copilot stores expires=0 (mints short session tokens) → not at risk.
        with tempfile.TemporaryDirectory() as tmp:
            cred_dir = Path(tmp) / "opencode"
            auth = cred_dir / ".local" / "share" / "opencode" / "auth.json"
            _write(
                auth, {"github-copilot": {"type": "oauth", "access": "a", "expires": 0}}
            )
            self.assertIsNone(
                token_expiry.staged_token_expiry({"opencode": cred_dir}, "opencode")
            )

    def test_missing_or_malformed_credentials_return_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "absent"
            self.assertIsNone(
                token_expiry.staged_token_expiry({"claude": missing}, "claude")
            )
            bad = Path(tmp) / "claude"
            bad.mkdir()
            (bad / ".credentials.json").write_text("not json", encoding="utf-8")
            self.assertIsNone(
                token_expiry.staged_token_expiry({"claude": bad}, "claude")
            )

    def test_non_path_credential_dir_does_not_raise(self) -> None:
        # Defends the launch path against partial fixtures / mocked dirs.
        self.assertIsNone(token_expiry.staged_token_expiry({}, "claude"))
