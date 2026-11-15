from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from project_sandbox.config_agents import (
    SUPPORTED_CREDENTIAL_AGENTS,
    allowed_credential_agents,
    filter_credential_dirs,
)


class CredentialPolicyTests(TestCase):
    def test_named_interactive_and_headless_modes_allow_only_base_agent(self) -> None:
        for agent in sorted(SUPPORTED_CREDENTIAL_AGENTS):
            for mode in (agent, f"{agent}-headless"):
                with self.subTest(mode=mode):
                    self.assertEqual(allowed_credential_agents(mode), {agent})

    def test_bash_modes_allow_every_supported_credential_agent_but_copilot(
        self,
    ) -> None:
        for mode in ("bash", "bash-headless"):
            with self.subTest(mode=mode):
                self.assertEqual(
                    allowed_credential_agents(mode),
                    SUPPORTED_CREDENTIAL_AGENTS - {"copilot"},
                )

    def test_copilot_token_is_never_allowed_for_bash(self) -> None:
        staged = {"claude": Path("/staged/claude"), "copilot": Path("/staged/copilot")}
        for mode in ("bash", "bash-headless"):
            with self.subTest(mode=mode):
                self.assertNotIn("copilot", filter_credential_dirs(staged, mode))
        self.assertEqual(
            filter_credential_dirs(staged, "copilot-headless"),
            {"copilot": Path("/staged/copilot")},
        )

    def test_unknown_modes_fail_closed(self) -> None:
        for mode in ("unknown", "unknown-headless", "claude-devcontainer"):
            with self.subTest(mode=mode):
                self.assertEqual(allowed_credential_agents(mode), frozenset())

    def test_filter_credential_dirs_drops_unselected_and_profile_entries(self) -> None:
        staged = {
            "claude": Path("/staged/claude"),
            "claude-devcontainer": Path("/staged/claude-devcontainer"),
            "codex": Path("/staged/codex"),
        }
        self.assertEqual(
            filter_credential_dirs(staged, "claude-headless"),
            {"claude": Path("/staged/claude")},
        )


class StagedCredentialFileTests(TestCase):
    """Regression: copies were created umask-default, then chmod-ed to 0600."""

    def test_copies_are_private_from_creation_and_replace_planted_links(self) -> None:
        import os
        import tempfile

        from project_sandbox import config_agents

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "credentials.json"
            source.write_text('{"token": "secret"}\n')
            outside = root / "outside"
            outside.write_text("untouched\n")
            target = root / "staged.json"
            target.symlink_to(outside)
            seen_modes: list[int] = []
            real_open = os.open

            def recording_open(path, flags, mode=0o777, *args, **kwargs):
                if flags & os.O_CREAT:
                    seen_modes.append(mode)
                return real_open(path, flags, mode, *args, **kwargs)

            old = os.umask(0o002)
            try:
                with patch.object(config_agents.os, "open", recording_open):
                    self.assertTrue(config_agents._copy_if_file(source, target))
            finally:
                os.umask(old)
            self.assertEqual(seen_modes, [0o600])
            self.assertFalse(target.is_symlink())
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(target.read_text(), '{"token": "secret"}\n')
            self.assertEqual(outside.read_text(), "untouched\n")
            self.assertEqual(
                sorted(p.name for p in root.iterdir()),
                sorted(["credentials.json", "outside", "staged.json"]),
            )
