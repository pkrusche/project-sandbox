import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import config_agents, devcontainer
from project_sandbox.git_identity import GitIdentity

MASK_ROOT = "${localWorkspaceFolder}/.project-sandbox/workspace-mask"
HISTORY_SHELL = "${localWorkspaceFolder}/.project-sandbox/history/shell"
OFFLINE = {
    "UV_OFFLINE": "1",
    "CARGO_NET_OFFLINE": "true",
    "npm_config_offline": "true",
}


def _render(project: Path, **overrides) -> dict:
    kwargs = {
        "identity": GitIdentity("Ada", "ada@example.com"),
        "memory": "8g",
        "cpus": 4,
        "extra_mounts": [],
    }
    kwargs.update(overrides)
    devcontainer.render(project, **kwargs)
    return json.loads(
        (project / ".devcontainer" / "devcontainer.json").read_text(encoding="utf-8")
    )


class _ProjectTestCase(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name)
        (self.project / ".project-sandbox").mkdir()


class HostMemoryTests(_ProjectTestCase):
    def test_normalizes_units(self) -> None:
        self.assertEqual(devcontainer._host_memory("8g"), "8gb")
        self.assertEqual(devcontainer._host_memory("8gb"), "8gb")
        self.assertEqual(devcontainer._host_memory("512m"), "512mb")
        self.assertEqual(devcontainer._host_memory("512mb"), "512mb")

    def test_returns_none_for_empty_or_unrecognized(self) -> None:
        self.assertIsNone(devcontainer._host_memory(None))
        self.assertIsNone(devcontainer._host_memory(""))
        self.assertIsNone(devcontainer._host_memory("lots"))

    def test_limits_are_kept(self) -> None:
        spec = _render(self.project, memory="512m", cpus=2)

        self.assertIn("--memory=512m", spec["runArgs"])
        self.assertIn("--cpus=2", spec["runArgs"])
        self.assertEqual(spec["hostRequirements"], {"cpus": 2, "memory": "512mb"})


class NetworkTests(_ProjectTestCase):
    """Requirement: no network by default; an explicit, unfiltered opt-in."""

    def test_default_has_no_network_and_offline_variables(self) -> None:
        spec = _render(self.project)

        args = spec["runArgs"]
        self.assertIn("--network", args)
        self.assertEqual(args[args.index("--network") + 1], "none")
        for name, value in OFFLINE.items():
            self.assertEqual(spec["containerEnv"][name], value)

    def test_default_ignores_proxy_environment(self) -> None:
        spec = _render(
            self.project, proxy_environment={"HTTPS_PROXY": "http://proxy:3128"}
        )

        self.assertNotIn("HTTPS_PROXY", spec["containerEnv"])

    def test_runtime_mode_has_no_network_override_or_offline_variables(self) -> None:
        spec = _render(
            self.project,
            network=devcontainer.NETWORK_RUNTIME,
            proxy_environment={"HTTPS_PROXY": "http://proxy:3128"},
        )

        self.assertFalse(any(arg.startswith("--network") for arg in spec["runArgs"]))
        for name in OFFLINE:
            self.assertNotIn(name, spec["containerEnv"])
        self.assertEqual(spec["containerEnv"]["HTTPS_PROXY"], "http://proxy:3128")

    def test_unknown_network_mode_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _render(self.project, network="bridge")


class PrivilegeTests(_ProjectTestCase):
    """Requirement: the devcontainer runs without elevated privileges."""

    def test_no_capabilities_sudo_or_firewall_in_either_mode(self) -> None:
        for network in devcontainer.NETWORK_MODES:
            with self.subTest(network=network):
                spec = _render(self.project, network=network)

                self.assertFalse(
                    any(arg.startswith("--cap-add") for arg in spec["runArgs"])
                )
                self.assertNotIn("sudo", spec["postStartCommand"])
                self.assertNotIn("firewall", spec["postStartCommand"])
                self.assertEqual(
                    spec["postStartCommand"],
                    "/usr/local/bin/project-sandbox-devcontainer-init",
                )
                self.assertEqual(spec["remoteUser"], "agent")


class NoAgentTests(_ProjectTestCase):
    """Requirement: the devcontainer contains no agents or agent credentials."""

    def test_no_credentials_configs_or_claude_history(self) -> None:
        spec = _render(self.project)
        text = json.dumps(spec)

        for forbidden in (
            "/project-sandbox-secrets",
            "/project-sandbox-config",
            str(config_agents.CREDENTIALS_ROOT / "project-sandbox-"),
            str(config_agents.credentials_dir(self.project / ".project-sandbox")),
            "claude_projects",
            "/home/agent/.claude",
        ):
            self.assertNotIn(forbidden, text)

    def test_no_agent_environment(self) -> None:
        spec = _render(self.project)

        for name in spec["containerEnv"]:
            self.assertFalse(
                name.startswith(("CLAUDE_", "CODEX_", "PI_", "OPENSPEC_")), name
            )
        self.assertEqual(
            set(spec["containerEnv"]),
            {"NODE_OPTIONS", "HISTFILE", *OFFLINE},
        )

    def test_no_agent_extensions(self) -> None:
        spec = _render(self.project)

        extensions = spec["customizations"]["vscode"].get("extensions", [])
        self.assertFalse(
            [e for e in extensions if "claude" in e.lower() or "codex" in e.lower()]
        )

    def test_stale_agent_and_firewall_links_are_removed(self) -> None:
        dc_dir = self.project / ".devcontainer"
        dc_dir.mkdir()
        stale = (
            "init-firewall.sh",
            "claude",
            "claude-devcontainer",
            "codex",
            "codex-devcontainer",
        )
        for name in stale:
            (dc_dir / name).symlink_to(Path("../.project-sandbox") / name)
        (dc_dir / "notes.txt").write_text("user file\n", encoding="utf-8")

        _render(self.project)

        for name in stale:
            self.assertFalse((dc_dir / name).is_symlink(), name)
        self.assertTrue((dc_dir / "notes.txt").exists())
        link = dc_dir / "Dockerfile"
        self.assertTrue(link.is_symlink())
        self.assertEqual(
            link.readlink(), Path("../.project-sandbox/Dockerfile.devcontainer")
        )


class ConvenienceTests(_ProjectTestCase):
    """Requirement: developer conveniences are retained."""

    def test_identity_is_passed_as_remote_env(self) -> None:
        spec = _render(self.project)

        self.assertEqual(
            spec["remoteEnv"],
            {
                "PROJECT_SANDBOX_USER_NAME": "Ada",
                "PROJECT_SANDBOX_USER_EMAIL": "ada@example.com",
            },
        )

    def test_shell_history_is_persisted(self) -> None:
        spec = _render(self.project)

        self.assertIn(
            f"source={HISTORY_SHELL},target=/home/agent/.bash_history.d,type=bind",
            spec["mounts"],
        )
        self.assertEqual(
            spec["containerEnv"]["HISTFILE"], "/home/agent/.bash_history.d/bash_history"
        )
        self.assertTrue(
            (self.project / ".project-sandbox" / "history" / "shell").is_dir()
        )

    def test_build_uses_generated_devcontainer_dockerfile(self) -> None:
        spec = _render(self.project)

        self.assertEqual(
            spec["build"]["dockerfile"], "../.project-sandbox/Dockerfile.devcontainer"
        )
        self.assertEqual(spec["build"]["context"], "../.project-sandbox")

    def test_render_can_use_project_root_build_context(self) -> None:
        spec = _render(self.project, build_context=self.project)

        self.assertEqual(
            spec["build"]["dockerfile"], "../.project-sandbox/Dockerfile.devcontainer"
        )
        self.assertEqual(spec["build"]["context"], "..")

    def test_render_overwrites_existing_devcontainer_json(self) -> None:
        spec_path = self.project / ".devcontainer" / "devcontainer.json"
        spec_path.parent.mkdir()
        spec_path.write_text('{"old":true}\n', encoding="utf-8")

        spec = _render(self.project)

        self.assertEqual(spec["remoteUser"], "agent")
        self.assertNotIn("old", spec)


class MaskTests(_ProjectTestCase):
    """Requirement: devcontainer configuration is hidden inside the devcontainer."""

    def test_masks_come_after_user_mounts(self) -> None:
        custom = "source=/tmp/custom,target=/workspace/.project-sandbox,type=bind"

        spec = _render(self.project, extra_mounts=[custom])

        mounts = spec["mounts"]
        sandbox_mask = (
            f"source={MASK_ROOT},target=/workspace/.project-sandbox,type=bind,readonly"
        )
        devcontainer_mask = (
            f"source={MASK_ROOT},target=/workspace/.devcontainer,type=bind,readonly"
        )
        self.assertEqual(mounts[-2:], [sandbox_mask, devcontainer_mask])
        self.assertLess(mounts.index(custom), mounts.index(sandbox_mask))
        self.assertTrue((self.project / ".project-sandbox" / "workspace-mask").is_dir())

    def test_refuses_a_symlinked_devcontainer_directory(self) -> None:
        elsewhere = self.project / "elsewhere"
        elsewhere.mkdir()
        (self.project / ".devcontainer").symlink_to(elsewhere)

        with self.assertRaises(RuntimeError):
            _render(self.project)
        self.assertFalse((elsewhere / "devcontainer.json").exists())


class InitializeCommandTests(TestCase):
    def test_recreates_history_and_mask_sources_safely(self) -> None:
        # The bind sources live in the gitignored .project-sandbox and can be
        # missing at container-create time. initializeCommand runs on the host
        # first; its argv form must survive an apostrophe in the path.
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "ada's project"
            (project / ".project-sandbox").mkdir(parents=True)

            spec = _render(project)
            command = spec["initializeCommand"]

            self.assertEqual(command, ["mkdir", "-p", HISTORY_SHELL, MASK_ROOT])

            shutil.rmtree(project / ".project-sandbox")
            resolved = [
                arg.replace("${localWorkspaceFolder}", str(project)) for arg in command
            ]
            subprocess.run(resolved, check=True)

            self.assertTrue((project / ".project-sandbox/history/shell").is_dir())
            self.assertTrue((project / ".project-sandbox/workspace-mask").is_dir())


class InjectionTests(TestCase):
    def test_render_escapes_injection_in_strings(self) -> None:
        # Quotes, braces, and newlines in the project name, git identity, and an
        # extra --mount value must stay JSON string values: they must not close
        # a string and inject new devcontainer fields, nor corrupt the file.
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / 'pro"ject\n{"injected": true}'
            (project / ".project-sandbox").mkdir(parents=True)
            malicious_mount = (
                'source=/tmp/x,target=/x,type=bind","postStartCommand":"rm -rf /'
            )

            spec = _render(
                project,
                identity=GitIdentity(
                    'Ada"\n"injectedName":"x', 'a@b.com","injectedEmail":"x'
                ),
                extra_mounts=[malicious_mount],
            )

            self.assertEqual(spec["remoteUser"], "agent")
            self.assertEqual(spec["postStartCommand"].count("rm -rf /"), 0)
            self.assertNotIn("injected", spec)
            self.assertNotIn("injectedName", spec["remoteEnv"])
            self.assertNotIn("injectedEmail", spec["remoteEnv"])
            self.assertTrue(spec["name"].startswith('pro"ject\n{"injected": true}'))
            self.assertEqual(
                spec["remoteEnv"]["PROJECT_SANDBOX_USER_NAME"],
                'Ada"\n"injectedName":"x',
            )
            self.assertEqual(
                spec["remoteEnv"]["PROJECT_SANDBOX_USER_EMAIL"],
                'a@b.com","injectedEmail":"x',
            )
            self.assertIn(malicious_mount, spec["mounts"])


class GitignoreTests(TestCase):
    def test_history_dir_is_excluded_by_gitignore(self) -> None:
        from project_sandbox import cli

        with tempfile.TemporaryDirectory() as tmp:
            context_dir = Path(tmp)
            cli._write_project_sandbox_gitignore(context_dir)
            content = (context_dir / ".gitignore").read_text(encoding="utf-8")

            self.assertIn("*\n", content)
            self.assertNotIn("history/", content)
            for removed in (
                "init-firewall-devcontainer.sh",
                "claude-devcontainer",
                "codex-devcontainer",
            ):
                self.assertNotIn(removed, content)
