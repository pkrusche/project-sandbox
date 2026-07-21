from pathlib import Path
from unittest import TestCase

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

    def test_bash_modes_allow_every_supported_credential_agent(self) -> None:
        for mode in ("bash", "bash-headless"):
            with self.subTest(mode=mode):
                self.assertEqual(
                    allowed_credential_agents(mode), SUPPORTED_CREDENTIAL_AGENTS
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
