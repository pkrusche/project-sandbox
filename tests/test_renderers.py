import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import config_agents, dockerfile, firewall


def _credentials_root(root: Path):
    return patch("project_sandbox.config_agents.CREDENTIALS_ROOT", root / "tmp")


class RendererTests(TestCase):
    def test_config_and_firewall_renderers_write_expected_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)

            dockerfile.render(
                context,
                base_image="python:3.12-slim",
            )
            cfg = config_agents.render(context)
            claude = cfg["claude"]
            codex = cfg["codex"]
            fw = firewall.render(
                context,
                extra_domains=["internal.example.com"],
            )

            docker_text = (context / "Dockerfile").read_text(encoding="utf-8")
            self.assertIn("FROM python:3.12-slim", docker_text)
            self.assertIn("npm install -g @anthropic-ai/claude-code", docker_text)
            self.assertIn("npm install -g @openai/codex", docker_text)
            self.assertIn("npm install -g opencode-ai", docker_text)
            self.assertIn("npm install -g @fission-ai/openspec@1.4.1", docker_text)
            self.assertIn("libatomic1", docker_text)
            self.assertIn("/home/agent/.claude/settings.json", docker_text)
            self.assertIn("/home/agent/.codex/config.toml", docker_text)
            self.assertIn('JJ_VERSION="v0.42.0"', docker_text)
            self.assertNotIn("releases/latest", docker_text)
            self.assertIn(
                'jj-${JJ_VERSION}-${JJ_ARCH}-unknown-linux-musl.tar.gz',
                docker_text,
            )
            self.assertNotIn("releases/download//jj--", docker_text)
            self.assertIn('install -m 0755 "$jj_tmp/jj" /usr/local/bin/jj', docker_text)
            self.assertNotIn("releases/latest/download/jj-${JJ_ARCH}", docker_text)
            self.assertNotIn("tar -xz -C /usr/local/bin jj", docker_text)
            self.assertIn("bypassPermissions", claude.read_text(encoding="utf-8"))
            self.assertIn('"theme"', claude.read_text(encoding="utf-8"))
            codex_text = codex.read_text(encoding="utf-8")
            self.assertIn(
                'approval_policy = "never"', codex_text
            )
            self.assertIn('[projects."/workspace"]\ntrust_level = "trusted"', codex_text)
            self.assertIn("[analytics]\nenabled = false", codex_text)
            self.assertIn("[feedback]\nenabled = false", codex_text)
            firewall_text = fw.read_text(encoding="utf-8")
            self.assertIn('"claude.ai"', firewall_text)
            self.assertIn('"code.claude.com"', firewall_text)
            self.assertIn('"platform.claude.com"', firewall_text)
            self.assertIn('"api.openai.com"', firewall_text)
            self.assertIn('"auth.openai.com"', firewall_text)
            self.assertIn('"chatgpt.com"', firewall_text)
            self.assertNotIn("api.github.com/meta", firewall_text)
            self.assertNotIn('"github.com"', firewall_text)
            self.assertNotIn('"api.githubcopilot.com"', firewall_text)
            self.assertNotIn("statsig", firewall_text)
            self.assertIn("internal.example.com", firewall_text)
            self.assertNotIn("--dport 22", firewall_text)
            self.assertNotIn("--sport 22", firewall_text)

    def test_dockerfile_pins_and_verifies_mutable_remote_installs(self) -> None:
        import re

        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            dockerfile.render(context, base_image="python:3.12-slim")
            docker_text = (context / "Dockerfile").read_text(encoding="utf-8")

            # No unpinned npm installs anywhere in the generated steps.
            self.assertNotIn("@latest", docker_text)
            # Every global npm tool carries an explicit @<version> pin.
            for pkg in (
                "@fission-ai/openspec",
                "@anthropic-ai/claude-code",
                "@openai/codex",
                "opencode-ai",
            ):
                self.assertRegex(
                    docker_text,
                    rf"npm install -g {re.escape(pkg)}@\d+\.\d+\.\d+",
                )

            # jj is pinned to an exact tag and not discovered via "latest".
            self.assertNotIn("releases/latest", docker_text)
            self.assertNotIn("/releases/latest", docker_text)
            self.assertRegex(docker_text, r'JJ_VERSION="v\d+\.\d+\.\d+"')

            # Node is pinned to an exact release rather than piping the
            # NodeSource installer through bash.
            self.assertNotIn("deb.nodesource.com", docker_text)
            self.assertNotIn("| bash", docker_text)
            self.assertRegex(docker_text, r'NODE_VERSION="v\d+\.\d+\.\d+"')

            # Downloaded artifacts (node + jj tarballs) are SHA256-verified
            # before use, with a per-architecture expected checksum.
            self.assertGreaterEqual(docker_text.count("sha256sum -c -"), 2)
            self.assertIn("NODE_SHA256=", docker_text)
            self.assertIn("JJ_SHA256=", docker_text)
            self.assertRegex(
                docker_text, r'echo "\$\{NODE_SHA256\}  \$\{node_tarball\}" \| sha256sum -c -'
            )
            self.assertRegex(
                docker_text, r'echo "\$\{JJ_SHA256\}  \$\{jj_tarball\}" \| sha256sum -c -'
            )

    def test_entrypoint_requires_prompt_file_for_headless_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)

            entrypoint = dockerfile.render_entrypoint(context)

            text = entrypoint.read_text(encoding="utf-8")
            self.assertIn("No prompt file provided", text)
            self.assertIn("PROJECT_SANDBOX_PROMPT_FILE", text)
            self.assertNotIn("PROJECT_SANDBOX_PROMPT:-", text)

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

            config = config_agents.render(context)["claude"]
            with _credentials_root(root):
                staged_dir = config_agents.sync_credentials(context, home=home)["claude"]

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
                staged_dir = config_agents.sync_credentials(context, home=home)["claude"]

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

    def test_non_claude_credentials_are_staged_outside_project_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = root / ".project-sandbox"
            home = root / "home"
            codex_home = root / "home" / ".codex"
            opencode_home = root / "home" / ".config" / "opencode"
            codex_home.mkdir(parents=True)
            opencode_home.mkdir(parents=True)
            (opencode_home / "node_modules" / ".bin").mkdir(parents=True)
            (opencode_home / "node_modules" / "tool.js").write_text(
                "tool\n",
                encoding="utf-8",
            )
            (opencode_home / "node_modules" / ".bin" / "tool").symlink_to(
                opencode_home / "node_modules" / "tool.js"
            )
            (opencode_home / "opencode.jsonc").write_text(
                '{"model":"test"}\n',
                encoding="utf-8",
            )
            (home / ".local" / "share" / "opencode").mkdir(parents=True)
            (home / ".local" / "state" / "opencode").mkdir(parents=True)
            (home / ".local" / "share" / "opencode" / "opencode.db").write_text(
                "db\n",
                encoding="utf-8",
            )
            (home / ".local" / "state" / "opencode" / "model.json").write_text(
                '{"model":"test"}\n',
                encoding="utf-8",
            )
            (context / "codex").mkdir(parents=True)
            (context / "opencode").mkdir(parents=True)
            (context / "codex" / "auth.json").write_text("stale\n", encoding="utf-8")
            (context / "codex" / "config.toml").write_text(
                "sandbox = true\n",
                encoding="utf-8",
            )
            (context / "opencode" / "auth.json").write_text("stale\n", encoding="utf-8")
            (codex_home / "auth.json").write_text('{"token":"codex"}\n', encoding="utf-8")
            (codex_home / "config.toml").write_text("secret = true\n", encoding="utf-8")

            with _credentials_root(root):
                result = config_agents.sync_credentials(context, home=home)
                codex_staged = result["codex"]
                opencode_staged = result["opencode"]

            self.assertFalse((context / "codex" / "auth.json").exists())
            self.assertTrue((context / "codex" / "config.toml").exists())
            self.assertFalse((context / "opencode").exists())
            self.assertEqual(
                (codex_staged / "auth.json").read_text(encoding="utf-8"),
                '{"token":"codex"}\n',
            )
            self.assertFalse((codex_staged / "config.toml").exists())
            self.assertEqual(
                (
                    opencode_staged / ".config" / "opencode" / "opencode.jsonc"
                ).read_text(encoding="utf-8"),
                '{"model":"test"}\n',
            )
            self.assertFalse(
                (opencode_staged / ".config" / "opencode" / "node_modules").exists()
            )
            self.assertEqual(
                (
                    opencode_staged / ".local" / "share" / "opencode" / "opencode.db"
                ).read_text(encoding="utf-8"),
                "db\n",
            )
            self.assertEqual(
                (
                    opencode_staged / ".local" / "state" / "opencode" / "model.json"
                ).read_text(encoding="utf-8"),
                '{"model":"test"}\n',
            )
            for staged in (codex_staged, opencode_staged):
                self.assertEqual(staged.stat().st_mode & 0o777, 0o700)

    def test_claude_config_state_is_created_to_accept_bypass_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            context = root / ".project-sandbox"
            home.mkdir()

            with _credentials_root(root):
                staged_dir = config_agents.sync_credentials(context, home=home)["claude"]

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
                staged_dir = config_agents.sync_credentials(context, home=home)["claude"]

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

    def test_claude_settings_with_host_theme_are_overwritten_to_auto(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp) / ".project-sandbox"
            settings = context / "claude" / "settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text('{"theme":"dark"}\n', encoding="utf-8")

            config_agents.render(context)

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
                patch("project_sandbox.config_agents.sys.platform", "darwin"),
                patch("project_sandbox.config_agents.shutil.which", return_value="/usr/bin/security"),
                patch(
                    "project_sandbox.config_agents._keychain_account",
                    return_value="test-user",
                ),
                patch("project_sandbox.config_agents.subprocess.run") as run,
                patch.dict(
                    "os.environ",
                    {"CLAUDE_SECURESTORAGE_CONFIG_DIR": "", "CLAUDE_CONFIG_DIR": ""},
                ),
            ):
                run.return_value = subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=keychain_payload,
                    stderr="",
                )

                with _credentials_root(Path(tmp)):
                    staged_dir = config_agents.sync_credentials(context)["claude"]

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

    def test_credentials_dir_rejects_invalid_agent_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as raised:
                config_agents.credentials_dir(Path(tmp) / ".project-sandbox", "../bad")

        self.assertIn("Invalid credential agent name", str(raised.exception))

    def test_purge_staged_credentials_removes_digest_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = root / ".project-sandbox"
            context.mkdir()
            home = root / "home"
            (home / ".claude").mkdir(parents=True)
            (home / ".claude" / ".credentials.json").write_text("{}\n", encoding="utf-8")
            with _credentials_root(root):
                staged = config_agents.sync_credentials(context, home=home)["claude"]
                digest_dir = staged.parent
                self.assertTrue(digest_dir.exists())

                config_agents.purge_staged_credentials(context)
                self.assertFalse(digest_dir.exists())
                # Idempotent: a second purge (nothing staged) does not raise.
                config_agents.purge_staged_credentials(context)

    def test_staging_refuses_symlinked_credential_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            symlink = root / "link"
            symlink.symlink_to(root)

            with self.assertRaises(RuntimeError) as raised:
                config_agents._ensure_private_dir(symlink / "agent")

        self.assertIn("symlinked credential directory", str(raised.exception))

    def test_render_refuses_symlinked_project_config_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = root / ".project-sandbox"
            context.mkdir()
            external = root / "external"
            external.mkdir()
            (context / "claude").symlink_to(external, target_is_directory=True)

            with self.assertRaises(RuntimeError) as raised:
                config_agents.render(context)

            self.assertIn("symlinked project config path", str(raised.exception))
            # Nothing was written through the link.
            self.assertEqual(list(external.iterdir()), [])

    def test_stale_cleanup_refuses_symlinked_project_config_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = root / ".project-sandbox"
            context.mkdir()
            external = root / "external"
            external.mkdir()
            victim = external / ".credentials.json"
            victim.write_text("host-secret\n", encoding="utf-8")
            (context / "claude").symlink_to(external, target_is_directory=True)

            with self.assertRaises(RuntimeError) as raised:
                config_agents._remove_stale_project_credentials(context)

            with self.assertRaises(RuntimeError):
                config_agents._remove_stale_project_agent_credentials(
                    context, "claude", None
                )

            self.assertIn("symlinked project config path", str(raised.exception))
            # The host credential file behind the link was not deleted.
            self.assertTrue(victim.exists())

    def test_invalid_claude_json_is_replaced_with_sanitized_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            context = root / ".project-sandbox"
            home.mkdir()
            (home / ".claude.json").write_text("{not json\n", encoding="utf-8")

            with _credentials_root(root):
                staged_dir = config_agents.sync_credentials(context, home=home)["claude"]

            state = json.loads((staged_dir / ".claude.json").read_text(encoding="utf-8"))
            self.assertEqual(state["permissions"]["defaultMode"], "bypassPermissions")
            self.assertEqual(state["installMethod"], "npm")
            self.assertNotIn("not json", json.dumps(state))

    def test_macos_keychain_failures_do_not_stage_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            with (
                patch("project_sandbox.config_agents.sys.platform", "darwin"),
                patch("project_sandbox.config_agents.shutil.which", return_value="/usr/bin/security"),
                patch(
                    "project_sandbox.config_agents.subprocess.run",
                    side_effect=subprocess.TimeoutExpired(["security"], timeout=2),
                ),
            ):
                self.assertFalse(config_agents._stage_macos_keychain_credentials(out_dir))

            self.assertFalse((out_dir / ".credentials.json").exists())

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
            self.assertIn("@fission-ai/openspec@1.4.1", text)

    def test_dockerfile_renderer_overwrites_existing_agent_uid_setup(self) -> None:
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
            self.assertEqual(warnings, [])

    def test_dockerfile_renderer_overwrites_existing_jj_install(self) -> None:
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
            self.assertEqual(warnings, [])

    def test_dockerfile_renderer_overwrites_missing_config_mount_targets(self) -> None:
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
            self.assertEqual(warnings, [])

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

    def test_render_dockerignore_writes_scoped_files_for_project_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            context = project / ".project-sandbox"
            context.mkdir()

            dockerfile.render_dockerignore(context, build_context=project)

            ignore = context / "Dockerfile.dockerignore"
            self.assertTrue(ignore.exists())
            patterns = ignore.read_text(encoding="utf-8").splitlines()
            self.assertIn(".venv", patterns)
            self.assertIn("node_modules", patterns)
            # .git must stay in the context: git version backends read it during
            # the in-image project install.
            self.assertNotIn(".git", patterns)
            # Must not exclude the generated dir whose scripts are COPY'd in.
            self.assertNotIn(".project-sandbox", patterns)
            self.assertTrue(
                (context / "Dockerfile.devcontainer.dockerignore").exists()
            )

    def test_render_dockerignore_skips_when_context_is_sandbox(self) -> None:
        # When the build context is just .project-sandbox/ there is nothing heavy
        # to trim, so no .dockerignore is written.
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp) / ".project-sandbox"
            context.mkdir()

            dockerfile.render_dockerignore(context, build_context=context)

            self.assertFalse((context / "Dockerfile.dockerignore").exists())

    def test_render_dockerignore_respects_existing_root_dockerignore(self) -> None:
        # A user's root .dockerignore is authoritative; generating a
        # per-Dockerfile file would override (not merge with) it.
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            context = project / ".project-sandbox"
            context.mkdir()
            (project / ".dockerignore").write_text("secrets/\n", encoding="utf-8")

            dockerfile.render_dockerignore(context, build_context=project)

            self.assertFalse((context / "Dockerfile.dockerignore").exists())

    def test_render_does_not_emit_dockerignore_itself(self) -> None:
        # render() renders Dockerfiles only; the .dockerignore policy lives in the
        # caller (python-uv flow), never for base-image or custom --dockerfile.
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            context = project / ".project-sandbox"
            context.mkdir()

            dockerfile.render(
                context,
                base_image="python:3.12-slim",
                build_context=project,
                install_agents=("claude",),
            )

            self.assertFalse((context / "Dockerfile.dockerignore").exists())

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

    def test_dockerfile_source_rejects_mixed_user_setup_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            context = project / ".project-sandbox"
            context.mkdir()
            source = project / "Dockerfile"
            source.write_text(
                "FROM ubuntu:24.04\n"
                "RUN apt-get update && apt-get install -y curl && \\\n"
                "    useradd -m app && \\\n"
                "    make setup\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx:
                dockerfile.render(
                    context,
                    base_dockerfile=source,
                    build_context=project,
                    install_agents=("codex",),
                    warn=[].append,
                )
            message = str(ctx.exception)
            self.assertIn("useradd", message)
            self.assertIn("separate RUN", message)
            # The mixed block must not have been silently dropped to a file.
            self.assertFalse((context / "Dockerfile").exists())

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
            self.assertIn("/project-sandbox-secrets/codex/auth.json", text)
            self.assertIn("/project-sandbox-secrets/opencode/.config/opencode", text)
            self.assertIn("/project-sandbox-secrets/opencode/.local/share/opencode", text)
            self.assertIn("/project-sandbox-secrets/opencode/.local/state/opencode", text)
            self.assertNotIn(".codex.host", text)
            self.assertNotIn("opencode.host", text)
            self.assertIn("sudo -n /usr/local/bin/project-sandbox-init-firewall", text)
            # Quiet mode suppresses the firewall banner but re-surfaces output on
            # failure (then aborts, since the firewall is the sandbox boundary).
            self.assertIn('"${PROJECT_SANDBOX_QUIET:-0}" = "1"', text)
            self.assertIn('printf \'%s\\n\' "$fw_out" >&2', text)
            self.assertNotIn("sudo chown", text)
            self.assertIn('jj config set --user user.name "$NAME"', text)
            self.assertIn('jj config set --user user.email "$EMAIL"', text)
            self.assertIn("claude-headless", text)
            self.assertIn('exec claude "$@"', text)
            self.assertIn("codex-headless", text)
            self.assertIn(
                'set -- --skip-git-repo-check --json --color never "$PROMPT"',
                text,
            )
            self.assertIn('exec codex exec "$@"', text)
            self.assertIn("opencode-headless", text)
            self.assertIn("bash-headless", text)
            self.assertIn('exec bash -lc "$PROMPT"', text)
            # UV_OFFLINE is set after the firewall block so uv works offline.
            self.assertIn("export UV_OFFLINE=1", text)

    def test_entrypoint_headless_supports_model_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            dockerfile.render_entrypoint(context)
            text = (context / "entrypoint.sh").read_text(encoding="utf-8")
            # Claude headless builds args with set -- and conditionally prepends flags.
            self.assertIn("PROJECT_SANDBOX_MODEL", text)
            self.assertIn(
                'set -- -p "$PROMPT" --output-format stream-json --verbose --dangerously-skip-permissions',
                text,
            )
            self.assertIn('set -- --model "$PROJECT_SANDBOX_MODEL" "$@"', text)
            self.assertIn('exec claude "$@"', text)
            # Codex builds args with set -- so model and config overrides compose.
            self.assertIn(
                'set -- --model "$PROJECT_SANDBOX_MODEL" "$@"',
                text,
            )
            self.assertIn('exec codex exec "$@"', text)
            self.assertIn(
                'set -- --model "$PROJECT_SANDBOX_MODEL" "$@"',
                text,
            )
            self.assertIn(
                'set -- --skip-git-repo-check --json --color never "$PROMPT"',
                text,
            )
            self.assertIn('exec opencode run "$@"', text)

    def test_entrypoint_headless_supports_effort_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            dockerfile.render_entrypoint(context)
            text = (context / "entrypoint.sh").read_text(encoding="utf-8")
            self.assertIn("PROJECT_SANDBOX_EFFORT", text)
            self.assertIn('set -- --effort "$PROJECT_SANDBOX_EFFORT" "$@"', text)
            self.assertIn(
                'set -- -c "model_reasoning_effort=\\"$PROJECT_SANDBOX_EFFORT\\"" "$@"',
                text,
            )
            self.assertIn('set -- --variant "$PROJECT_SANDBOX_EFFORT" "$@"', text)

    def test_entrypoint_interactive_supports_model_and_effort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            dockerfile.render_entrypoint(context)
            text = (context / "entrypoint.sh").read_text(encoding="utf-8")
            # Interactive agent branches consume the same env vars as headless and
            # turn them into each agent's own model/effort flags before exec.
            self.assertIn('log_project_sandbox_agent_command claude "$@"', text)
            self.assertIn('log_project_sandbox_agent_command codex "$@"', text)
            self.assertIn('log_project_sandbox_agent_command opencode "$@"', text)
            self.assertIn('exec codex "$@"', text)
            self.assertIn('exec opencode "$@"', text)
            # The codex interactive branch uses -c for reasoning effort and the
            # opencode branch uses --variant, mirroring their headless forms.
            self.assertEqual(
                text.count(
                    'set -- -c "model_reasoning_effort=\\"$PROJECT_SANDBOX_EFFORT\\"" "$@"'
                ),
                2,
            )
            self.assertEqual(
                text.count('set -- --variant "$PROJECT_SANDBOX_EFFORT" "$@"'), 2
            )

    def test_entrypoint_headless_logs_agent_command_when_verbose(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            dockerfile.render_entrypoint(context)
            text = (context / "entrypoint.sh").read_text(encoding="utf-8")
            # The verbose echo is gated on PROJECT_SANDBOX_VERBOSE and reports the
            # resolved model/effort plus the full agent argv.
            self.assertIn("log_project_sandbox_agent_command() {", text)
            self.assertIn(
                'if [ "${PROJECT_SANDBOX_VERBOSE:-0}" != "1" ]; then', text
            )
            self.assertIn(
                "printf '  model:  %s\\n' \"${PROJECT_SANDBOX_MODEL:-(agent default)}\"",
                text,
            )
            self.assertIn(
                "printf '  effort: %s\\n' \"${PROJECT_SANDBOX_EFFORT:-(agent default)}\"",
                text,
            )
            # Each headless agent logs its resolved argv right before exec.
            self.assertIn('log_project_sandbox_agent_command claude "$@"', text)
            self.assertIn('log_project_sandbox_agent_command codex exec "$@"', text)
            self.assertIn('log_project_sandbox_agent_command opencode run "$@"', text)

    def test_entrypoint_renderer_overwrites_missing_jj_identity_setup(self) -> None:
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

    def test_entrypoint_renderer_overwrites_config_dir_claude_credentials(self) -> None:
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
            self.assertIn("/project-sandbox-secrets/codex/auth.json", text)
            self.assertIn("/project-sandbox-secrets/opencode/.config/opencode", text)
            self.assertIn("/project-sandbox-secrets/opencode/.local/share/opencode", text)
            self.assertIn("/project-sandbox-secrets/opencode/.local/state/opencode", text)
            self.assertNotIn(".codex.host", text)
            self.assertNotIn("opencode.host", text)
            self.assertIn('jj config set --user user.name "$NAME"', text)
            self.assertIn('jj config set --user user.email "$EMAIL"', text)

    def test_devcontainer_entrypoint_renderer_overwrites_config_dir_claude_credentials(
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

    def test_entrypoint_renderer_overwrites_stale_claude_config_dir_env(self) -> None:
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

    def test_devcontainer_entrypoint_overwrites_missing_jj_identity_setup(self) -> None:
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

    def test_firewall_render_writes_both_container_and_devcontainer_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            fw = firewall.render(context, extra_domains=[])
            container_text = (context / "init-firewall.sh").read_text(encoding="utf-8")
            devcontainer_text = (context / "init-firewall-devcontainer.sh").read_text(encoding="utf-8")

            self.assertEqual(fw, context / "init-firewall.sh")
            self.assertNotIn("HOST_GW4", container_text)
            self.assertNotIn("HOST_GW6", container_text)
            self.assertNotIn("Host gateway", container_text)
            self.assertIn("HOST_GW4", devcontainer_text)
            self.assertIn("HOST_GW6", devcontainer_text)
            self.assertIn("Host gateway", devcontainer_text)
            for text in (container_text, devcontainer_text):
                self.assertIn('"api.anthropic.com"', text)
                self.assertIn('"claude.ai"', text)

    def test_firewall_host_network_allows_gateway_not_interface_cidr(self) -> None:
        # Regression: the devcontainer (allow_host_network) variant must allow
        # only the default IPv4 gateway, not the entire interface CIDR, so peers
        # sharing the container subnet are not reachable. Mirrors the IPv6 path,
        # which already restricts to the gateway.
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            firewall.render(context, extra_domains=[])
            text = (context / "init-firewall-devcontainer.sh").read_text(
                encoding="utf-8"
            )
            # Must derive the gateway from the default route and allow only it.
            self.assertIn("HOST_GW4", text)
            self.assertIn('iptables -A OUTPUT -d "$HOST_GW4"', text)
            self.assertIn('iptables -A INPUT  -s "$HOST_GW4"', text)
            # Must NOT open the whole interface CIDR any more.
            self.assertNotIn("HOST_NET4", text)
            self.assertNotIn('-d "$HOST_NET4"', text)
            self.assertNotIn('-s "$HOST_NET4"', text)
            # The IPv4 narrowing should match the IPv6 gateway derivation style.
            self.assertIn("ip -4 route", text)
            self.assertIn("/default/ {print $3; exit}", text)

    def test_firewall_collects_all_resolvers_not_just_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            firewall.render(context, extra_domains=[])
            for name in ("init-firewall.sh", "init-firewall-devcontainer.sh"):
                text = (context / name).read_text(encoding="utf-8")
                # Must NOT use the single-resolver awk pattern
                self.assertNotIn("{print $2; exit}", text)
                # Must collect all nameservers via mapfile arrays
                self.assertIn("mapfile -t DNS4_LIST", text)
                self.assertIn("mapfile -t DNS6_LIST", text)
                # Fallback for empty IPv4 list (uses ${var+x} to avoid Jinja2 {# conflict)
                self.assertIn('DNS4_LIST=("127.0.0.11")', text)
                # ACCEPT rules must iterate over the list, not reference a scalar
                self.assertIn('for dns in "${DNS4_LIST[@]}"', text)
                self.assertIn('for dns6 in "${DNS6_LIST[@]}"', text)

    def test_firewall_narrows_icmpv6_to_required_types(self) -> None:
        # Regression: a blanket "ip6tables -p ipv6-icmp -j ACCEPT" on INPUT and
        # OUTPUT let arbitrary data ride ICMPv6 to any host. Only the specific
        # control types must be allowed, with neighbor/router discovery confined
        # to link-local scope.
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            firewall.render(context, extra_domains=[])
            for name in ("init-firewall.sh", "init-firewall-devcontainer.sh"):
                text = (context / name).read_text(encoding="utf-8")
                # No blanket ipv6-icmp accept on INPUT or OUTPUT.
                self.assertNotIn("-p ipv6-icmp -j ACCEPT", text)
                # Per-type matching is used instead.
                self.assertIn("--icmpv6-type", text)
                # Error / PMTU types are present.
                for icmp6_type in ("1", "2", "3", "4"):
                    self.assertIn(icmp6_type, text)
                # Neighbor / router discovery types are present...
                for icmp6_type in ("133", "134", "135", "136", "137"):
                    self.assertIn(icmp6_type, text)
                # ...and confined to link-local scope.
                self.assertIn("fe80::/10", text)
                self.assertIn("ff02::/16", text)

    def test_firewall_blocks_general_dns_after_preresolution(self) -> None:
        # The resolver must no longer be reachable for arbitrary names (which
        # enabled DNS-tunnel exfiltration). Allowlisted domains are pre-resolved
        # and pinned into /etc/hosts, then general outbound DNS is dropped.
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            firewall.render(context, extra_domains=[])
            for name in ("init-firewall.sh", "init-firewall-devcontainer.sh"):
                text = (context / name).read_text(encoding="utf-8")
                # No blanket DNS ACCEPT to/from the resolver any more.
                self.assertNotIn('--dport 53 -d "$dns" -j ACCEPT', text)
                self.assertNotIn('--dport 53 -d "$dns6" -j ACCEPT', text)
                self.assertNotIn('--sport 53 -s "$dns" -j ACCEPT', text)
                # Allowlisted names are pinned into /etc/hosts.
                self.assertIn("/etc/hosts", text)
                self.assertIn("project-sandbox-dns-pin", text)
                # General outbound DNS is dropped (both transports).
                self.assertIn("-p udp --dport 53 -j DROP", text)
                self.assertIn("-p tcp --dport 53 -j DROP", text)
                # The DROP must precede the allowlist ACCEPT so DNS is blocked
                # even toward an allowlisted address.
                self.assertLess(
                    text.index("--dport 53 -j DROP"),
                    text.index("--match-set allowed-ipv4 dst -j ACCEPT"),
                )

    def test_firewall_nat_restore_is_valid_and_non_fatal(self) -> None:
        # Regression: "iptables-restore --noflush -t nat" is invalid (-t is not
        # an iptables-restore option), so it tried to open a file named "nat"
        # and failed with "Can't open nat: No such file or directory". Rules must
        # be wrapped in *nat/COMMIT, and the restore must be non-fatal so a
        # limited nat table (apple/container) does not abort the firewall.
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            firewall.render(context, extra_domains=[])
            for name in ("init-firewall.sh", "init-firewall-devcontainer.sh"):
                text = (context / name).read_text(encoding="utf-8")
                self.assertNotIn("--noflush -t nat", text)
                self.assertIn(
                    "printf '*nat\\n%sCOMMIT\\n' \"$NAT4\" | "
                    "iptables-restore --noflush 2>/dev/null || true",
                    text,
                )
                # Only append real matches (no bare-newline padding that made
                # NAT4/NAT6 look non-empty and forced a restore on empty input).
                self.assertIn('[ -n "$match" ] && NAT4+="$match"', text)

    def test_firewall_rejects_unsafe_extra_domains(self) -> None:
        # Regression: extra domains are interpolated into a root-run Bash array,
        # so command substitutions, backticks, embedded quotes, or newlines must
        # be rejected before the firewall script is rendered.
        unsafe = [
            "$(touch /tmp/pwned)",
            "`touch /tmp/pwned`",
            'evil.com"; touch /tmp/pwned; "',
            "evil.com\nrm -rf /",
        ]
        for domain in unsafe:
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ValueError):
                    firewall.render(Path(tmp), extra_domains=[domain])

    def test_firewall_renders_valid_extra_domain_quoted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            firewall.render(context, extra_domains=["example.com"])
            for name in ("init-firewall.sh", "init-firewall-devcontainer.sh"):
                text = (context / name).read_text(encoding="utf-8")
                # Rendered as a shell-safe token inside the DOMAINS=( ... ) array.
                array = text.split("DOMAINS=(", 1)[1].split(")", 1)[0]
                self.assertIn("example.com", array)

    def test_firewall_allows_github_only_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            firewall.render(context, extra_domains=[], allow_github=True)
            for name in ("init-firewall.sh", "init-firewall-devcontainer.sh"):
                text = (context / name).read_text(encoding="utf-8")
                self.assertIn("https://api.github.com/meta", text)
                self.assertIn('"github.com"', text)
                self.assertIn('"api.github.com"', text)
                self.assertIn('"raw.githubusercontent.com"', text)
                self.assertIn('"objects.githubusercontent.com"', text)
                self.assertIn('"api.githubcopilot.com"', text)
                self.assertIn('"api.individual.githubcopilot.com"', text)
                self.assertIn('"api.business.githubcopilot.com"', text)
                self.assertIn('"api.enterprise.githubcopilot.com"', text)
                self.assertIn('"copilot-proxy.githubusercontent.com"', text)
                self.assertIn('"origin-tracker.githubusercontent.com"', text)
                self.assertIn('"copilot-telemetry.githubusercontent.com"', text)
                self.assertIn('"collector.github.com"', text)
                self.assertIn('"default.exp-tas.com"', text)

    def test_render_returns_all_four_config_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            cfg = config_agents.render(context)
            self.assertEqual(cfg["claude"], context / "claude" / "settings.json")
            self.assertEqual(cfg["claude-devcontainer"], context / "claude-devcontainer" / "settings.json")
            self.assertEqual(cfg["codex"], context / "codex" / "config.toml")
            self.assertEqual(cfg["codex-devcontainer"], context / "codex-devcontainer" / "config.toml")

    def test_render_claude_devcontainer_uses_auto_permission_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            cfg = config_agents.render(context)
            out = cfg["claude-devcontainer"]
            settings = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(settings["permissions"]["defaultMode"], "auto")
            self.assertNotIn("bypassPermissions", out.read_text(encoding="utf-8"))

    def test_render_claude_container_uses_bypass_permission_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            out = config_agents.render(context)["claude"]
            settings = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(settings["permissions"]["defaultMode"], "bypassPermissions")

    def test_render_codex_devcontainer_uses_on_request_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            out = config_agents.render(context)["codex-devcontainer"]
            self.assertIn('approval_policy = "on-request"', out.read_text(encoding="utf-8"))

    def test_render_codex_container_uses_never_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            out = config_agents.render(context)["codex"]
            self.assertIn('approval_policy = "never"', out.read_text(encoding="utf-8"))

    def test_dockerfile_source_warns_on_alpine_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            context = project / ".project-sandbox"
            context.mkdir()
            source = project / "Dockerfile"
            source.write_text("FROM alpine:3.19\nRUN echo hello\n", encoding="utf-8")
            warnings: list[str] = []
            dockerfile.render(
                context, base_dockerfile=source, build_context=project, warn=warnings.append
            )
            self.assertEqual(len(warnings), 1)
            self.assertIn("alpine", warnings[0])
            self.assertIn("apt-get", warnings[0])

    def test_dockerfile_source_warns_on_alpine_base_with_platform_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            context = project / ".project-sandbox"
            context.mkdir()
            source = project / "Dockerfile"
            source.write_text(
                "FROM --platform=$BUILDPLATFORM alpine:3.19\nRUN echo hello\n",
                encoding="utf-8",
            )
            warnings: list[str] = []
            dockerfile.render(
                context, base_dockerfile=source, build_context=project, warn=warnings.append
            )
            self.assertEqual(len(warnings), 1)
            self.assertIn("alpine", warnings[0])
            self.assertIn("apt-get", warnings[0])

    def test_dockerfile_source_warns_on_distroless_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            context = project / ".project-sandbox"
            context.mkdir()
            source = project / "Dockerfile"
            source.write_text(
                "FROM gcr.io/distroless/python3:latest\nRUN echo hello\n", encoding="utf-8"
            )
            warnings: list[str] = []
            dockerfile.render(
                context, base_dockerfile=source, build_context=project, warn=warnings.append
            )
            self.assertEqual(len(warnings), 1)
            self.assertIn("distroless", warnings[0])
            self.assertIn("apt-get", warnings[0])

    def test_dockerfile_source_no_warning_for_debian_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            context = project / ".project-sandbox"
            context.mkdir()
            source = project / "Dockerfile"
            source.write_text("FROM debian:bookworm-slim\nRUN echo hello\n", encoding="utf-8")
            warnings: list[str] = []
            dockerfile.render(
                context, base_dockerfile=source, build_context=project, warn=warnings.append
            )
            self.assertEqual(warnings, [])

    def test_dockerfile_source_warns_on_workdir_and_uv_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            context = project / ".project-sandbox"
            context.mkdir()
            source = project / "Dockerfile"
            source.write_text(
                "FROM python:3.11-slim\nWORKDIR /app\nRUN uv sync --frozen\n",
                encoding="utf-8",
            )
            warnings: list[str] = []
            dockerfile.render(
                context, base_dockerfile=source, build_context=project, warn=warnings.append
            )
            self.assertEqual(len(warnings), 1)
            self.assertIn("/app", warnings[0])
            self.assertIn("/workspace", warnings[0])

    def test_dockerfile_source_warns_on_workdir_and_pip_install_dot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            context = project / ".project-sandbox"
            context.mkdir()
            source = project / "Dockerfile"
            source.write_text(
                "FROM python:3.11-slim\nWORKDIR /code\nRUN pip install -e .\n",
                encoding="utf-8",
            )
            warnings: list[str] = []
            dockerfile.render(
                context, base_dockerfile=source, build_context=project, warn=warnings.append
            )
            self.assertEqual(len(warnings), 1)
            self.assertIn("/code", warnings[0])

    def test_dockerfile_source_warns_on_workdir_and_npm_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            context = project / ".project-sandbox"
            context.mkdir()
            source = project / "Dockerfile"
            source.write_text(
                "FROM node:20\nWORKDIR /app\nRUN npm install\n", encoding="utf-8"
            )
            warnings: list[str] = []
            dockerfile.render(
                context, base_dockerfile=source, build_context=project, warn=warnings.append
            )
            self.assertEqual(len(warnings), 1)
            self.assertIn("/workspace", warnings[0])

    def test_dockerfile_source_warns_on_workdir_and_npm_ci(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            context = project / ".project-sandbox"
            context.mkdir()
            source = project / "Dockerfile"
            source.write_text(
                "FROM node:20\nWORKDIR /app\nRUN npm ci\n", encoding="utf-8"
            )
            warnings: list[str] = []
            dockerfile.render(
                context, base_dockerfile=source, build_context=project, warn=warnings.append
            )
            self.assertEqual(len(warnings), 1)
            self.assertIn("/workspace", warnings[0])

    def test_dockerfile_source_no_warning_for_global_npm_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            context = project / ".project-sandbox"
            context.mkdir()
            source = project / "Dockerfile"
            source.write_text(
                "FROM node:20\nWORKDIR /app\nRUN npm install -g @anthropic-ai/claude-code\n",
                encoding="utf-8",
            )
            warnings: list[str] = []
            dockerfile.render(
                context, base_dockerfile=source, build_context=project, warn=warnings.append
            )
            self.assertEqual(warnings, [])

    def test_dockerfile_source_no_warning_when_workdir_is_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            context = project / ".project-sandbox"
            context.mkdir()
            source = project / "Dockerfile"
            source.write_text(
                "FROM python:3.11-slim\nWORKDIR /workspace\nRUN uv sync --frozen\n",
                encoding="utf-8",
            )
            warnings: list[str] = []
            dockerfile.render(
                context, base_dockerfile=source, build_context=project, warn=warnings.append
            )
            self.assertEqual(warnings, [])

    def test_dockerfile_source_multiple_warnings_combined(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            context = project / ".project-sandbox"
            context.mkdir()
            source = project / "Dockerfile"
            source.write_text(
                "FROM alpine:3.19\nWORKDIR /app\nRUN uv sync --frozen\n", encoding="utf-8"
            )
            warnings: list[str] = []
            dockerfile.render(
                context, base_dockerfile=source, build_context=project, warn=warnings.append
            )
            self.assertEqual(len(warnings), 2)
            self.assertTrue(any("apt-get" in w for w in warnings))
            self.assertTrue(any("/workspace" in w for w in warnings))

    def test_dockerfile_source_warnings_available_in_dry_run_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "Dockerfile"
            source.write_text(
                "FROM alpine:3.19\nWORKDIR /app\nRUN uv sync\n", encoding="utf-8"
            )
            warnings = dockerfile.source_warnings(source)
            self.assertEqual(len(warnings), 2)
            self.assertTrue(any("apt-get" in w for w in warnings))
            self.assertTrue(any("/workspace" in w for w in warnings))

    def test_sync_credentials_devcontainer_uses_auto_permission_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            context = root / ".project-sandbox"
            home.mkdir()
            with _credentials_root(root):
                staged_dir = config_agents.sync_credentials(context, home=home)["claude-devcontainer"]
            state = json.loads((staged_dir / ".claude.json").read_text(encoding="utf-8"))
            self.assertEqual(state["permissions"]["defaultMode"], "auto")
            self.assertNotIn("bypassPermissionsModeAccepted", state)
            self.assertNotIn("skipDangerousModePermissionPrompt", state.get("permissions", {}))
            self.assertEqual(staged_dir.stat().st_mode & 0o777, 0o700)

    def test_dockerfile_renderer_produces_separate_container_and_devcontainer_dockerfiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            context = project / ".project-sandbox"
            context.mkdir()
            source = project / "Dockerfile"
            source.write_text("FROM ubuntu:24.04\n", encoding="utf-8")
            dockerfile.render(
                context,
                base_dockerfile=source,
                build_context=project,
                install_agents=("claude",),
            )
            container_text = (context / "Dockerfile").read_text(encoding="utf-8")
            devcontainer_text = (context / "Dockerfile.devcontainer").read_text(encoding="utf-8")

            self.assertIn(
                "COPY .project-sandbox/init-firewall.sh /usr/local/bin/project-sandbox-init-firewall",
                container_text,
            )
            self.assertNotIn("init-firewall-devcontainer", container_text)
            self.assertIn(
                "COPY .project-sandbox/init-firewall-devcontainer.sh /usr/local/bin/project-sandbox-init-firewall",
                devcontainer_text,
            )
            self.assertNotIn("init-firewall.sh", devcontainer_text.replace("init-firewall-devcontainer.sh", ""))
            # Both Dockerfiles have the same binary name — only one sudoers entry each
            self.assertEqual(container_text.count("NOPASSWD"), 1)
            self.assertEqual(devcontainer_text.count("NOPASSWD"), 1)
            self.assertIn("NOPASSWD: /usr/local/bin/project-sandbox-init-firewall", container_text)
            self.assertIn("NOPASSWD: /usr/local/bin/project-sandbox-init-firewall", devcontainer_text)
