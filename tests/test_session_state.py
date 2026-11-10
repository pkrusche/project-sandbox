"""host-state-storage at the CLI: per-session staging, masks, mounts, logs.

Every launch here mocks the container runtime; the container "runs" inside the
mocked ``container_cli.run`` / ``session.run``, while the session's staging is
alive. Nested ``cli.main`` calls from inside a running session model two
sessions of one project running at the same time.
"""

import contextlib
import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import cli, paths, staging
from project_sandbox.dockerfile import PRIVILEGE_MODEL
from project_sandbox.git_identity import GitIdentity

MOUNT_RE = re.compile(r"source=([^,]+),target=([^,]+)")


def _mounts(cmd: list[str]) -> dict[str, str]:
    """``{target: source}`` for every ``--mount`` in ``cmd``."""
    found = {}
    for value in cmd:
        match = MOUNT_RE.search(value)
        if match and value.startswith("type=bind"):
            found[match.group(2)] = match.group(1)
    return found


def _staging_dir(cmd: list[str]) -> Path:
    """The session staging directory a container command mounts from."""
    return Path(_mounts(cmd)["/project-sandbox-config/claude"]).parent.parent


def _staging_entries() -> list[str]:
    root = paths.runtime_root_path() / "staging"
    return sorted(p.name for p in root.iterdir()) if root.exists() else []


class _Harness(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "README.md").write_text("# demo\n", encoding="utf-8")
        self.home = self.root / "home"
        (self.home / ".claude").mkdir(parents=True)
        (self.home / ".claude" / ".credentials.json").write_text(
            '{"token":"host"}\n', encoding="utf-8"
        )

    def _host_paths(self) -> dict[str, Path]:
        return {
            "claude": self.home / ".claude",
            "codex": self.home / ".codex",
            "opencode": self.home / ".config" / "opencode",
            "pi": self.home / ".pi" / "agent",
            "copilot": self.home / ".copilot",
        }

    @contextlib.contextmanager
    def _patched(self, *, run=None, session_run=None, build_image=None, out=None):
        real_sync = cli.config_agents.sync_credentials
        with (
            patch.object(
                cli, "read_identity", return_value=GitIdentity("Ada", "a@example.com")
            ),
            patch.object(
                cli.config_agents, "_agent_host_paths", return_value=self._host_paths()
            ),
            # Stage from the fake home, never the real keychain.
            patch.object(
                cli.config_agents,
                "sync_credentials",
                side_effect=lambda secrets, **kw: real_sync(
                    secrets, home=self.home, **kw
                ),
            ),
            patch.object(
                cli.container_cli,
                "select_runtime",
                return_value=cli.container_cli.DOCKER,
            ),
            patch.object(cli.container_cli, "ensure_system_started", return_value=0),
            patch.object(cli.container_cli, "host_build_identity", return_value=None),
            patch.object(cli.container_cli, "image_exists", return_value=True),
            patch.object(
                cli.container_cli, "image_label", return_value=PRIVILEGE_MODEL
            ),
            patch.object(
                cli.container_cli,
                "build_image",
                side_effect=build_image or (lambda **_: 0),
            ),
            patch.object(
                cli.container_cli, "run", side_effect=run or (lambda *a, **k: 0)
            ),
            patch.object(
                cli.session,
                "run",
                side_effect=session_run or self._write_log,
            ),
            contextlib.redirect_stdout(out or io.StringIO()),
        ):
            yield

    @staticmethod
    def _write_log(cmd, *, log_path, **_kwargs) -> int:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("done\n", encoding="utf-8")
        return 0

    def _main(self, *argv: str, project: Path | None = None) -> int:
        return cli.main([*argv, str(project or self.project), "python:3.12-slim"])


class SessionStagingTests(_Harness):
    """Each session has its own staging; it removes only its own."""

    def test_two_sessions_get_distinct_staging_and_remove_only_their_own(self) -> None:
        seen: dict[str, Path] = {}

        def inner_run(cmd, **_kwargs):
            seen["inner"] = _staging_dir(cmd)
            self.assertTrue(seen["inner"].is_dir())
            return 0

        def outer_run(cmd, **_kwargs):
            seen["outer"] = _staging_dir(cmd)
            with patch.object(cli.container_cli, "run", side_effect=inner_run):
                self.assertEqual(self._main("--no-build", "--agent", "bash"), 0)
            # The inner session is gone; this one's staging is intact.
            self.assertFalse(seen["inner"].exists())
            self.assertTrue((seen["outer"] / "secrets" / "claude").is_dir())
            return 0

        with self._patched(run=outer_run):
            self.assertEqual(self._main("--no-build", "--agent", "bash"), 0)

        self.assertNotEqual(seen["inner"], seen["outer"])
        self.assertFalse(seen["outer"].exists())
        self.assertEqual(_staging_entries(), [])
        # Staging is under the runtime root, never in the project.
        self.assertTrue(
            seen["outer"].is_relative_to(
                Path(os.path.realpath(paths.runtime_root_path()))
            )
        )
        self.assertEqual(
            sorted(p.name for p in self.project.iterdir()),
            [".devcontainer", "README.md"],
        )

    def test_concurrent_sessions_see_only_their_own_prompt_and_proxy_key(self) -> None:
        keys = iter([("key-A", "environment"), ("key-B", "environment")])
        seen: dict[str, tuple[str, str]] = {}

        def contents(cmd) -> tuple[str, str]:
            mounts = _mounts(cmd)
            prompt = (
                Path(mounts["/project-sandbox-prompt"]) / "prompt.txt"
            ).read_text()
            models = (
                Path(mounts["/project-sandbox-config/pi"]) / "models.json"
            ).read_text()
            return prompt, models

        def inner_run(cmd, *, log_path, **_kwargs):
            seen["B"] = contents(cmd)
            return self._write_log(cmd, log_path=log_path)

        def outer_run(cmd, *, log_path, **_kwargs):
            with patch.object(cli.session, "run", side_effect=inner_run):
                self.assertEqual(self._launch_pi("task B"), 0)
            # Still this session's own inputs after B ran and exited.
            seen["A"] = contents(cmd)
            return self._write_log(cmd, log_path=log_path)

        with (
            self._patched(session_run=outer_run),
            patch.object(
                cli.agent_proxy, "resolve_key", side_effect=lambda *_: next(keys)
            ),
            patch.object(cli.agent_proxy, "discover_models", return_value=["model"]),
        ):
            self.assertEqual(self._launch_pi("task A"), 0)

        self.assertEqual(seen["A"][0], "task A")
        self.assertIn("key-A", seen["A"][1])
        self.assertNotIn("key-B", seen["A"][1])
        self.assertEqual(seen["B"][0], "task B")
        self.assertIn("key-B", seen["B"][1])
        self.assertNotIn("key-A", seen["B"][1])

    def _launch_pi(self, prompt: str) -> int:
        with patch.object(
            cli.config_agents, "available_agents", return_value=("pi", "bash")
        ):
            return self._main(
                "--no-build",
                "--agent",
                "pi",
                "--agent-proxy",
                "http://127.0.0.1:4000/v1",
                "--model",
                "agent-proxy/model",
                "--prompt-text",
                prompt,
            )

    def test_staging_survives_another_session_exiting(self) -> None:
        """Session A exits while session B runs: B keeps its prompt and creds."""

        def b_run(cmd, *, log_path, **_kwargs):
            staging_dir = _staging_dir(cmd)
            with patch.object(cli.session, "run", side_effect=self._write_log):
                self.assertEqual(
                    self._main("--no-build", "--agent", "bash", "--prompt-text", "A"), 0
                )
            self.assertEqual((staging_dir / "prompt" / "prompt.txt").read_text(), "B")
            self.assertTrue(
                (staging_dir / "secrets" / "claude" / ".credentials.json").is_file()
            )
            return self._write_log(cmd, log_path=log_path)

        with self._patched(session_run=b_run):
            self.assertEqual(
                self._main("--no-build", "--agent", "bash", "--prompt-text", "B"), 0
            )

    def test_no_forward_credentials_beside_a_forwarding_session(self) -> None:
        def forwarding_run(cmd, **_kwargs):
            staged = _staging_dir(cmd) / "secrets" / "claude" / ".credentials.json"
            self.assertTrue(staged.is_file())
            with patch.object(cli.container_cli, "run", return_value=0) as inner:
                self.assertEqual(
                    self._main(
                        "--no-build", "--agent", "bash", "--no-forward-credentials"
                    ),
                    0,
                )
            self.assertNotIn(
                "/project-sandbox-secrets/", " ".join(inner.call_args.args[0])
            )
            self.assertTrue(staged.is_file())
            return 0

        with self._patched(run=forwarding_run):
            self.assertEqual(self._main("--no-build", "--agent", "bash"), 0)

    def test_launch_reaps_dead_sessions_and_keeps_live_ones(self) -> None:
        live = staging.SessionStaging.create("20260101T000000-111111111111")
        self.addCleanup(live.close)
        with paths.open_runtime_root() as root, root.subdir("staging") as st:
            # A session that died before it took its lease (or whose lease is free).
            st.subdir("20260101T000000-222222222222").close()

        with self._patched():
            self.assertEqual(self._main("--no-build", "--agent", "bash"), 0)

        self.assertEqual(_staging_entries(), ["20260101T000000-111111111111"])


class BuildInputTests(_Harness):
    def _mark_migrated(self) -> None:
        with paths.open_project_state(self.project) as state:
            state.write("migrated", "{}\n")

    def test_planted_in_project_inputs_are_never_read(self) -> None:
        self._mark_migrated()
        legacy = self.project / ".project-sandbox"
        legacy.mkdir()
        (legacy / "init-firewall.sh").write_text("PLANTED\n", encoding="utf-8")
        (legacy / "Dockerfile").write_text("FROM planted\n", encoding="utf-8")
        built: dict[str, str] = {}

        def build_image(**kwargs):
            context = kwargs["context_dir"]
            built["firewall"] = (context / "init-firewall.sh").read_text()
            built["dockerfile"] = Path(kwargs["dockerfile_path"]).read_text()
            built["context"] = str(kwargs["build_context"])
            return 0

        with (
            self._patched(build_image=build_image),
            patch.object(cli.container_cli, "image_exists", return_value=False),
        ):
            self.assertEqual(self._main("--agent", "bash"), 0)

        self.assertNotIn("PLANTED", built["firewall"])
        self.assertNotIn("planted", built["dockerfile"])
        self.assertNotIn(str(self.project), built["context"])
        self.assertEqual((legacy / "init-firewall.sh").read_text(), "PLANTED\n")

    def _python_uv_project(self) -> None:
        (self.project / "pyproject.toml").write_text("[project]\nname='d'\n")
        (self.project / "uv.lock").write_text("version = 1\n")

    def _build_python_uv(self) -> dict:
        built: dict = {}

        def build_image(**kwargs):
            dockerfile = Path(kwargs["dockerfile_path"])
            built["dockerfile_path"] = dockerfile
            built["names"] = sorted(p.name for p in dockerfile.parent.iterdir())
            return 0

        with (
            self._patched(build_image=build_image),
            patch.object(cli.container_cli, "image_exists", return_value=False),
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-secret"}),
        ):
            rc = cli.main(
                [
                    "--agent",
                    "bash",
                    "--python-uv",
                    "--no-forward-credentials",
                    "--api-key-env",
                    "ANTHROPIC_API_KEY",
                    str(self.project),
                ]
            )
        built["rc"] = rc
        return built

    def test_whole_project_copy_holds_only_secret_free_build_inputs(self) -> None:
        self._python_uv_project()
        built = self._build_python_uv()
        self.assertEqual(built["rc"], 0)
        self.assertEqual(
            built["dockerfile_path"],
            Path(os.path.realpath(self.project)) / ".project-sandbox" / "Dockerfile",
        )
        self.assertEqual(
            built["names"],
            [
                "Dockerfile",
                "Dockerfile.dockerignore",
                "Dockerfile.python-uv",
                "entrypoint.sh",
                "init-firewall.sh",
                "project-sandbox-devcontainer-init",
                "project-sandbox-init",
            ],
        )
        for path in (self.project / ".project-sandbox").iterdir():
            self.assertNotIn("sk-secret", path.read_text())

    def test_whole_project_copy_replaces_a_symlinked_leaf(self) -> None:
        self._python_uv_project()
        self._mark_migrated()
        victim = self.root / "victim.sh"
        victim.write_text("keep\n", encoding="utf-8")
        (self.project / ".project-sandbox").mkdir()
        (self.project / ".project-sandbox" / "entrypoint.sh").symlink_to(victim)

        self.assertEqual(self._build_python_uv()["rc"], 0)

        self.assertEqual(victim.read_text(), "keep\n")
        copied = self.project / ".project-sandbox" / "entrypoint.sh"
        self.assertFalse(copied.is_symlink())
        self.assertIn("project-sandbox-run", copied.read_text())

    def test_whole_project_copy_refuses_a_symlinked_directory(self) -> None:
        self._python_uv_project()
        self._mark_migrated()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (self.project / ".project-sandbox").symlink_to(elsewhere)

        with self.assertRaises(SystemExit) as raised:
            self._build_python_uv()

        self.assertIn(".project-sandbox", str(raised.exception))
        self.assertEqual(list(elsewhere.iterdir()), [])


class MountSourceTests(_Harness):
    def test_final_mount_list_uses_only_private_sources(self) -> None:
        # A legacy tree an earlier version (or a repository) left behind, with
        # links to an outside directory where history and the mask used to be.
        outside = self.root / "outside"
        outside.mkdir()
        legacy = self.project / ".project-sandbox"
        (legacy / "history").mkdir(parents=True)
        (legacy / "history" / "shell").symlink_to(outside)
        (legacy / "workspace-mask").symlink_to(outside)
        with paths.open_project_state(self.project) as state:
            state.write("migrated", "{}\n")  # keep the legacy tree in place
        seen: dict = {}

        def run(cmd, **_kwargs):
            seen["mounts"] = _mounts(cmd)
            # While the session runs, every source exists as checked below.
            seen["stat"] = {
                source: (os.path.islink(source), os.lstat(source).st_uid)
                for source in seen["mounts"].values()
            }
            return 0

        with self._patched(run=run):
            self.assertEqual(self._main("--no-build", "--agent", "bash"), 0)

        mounts = seen["mounts"]
        roots = [
            os.path.realpath(paths.runtime_root_path()),
            os.path.realpath(paths.state_root_path()),
        ]
        workspace = os.path.realpath(self.project)
        for target, source in mounts.items():
            self.assertNotIn(str(outside), source)
            self.assertFalse(source.startswith(str(legacy)), source)
            if target == "/workspace":
                self.assertEqual(source, workspace)
                continue
            self.assertTrue(
                any(source.startswith(r + "/") for r in roots), (target, source)
            )
            self.assertEqual(seen["stat"][source], (False, os.geteuid()))
        # The legacy directory is masked by this session's empty directory.
        mask = mounts["/workspace/.project-sandbox"]
        self.assertEqual(mask, mounts["/workspace/.devcontainer"])
        self.assertEqual(Path(mask).name, "empty")
        self.assertTrue(mounts["/home/agent/.bash_history.d"].startswith(roots[1]))

    def test_symlinked_implicit_source_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = staging.SessionStaging.create(cli.observability.new_session_id())
            self.addCleanup(session.close)
            state = paths.open_project_state(self.project)
            self.addCleanup(state.close)
            layout = staging.SessionLayout.for_session(session, state)
            link = session.path / "link"
            link.symlink_to(tmp)
            with self.assertRaises(paths.UnsafePathError):
                layout.validate_mount_sources([link])
            with self.assertRaises(paths.UnsafePathError):
                layout.validate_mount_sources([Path(tmp)])
            layout.validate_mount_sources([layout.empty])


class DevcontainerMaskTests(_Harness):
    def _launch(self, project: Path | None = None) -> list[str]:
        seen: dict = {}

        def run(cmd, **_kwargs):
            seen["cmd"] = cmd
            return 0

        with self._patched(run=run):
            self.assertEqual(
                self._main("--no-build", "--agent", "bash", project=project), 0
            )
        return seen["cmd"]

    def test_absent_devcontainer_is_created_self_ignoring_and_masked(self) -> None:
        cmd = self._launch()
        dc = self.project / ".devcontainer"
        self.assertEqual([p.name for p in dc.iterdir()], [".gitignore"])
        self.assertEqual((dc / ".gitignore").read_text(), "*\n")
        self.assertIn("/workspace/.devcontainer", _mounts(cmd))
        self.assertIn(
            f"type=bind,source={_mounts(cmd)['/workspace/.devcontainer']},"
            "target=/workspace/.devcontainer,readonly",
            cmd,
        )

    def test_existing_devcontainer_is_masked_and_left_unchanged(self) -> None:
        dc = self.project / ".devcontainer"
        dc.mkdir()
        (dc / "devcontainer.json").write_text('{"user": true}\n', encoding="utf-8")
        cmd = self._launch()
        self.assertEqual(sorted(p.name for p in dc.iterdir()), ["devcontainer.json"])
        self.assertEqual((dc / "devcontainer.json").read_text(), '{"user": true}\n')
        self.assertIn("/workspace/.devcontainer", _mounts(cmd))

    def test_symlinked_devcontainer_is_refused_before_any_container(self) -> None:
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (self.project / ".devcontainer").symlink_to(elsewhere)
        with (
            self._patched(),
            patch.object(cli.container_cli, "run") as run,
            self.assertRaises(SystemExit) as raised,
        ):
            self._main("--no-build", "--agent", "bash")
        self.assertIn(str(self.project / ".devcontainer"), str(raised.exception))
        self.assertIn("writable from the sandbox", str(raised.exception))
        run.assert_not_called()
        self.assertEqual(list(elsewhere.iterdir()), [])


class LogTests(_Harness):
    def test_default_log_is_in_private_state_and_reported(self) -> None:
        out = io.StringIO()
        summary = self.root / "out" / "summary.json"
        with self._patched(out=out):
            self.assertEqual(
                self._main(
                    "--no-build",
                    "--agent",
                    "bash",
                    "--prompt-text",
                    "x",
                    "--json-summary",
                    str(summary),
                ),
                0,
            )
        payload = json.loads(summary.read_text())
        log_path = Path(payload["log_path"])
        sessions = (
            Path(os.path.realpath(paths.project_state_path(self.project))) / "sessions"
        )
        self.assertEqual(log_path.parent, sessions)
        self.assertTrue(log_path.is_file())
        # Printed when the run starts and when it ends.
        self.assertGreaterEqual(out.getvalue().count(str(log_path)), 2)
        self.assertFalse((self.project / ".project-sandbox").exists())


class LauncherContractTests(_Harness):
    """project-sandbox-tui's background run: its paths and fields stay put."""

    def test_background_run_outputs_and_session_record(self) -> None:
        launcher = self.root / "tui" / "runs" / "1"
        launcher.mkdir(parents=True)
        records = paths.state_root_path() / "sessions"
        seen: dict = {}

        def session_run(cmd, *, log_path, **_kwargs):
            (record,) = list(records.glob("*.json"))
            seen["record"] = json.loads(record.read_text())
            seen["record_name"] = record.name
            seen["records_dir"] = sorted(p.name for p in records.iterdir())
            log_path.write_text(
                '{"type":"result","subtype":"success","result":"ok"}\n',
                encoding="utf-8",
            )
            return 0

        with self._patched(session_run=session_run):
            rc = self._main(
                "--no-build",
                "--agent",
                "claude",
                "--prompt-text",
                "x",
                f"--log={launcher / 'run.log'}",
                f"--json-summary={launcher / 'run.json'}",
            )

        self.assertEqual(rc, 0)
        self.assertTrue((launcher / "run.log").is_file())
        self.assertTrue((launcher / "run.md").is_file())
        summary = json.loads((launcher / "run.json").read_text())
        self.assertEqual(summary["pid"], os.getpid())
        self.assertEqual(summary["exit_code"], 0)
        session_id = summary["session_id"]
        self.assertEqual(seen["record_name"], f"{session_id}.json")
        record = seen["record"]
        for field in (
            "session_id",
            "container_name",
            "project_path",
            "workspace_path",
            "agent",
            "status",
            "pid",
            "started_at",
            "runtime",
        ):
            self.assertIn(field, record)
        self.assertEqual(record["status"], "running")
        self.assertEqual(record["session_id"], session_id)
        # Only session records live there; staging is under the runtime root.
        self.assertEqual(seen["records_dir"], [f"{session_id}.json"])
