import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import config_claude, config_codex, dockerfile, firewall


def _credentials_root(root: Path):
    return patch("project_sandbox.config_claude.CREDENTIALS_ROOT", root / "tmp")


class RendererTests(TestCase):
    def test_config_and_firewall_renderers_write_expected_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)

            dockerfile.render(
                context,
                base_image="python:3.12-slim",
            )
            claude = config_claude.render(context)
            codex = config_codex.render(context)
            fw = firewall.render(
                context,
                extra_domains=["internal.example.com"],
            )

            docker_text = (context / "Dockerfile").read_text(encoding="utf-8")
            self.assertIn("FROM python:3.12-slim", docker_text)
            self.assertIn("npm install -g @anthropic-ai/claude-code", docker_text)
            self.assertIn("npm install -g @openai/codex", docker_text)
            self.assertIn("npm install -g opencode-ai", docker_text)
            self.assertIn("npm install -g @github/copilot", docker_text)
            self.assertIn("/home/agent/.claude/settings.json", docker_text)
            self.assertIn("/home/agent/.codex/config.toml", docker_text)
            self.assertIn("https://api.github.com/repos/jj-vcs/jj/releases/latest", docker_text)
            self.assertIn(
                'jj-${JJ_VERSION}-${JJ_ARCH}-unknown-linux-musl.tar.gz',
                docker_text,
            )
            self.assertIn('install -m 0755 "$jj_tmp/jj" /usr/local/bin/jj', docker_text)
            self.assertNotIn("releases/latest/download/jj-${JJ_ARCH}", docker_text)
            self.assertNotIn("tar -xz -C /usr/local/bin jj", docker_text)
            self.assertIn("bypassPermissions", claude.read_text(encoding="utf-8"))
            self.assertIn('"theme"', claude.read_text(encoding="utf-8"))
            codex_text = codex.read_text(encoding="utf-8")
            self.assertIn(
                'approval_policy = "never"', codex_text
            )
            self.assertIn("[analytics]\nenabled = false", codex_text)
            self.assertIn("[feedback]\nenabled = false", codex_text)
            firewall_text = fw.read_text(encoding="utf-8")
            self.assertIn('"claude.ai"', firewall_text)
            self.assertIn('"code.claude.com"', firewall_text)
            self.assertIn('"platform.claude.com"', firewall_text)
            self.assertIn('"api.openai.com"', firewall_text)
            self.assertIn('"auth.openai.com"', firewall_text)
            self.assertIn('"chatgpt.com"', firewall_text)
            self.assertNotIn("statsig", firewall_text)
            self.assertIn('"internal.example.com"', firewall_text)

    def test_claude_credentials_are_staged_for_directory_mounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            context = root / ".project-sandbox"
            (home / ".claude").mkdir(parents=True)
            (context / "claude").mkdir(parents=True)
            (context / "claude" / ".credentials.json").write_text("stale\n", encoding="utf-8")
            (context / "claude" / ".claude.json").write_text("stale\n", encoding="utf-8")
            (home / ".claude" / ".credentials.json").write_text(
                '{"token":"dir"}\n',
                encoding="utf-8",
            )
            (home / ".claude.json").write_text(
                '{"token":"home","theme":"dark","userID":"user-123",'
                '"lastOnboardingVersion":"2.1.144",'
                '"projects":{"/tmp":{"hasTrustDialogAccepted":true}}}\n',
                encoding="utf-8",
            )

            config = config_claude.render(context)
            with _credentials_root(root):
                staged_dir = config_claude.sync_credentials(context, home=home)

            staged_credentials = staged_dir / ".credentials.json"
            staged_home_credentials = staged_dir / ".claude.json"
            self.assertFalse((context / "claude" / ".credentials.json").exists())
            self.assertFalse((context / "claude" / ".claude.json").exists())
            self.assertEqual(
                json.loads(config.read_text(encoding="utf-8"))["theme"],
                "auto",
            )
            self.assertEqual(
                staged_credentials.read_text(encoding="utf-8"),
                '{"token":"dir"}\n',
            )
            self.assertEqual(
                json.loads(staged_home_credentials.read_text(encoding="utf-8")),
                {
                    "autoUpdaterStatus": "disabled",
                    "autoUpdates": False,
                    "bypassPermissionsModeAccepted": True,
                    "hasCompletedOnboarding": True,
                    "installMethod": "npm",
                    "lastOnboardingVersion": "2.1.144",
                    "permissions": {
                        "defaultMode": "bypassPermissions",
                        "skipDangerousModePermissionPrompt": True,
                    },
                    "projects": {"/workspace": {"hasTrustDialogAccepted": True}},
                    "token": "home",
                    "userID": "user-123",
                },
            )
            self.assertEqual(staged_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(staged_dir.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(staged_credentials.stat().st_mode & 0o777, 0o600)
            self.assertEqual(staged_home_credentials.stat().st_mode & 0o777, 0o600)

    def test_claude_config_dir_account_state_is_staged_when_root_json_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            context = root / ".project-sandbox"
            (home / ".claude").mkdir(parents=True)
            (home / ".claude" / ".claude.json").write_text(
                '{"oauthAccount":{"accountUuid":"abc"}}\n',
                encoding="utf-8",
            )

            with _credentials_root(root):
                staged_dir = config_claude.sync_credentials(context, home=home)

            staged_home_credentials = staged_dir / ".claude.json"
            self.assertEqual(
                json.loads(staged_home_credentials.read_text(encoding="utf-8")),
                {
                    "autoUpdaterStatus": "disabled",
                    "autoUpdates": False,
                    "bypassPermissionsModeAccepted": True,
                    "hasCompletedOnboarding": True,
                    "installMethod": "npm",
                    "oauthAccount": {"accountUuid": "abc"},
                    "permissions": {
                        "defaultMode": "bypassPermissions",
                        "skipDangerousModePermissionPrompt": True,
                    },
                    "projects": {"/workspace": {"hasTrustDialogAccepted": True}},
                },
            )
            self.assertEqual(staged_home_credentials.stat().st_mode & 0o777, 0o600)

    def test_claude_config_state_is_created_to_accept_bypass_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            context = root / ".project-sandbox"
            home.mkdir()

            with _credentials_root(root):
                staged_dir = config_claude.sync_credentials(context, home=home)

            staged_home_credentials = staged_dir / ".claude.json"
            self.assertEqual(
                json.loads(staged_home_credentials.read_text(encoding="utf-8")),
                {
                    "autoUpdaterStatus": "disabled",
                    "autoUpdates": False,
                    "bypassPermissionsModeAccepted": True,
                    "hasCompletedOnboarding": True,
                    "installMethod": "npm",
                    "permissions": {
                        "defaultMode": "bypassPermissions",
                        "skipDangerousModePermissionPrompt": True,
                    },
                    "projects": {"/workspace": {"hasTrustDialogAccepted": True}},
                },
            )
            self.assertEqual(staged_home_credentials.stat().st_mode & 0o777, 0o600)

    def test_claude_host_native_install_state_is_overridden_for_container(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            context = root / ".project-sandbox"
            home.mkdir()
            (home / ".claude.json").write_text(
                '{"autoUpdates":true,"installMethod":"native","theme":"light"}\n',
                encoding="utf-8",
            )

            with _credentials_root(root):
                staged_dir = config_claude.sync_credentials(context, home=home)

            staged_home_credentials = staged_dir / ".claude.json"
            self.assertEqual(
                json.loads(staged_home_credentials.read_text(encoding="utf-8")),
                {
                    "autoUpdaterStatus": "disabled",
                    "autoUpdates": False,
                    "bypassPermissionsModeAccepted": True,
                    "hasCompletedOnboarding": True,
                    "installMethod": "npm",
                    "permissions": {
                        "defaultMode": "bypassPermissions",
                        "skipDangerousModePermissionPrompt": True,
                    },
                    "projects": {"/workspace": {"hasTrustDialogAccepted": True}},
                },
            )

    def test_claude_settings_with_host_theme_are_refreshed_to_auto(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp) / ".project-sandbox"
            settings = context / "claude" / "settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text('{"theme":"dark"}\n', encoding="utf-8")

            config_claude.render(context)

            self.assertEqual(
                json.loads(settings.read_text(encoding="utf-8"))["theme"],
                "auto",
            )

    def test_claude_oauth_credentials_are_staged_from_macos_keychain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp) / ".project-sandbox"
            keychain_payload = (
                '{"claudeAiOauth":{"accessToken":"access",'
                '"refreshToken":"refresh","expiresAt":4102444800000}}\n'
            )

            with (
                patch("project_sandbox.config_claude.sys.platform", "darwin"),
                patch("project_sandbox.config_claude.shutil.which", return_value="/usr/bin/security"),
                patch(
                    "project_sandbox.config_claude._keychain_account",
                    return_value="test-user",
                ),
                patch("project_sandbox.config_claude.subprocess.run") as run,
            ):
                run.return_value = subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=keychain_payload,
                    stderr="",
                )

                with _credentials_root(Path(tmp)):
                    staged_dir = config_claude.sync_credentials(context)

            staged_credentials = staged_dir / ".credentials.json"
            self.assertEqual(
                staged_credentials.read_text(encoding="utf-8"),
                keychain_payload,
            )
            self.assertEqual(staged_credentials.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                run.call_args.args[0],
                [
                    "security",
                    "find-generic-password",
                    "-a",
                    "test-user",
                    "-w",
                    "-s",
                    "Claude Code-credentials",
                ],
            )

    def test_dockerfile_renderer_can_skip_agent_installs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            dockerfile.render(
                context,
                base_image="python:3.12-slim",
                install_agents=("codex",),
            )
            text = (context / "Dockerfile").read_text(encoding="utf-8")
            self.assertNotIn("@anthropic-ai/claude-code", text)
            self.assertIn("@openai/codex", text)
            self.assertNotIn("opencode-ai", text)
            self.assertNotIn("@github/copilot", text)

    def test_dockerfile_renderer_refreshes_stale_agent_uid_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            existing = context / "Dockerfile"
            existing.write_text(
                "FROM python:3.12-slim\n"
                "RUN if ! id -u agent >/dev/null 2>&1; then \\\n"
                "        useradd -m -u 1000 -s /bin/bash agent; \\\n"
                "    fi\n",
                encoding="utf-8",
            )
            warnings: list[str] = []

            dockerfile.render(
                context,
                base_image="python:3.12-slim",
                install_agents=("codex",),
                warn=warnings.append,
            )

            text = existing.read_text(encoding="utf-8")
            self.assertNotIn("if ! id -u agent", text)
            self.assertIn("existing_uid_user", text)
            self.assertIn("Removing existing UID 1000 user", text)
            self.assertEqual(len(warnings), 1)
            self.assertIn("Regenerating stale project-sandbox Dockerfile", warnings[0])

    def test_dockerfile_renderer_refreshes_stale_jj_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            existing = context / "Dockerfile"
            existing.write_text(
                "FROM python:3.12-slim\n"
                "RUN JJ_ARCH=$(uname -m) && \\\n"
                "    curl -fsSL \"https://github.com/jj-vcs/jj/releases/latest/download/jj-${JJ_ARCH}-unknown-linux-musl.tar.gz\" \\\n"
                "    | tar -xz -C /usr/local/bin jj && \\\n"
                "    chmod 0755 /usr/local/bin/jj\n",
                encoding="utf-8",
            )
            warnings: list[str] = []

            dockerfile.render(
                context,
                base_image="python:3.12-slim",
                install_agents=("codex",),
                warn=warnings.append,
            )

            text = existing.read_text(encoding="utf-8")
            self.assertNotIn("releases/latest/download/jj-${JJ_ARCH}", text)
            self.assertNotIn("tar -xz -C /usr/local/bin jj", text)
            self.assertIn('install -m 0755 "$jj_tmp/jj" /usr/local/bin/jj', text)
            self.assertEqual(len(warnings), 1)
            self.assertIn("old jj download URL", warnings[0])
            self.assertIn("old jj extraction", warnings[0])

    def test_dockerfile_renderer_refreshes_missing_config_mount_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            existing = context / "Dockerfile"
            existing.write_text(
                "FROM python:3.12-slim\n"
                "COPY entrypoint.sh /usr/local/bin/project-sandbox-entrypoint\n",
                encoding="utf-8",
            )
            warnings: list[str] = []

            dockerfile.render(
                context,
                base_image="python:3.12-slim",
                install_agents=("codex",),
                warn=warnings.append,
            )

            text = existing.read_text(encoding="utf-8")
            self.assertIn("/home/agent/.claude/settings.json", text)
            self.assertIn("/home/agent/.codex/config.toml", text)
            self.assertEqual(len(warnings), 1)
            self.assertIn("old config file mount targets", warnings[0])

    def test_dockerfile_renderer_extends_source_dockerfile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            context = project / ".project-sandbox"
            context.mkdir()
            source = project / "Dockerfile"
            source.write_text(
                "# syntax=docker/dockerfile:1\n"
                "FROM ubuntu:24.04\n"
                "RUN echo app-layer\n"
                "USER app\n",
                encoding="utf-8",
            )
            warnings: list[str] = []

            dockerfile.render(
                context,
                base_dockerfile=source,
                build_context=project,
                install_agents=("codex",),
                warn=warnings.append,
            )

            text = (context / "Dockerfile").read_text(encoding="utf-8")
            self.assertTrue(text.startswith("# syntax=docker/dockerfile:1\n"))
            self.assertIn("RUN echo app-layer", text)
            self.assertNotIn("USER app", text)
            self.assertIn("USER root", text)
            self.assertIn("npm install -g @openai/codex", text)
            self.assertIn(
                "COPY .project-sandbox/init-firewall.sh /usr/local/bin/project-sandbox-init-firewall",
                text,
            )
            self.assertEqual(len(warnings), 1)
            self.assertIn("Removed 1 restricted user setup instruction", warnings[0])

    def test_dockerfile_renderer_removes_source_user_id_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            context = project / ".project-sandbox"
            context.mkdir()
            source = project / "Dockerfile"
            source.write_text(
                "FROM ubuntu:24.04\n"
                "ARG USERNAME=vscode\n"
                "RUN groupadd --gid 1000 $USERNAME && \\\n"
                "    useradd --uid 1000 --gid 1000 -m $USERNAME\n"
                "RUN echo app-layer\n"
                "USER $USERNAME\n",
                encoding="utf-8",
            )
            warnings: list[str] = []

            dockerfile.render(
                context,
                base_dockerfile=source,
                build_context=project,
                install_agents=("codex",),
                warn=warnings.append,
            )

            text = (context / "Dockerfile").read_text(encoding="utf-8")
            self.assertIn("ARG USERNAME=vscode", text)
            self.assertIn("RUN echo app-layer", text)
            self.assertNotIn("groupadd --gid 1000", text)
            self.assertNotIn("useradd --uid 1000", text)
            self.assertNotIn("USER $USERNAME", text)
            self.assertIn("groupadd -g 1000 agent", text)
            self.assertIn("useradd -m -u 1000 -g agent -s /bin/bash agent", text)
            self.assertIn("Removing existing UID 1000 user", text)
            self.assertIn("Removing existing GID 1000 group", text)
            self.assertEqual(len(warnings), 1)
            self.assertIn("Removed 2 restricted user setup instructions", warnings[0])

    def test_entrypoint_supports_all_headless_agents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            dockerfile.render_entrypoint(context)
            text = (context / "entrypoint.sh").read_text(encoding="utf-8")
            self.assertIn("/project-sandbox-config/claude/settings.json", text)
            self.assertIn("/project-sandbox-secrets/claude/.claude.json", text)
            self.assertIn("/project-sandbox-secrets/claude/.credentials.json", text)
            self.assertIn('"$HOME/.claude/.claude.json"', text)
            self.assertIn("CLAUDE_SECURESTORAGE_CONFIG_DIR", text)
            self.assertNotIn("CLAUDE_CONFIG_DIR", text)
            self.assertNotIn(".claude.host", text)
            self.assertIn("/project-sandbox-config/codex/config.toml", text)
            self.assertIn("sudo -n /usr/local/bin/project-sandbox-init-firewall", text)
            self.assertNotIn("sudo chown", text)
            self.assertIn('jj config set --user user.name "$NAME"', text)
            self.assertIn('jj config set --user user.email "$EMAIL"', text)
            self.assertIn("claude-headless", text)
            self.assertIn("codex-headless", text)
            self.assertIn("opencode-headless", text)
            self.assertIn("copilot-headless", text)
            self.assertIn("bash-headless", text)
            self.assertIn('exec bash -lc "$PROMPT"', text)

    def test_entrypoint_renderer_refreshes_missing_jj_identity_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            existing = context / "entrypoint.sh"
            existing.write_text(
                "#!/bin/sh\n"
                "[ -n \"${PROJECT_SANDBOX_USER_NAME:-}\" ] "
                "&& git config --global user.name "
                "\"$PROJECT_SANDBOX_USER_NAME\"\n",
                encoding="utf-8",
            )

            dockerfile.render_entrypoint(context)
            text = existing.read_text(encoding="utf-8")

            self.assertIn('jj config set --user user.name "$NAME"', text)
            self.assertIn('jj config set --user user.email "$EMAIL"', text)

    def test_entrypoint_renderer_refreshes_config_dir_claude_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            existing = context / "entrypoint.sh"
            existing.write_text(
                "#!/bin/sh\n"
                "if [ -f \"/project-sandbox-config/claude/.claude.json\" ]; then\n"
                "  cp \"/project-sandbox-config/claude/.claude.json\" \"$HOME/.claude.json\"\n"
                "fi\n",
                encoding="utf-8",
            )

            dockerfile.render_entrypoint(context)
            text = existing.read_text(encoding="utf-8")

            self.assertIn("/project-sandbox-secrets/claude/.claude.json", text)
            self.assertNotIn("/project-sandbox-config/claude/.claude.json", text)

    def test_devcontainer_entrypoint_copies_staged_claude_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            dockerfile.render_devcontainer_entrypoint(context)
            text = (context / "project-sandbox-devcontainer-init").read_text(
                encoding="utf-8"
            )
            self.assertIn("/project-sandbox-config/claude/settings.json", text)
            self.assertIn("/project-sandbox-secrets/claude/.claude.json", text)
            self.assertIn("/project-sandbox-secrets/claude/.credentials.json", text)
            self.assertIn('"$HOME/.claude/.claude.json"', text)
            self.assertIn("CLAUDE_SECURESTORAGE_CONFIG_DIR", text)
            self.assertNotIn("CLAUDE_CONFIG_DIR", text)
            self.assertIn("re-run 'project-sandbox <project> <base_image>'", text)
            self.assertNotIn(".claude.host", text)
            self.assertIn("/project-sandbox-config/codex/config.toml", text)
            self.assertIn('jj config set --user user.name "$NAME"', text)
            self.assertIn('jj config set --user user.email "$EMAIL"', text)

    def test_devcontainer_entrypoint_renderer_refreshes_config_dir_claude_credentials(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            existing = context / "project-sandbox-devcontainer-init"
            existing.write_text(
                "#!/bin/sh\n"
                "if [ -f \"/project-sandbox-config/claude/.claude.json\" ]; then\n"
                "  cp \"/project-sandbox-config/claude/.claude.json\" \"$HOME/.claude.json\"\n"
                "fi\n",
                encoding="utf-8",
            )

            dockerfile.render_devcontainer_entrypoint(context)
            text = existing.read_text(encoding="utf-8")

            self.assertIn("/project-sandbox-secrets/claude/.claude.json", text)
            self.assertNotIn("/project-sandbox-config/claude/.claude.json", text)

    def test_entrypoint_renderer_refreshes_stale_claude_config_dir_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            existing = context / "entrypoint.sh"
            existing.write_text(
                "#!/bin/sh\n"
                "export CLAUDE_CONFIG_DIR=\"${CLAUDE_CONFIG_DIR:-$HOME/.claude}\"\n",
                encoding="utf-8",
            )

            dockerfile.render_entrypoint(context)
            text = existing.read_text(encoding="utf-8")

            self.assertIn("CLAUDE_SECURESTORAGE_CONFIG_DIR", text)
            self.assertNotIn("CLAUDE_CONFIG_DIR", text)

    def test_devcontainer_entrypoint_refreshes_missing_jj_identity_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            existing = context / "project-sandbox-devcontainer-init"
            existing.write_text(
                "#!/bin/sh\n"
                "[ -n \"$NAME\" ] && git config --global user.name \"$NAME\"\n",
                encoding="utf-8",
            )

            dockerfile.render_devcontainer_entrypoint(context)
            text = existing.read_text(encoding="utf-8")

            self.assertIn('jj config set --user user.name "$NAME"', text)
            self.assertIn('jj config set --user user.email "$EMAIL"', text)
