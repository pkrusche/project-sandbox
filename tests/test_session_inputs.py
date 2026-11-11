import contextlib
import json
import os
import tempfile
import threading
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from runtime_inputs import mounted_source

from project_sandbox import (
    cli,
    config_agents,
    container_cli,
    egress_sidecar,
    implicit_mounts,
    mount_storage,
    observability,
    session_inputs,
)
from project_sandbox.git_identity import GitIdentity


class PrivateInputStorageTests(TestCase):
    def test_allocations_are_private_unique_and_clean_only_their_own_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "outside"
            outside.write_text("keep")
            with session_inputs.allocate() as first:
                self.assertEqual(first.stat().st_mode & 0o777, 0o700)
                (first / "secret").write_text("first")
                with self.assertRaises(KeyboardInterrupt):
                    with session_inputs.allocate() as second:
                        self.assertNotEqual(first, second)
                        (second / "link").symlink_to(outside)
                        raise KeyboardInterrupt
                self.assertFalse(second.exists())
                self.assertEqual((first / "secret").read_text(), "first")
                self.assertEqual(outside.read_text(), "keep")
            self.assertFalse(first.exists())

    def test_symlinked_or_public_storage_root_is_rejected(self):
        root = session_inputs.storage_root()
        root.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            root.symlink_to(tmp, target_is_directory=True)
            with self.assertRaises(OSError):
                with session_inputs.allocate():
                    self.fail("accepted symlinked storage")
            root.unlink()
        root.mkdir(mode=0o755)
        with self.assertRaises(ValueError):
            with session_inputs.allocate():
                self.fail("accepted public storage")

    def test_workspace_build_context_and_extra_mounts_cannot_expose_sessions(self):
        with tempfile.TemporaryDirectory() as tmp, session_inputs.allocate() as owned:
            project = Path(tmp)
            for source in (
                owned,
                owned / "credentials",
                owned.parent,
                owned.parent.parent,
            ):
                with self.subTest(source=source):
                    with self.assertRaises(ValueError):
                        implicit_mounts.validate_sources(
                            project,
                            extra_mounts=[
                                f"type=bind,source={source},target=/leak,readonly"
                            ],
                        )
                    with self.assertRaises(ValueError):
                        implicit_mounts.validate_sources(project, build_context=source)
                    with self.assertRaises(ValueError):
                        implicit_mounts.validate_sources(source)

    def test_credentials_cannot_escape_owned_staging_root(self):
        for name in ("", ".", "..", "../other"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                config_agents.credentials_dir(
                    Path("/project"), name, staging_root=Path("/owned")
                )


class SessionInputLifecycleTests(TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.project = self.root / "project"
        self.project.mkdir()
        self.home = self.root / "home"
        (self.home / ".claude").mkdir(parents=True)
        (self.home / ".claude/.credentials.json").write_text('{"token":"host-token"}')
        self.sync = config_agents.sync_credentials
        for target, name, kwargs in (
            (cli, "read_identity", {"return_value": GitIdentity(None, None)}),
            (
                config_agents,
                "available_agents",
                {"return_value": ("claude", "copilot", "pi", "bash")},
            ),
            (
                config_agents,
                "sync_credentials",
                {"side_effect": lambda *a, **kw: self.sync(*a, home=self.home, **kw)},
            ),
            (container_cli, "select_runtime", {"return_value": container_cli.DOCKER}),
            (container_cli, "ensure_system_started", {"return_value": 0}),
            (container_cli, "image_exists", {"return_value": True}),
            (container_cli, "image_identity", {"return_value": "sha256:" + "a" * 64}),
            (
                cli,
                "_resolve_copilot_token",
                {
                    "side_effect": lambda args, agent, host: (
                        "ghu_" + threading.current_thread().name
                        if agent == "copilot" and not args.no_forward_credentials
                        else None
                    )
                },
            ),
            (
                cli.agent_proxy,
                "resolve_key",
                {
                    "side_effect": lambda *a: (
                        threading.current_thread().name + "-key",
                        "environment",
                    )
                },
            ),
            (cli.agent_proxy, "discover_models", {"return_value": ["model"]}),
        ):
            self.stack.enter_context(patch.object(target, name, **kwargs))

    def launch(self, agent, prompt, *extra):
        return cli._main(
            [
                "--agent",
                agent,
                "--prompt-text",
                prompt,
                "--no-build",
                "--no-token-refresh",
                *extra,
                str(self.project),
                "python:3.12-slim",
            ]
        )

    def test_one_session_can_exit_while_another_waits_to_read_its_inputs(self):
        for mode in ("claude", "copilot", "proxy"):
            with self.subTest(mode=mode):
                self.check_overlapping(mode)

    def check_overlapping(self, mode):
        first_ready, release = threading.Event(), threading.Event()
        errors, paths, snapshots = [], {}, {}
        agent = "bash" if mode == "proxy" else mode
        flags = (
            ["--agent-proxy", "https://models.example/v1", "--model", "model"]
            if mode == "proxy"
            else []
        )

        def run(argv, **kwargs):
            name = threading.current_thread().name
            prompt_dir = mounted_source(argv, "/project-sandbox-prompt")
            inputs = mounted_source(argv, "/project-sandbox-config/claude").parent
            paths[name] = inputs
            self.assertEqual(inputs.parent, session_inputs.storage_root())
            self.assertFalse(inputs.is_relative_to(self.project))
            files = [prompt_dir / "prompt.txt"]
            if mode == "copilot":
                token = (
                    mounted_source(argv, "/project-sandbox-secrets/copilot") / "token"
                )
                self.assertEqual(token.read_text(), "ghu_" + name + "\n")
                files.append(token)
            elif mode == "proxy":
                models = (
                    mounted_source(argv, "/project-sandbox-config/pi") / "models.json"
                )
                self.assertEqual(
                    json.loads(models.read_text())["providers"]["agent-proxy"][
                        "apiKey"
                    ],
                    name + "-key",
                )
                files.append(models)
            elif name == "first":
                files.append(
                    mounted_source(argv, "/project-sandbox-secrets/claude")
                    / ".credentials.json"
                )
            else:
                self.assertNotIn("/project-sandbox-secrets/claude", " ".join(argv))
            snapshots[name] = {path: path.read_bytes() for path in files}
            self.assertEqual(files[0].read_text(), name + " prompt")
            if name == "first":
                first_ready.set()
                self.assertTrue(release.wait(10))
                for path, expected in snapshots[name].items():
                    self.assertEqual(path.read_bytes(), expected)
            return 0

        def worker(name):
            try:
                extra = (
                    ["--no-forward-credentials"]
                    if mode == "claude" and name == "second"
                    else []
                )
                self.assertEqual(
                    self.launch(agent, name + " prompt", *flags, *extra), 0
                )
            except BaseException as exc:  # noqa: BLE001 -- report worker failures
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(name,), name=name, daemon=True)
            for name in ("first", "second")
        ]
        with patch.object(cli.session, "run", side_effect=run):
            try:
                threads[0].start()
                self.assertTrue(first_ready.wait(10))
                threads[1].start()
                threads[1].join(10)
                self.assertFalse(
                    threads[1].is_alive(), "Second session waited for the first to exit"
                )
                self.assertEqual(errors, [])
                self.assertNotEqual(paths["first"], paths["second"])
                self.assertTrue(paths["first"].exists())
                self.assertFalse(paths["second"].exists())
                for path, expected in snapshots["first"].items():
                    self.assertEqual(path.read_bytes(), expected)
            finally:
                release.set()
                for thread in threads:
                    if thread.ident is not None:
                        thread.join(10)
        self.assertEqual(errors, [])
        self.assertFalse(paths["first"].exists())
        for path in (self.project / ".project-sandbox").rglob("*"):
            if path.is_file():
                self.assertNotIn(b"first-key", path.read_bytes())
                self.assertNotIn(b"first prompt", path.read_bytes())

    def test_failed_launch_cleans_private_inputs_and_preserves_devcontainer_credentials(
        self,
    ):
        persistent = self.sync(self.project / ".project-sandbox", home=self.home)
        original = (persistent["claude"] / ".credentials.json").read_bytes()
        seen = []

        def fail(argv, **kwargs):
            seen.append(mounted_source(argv, "/project-sandbox-prompt").parent)
            raise KeyboardInterrupt

        with patch.object(cli.session, "run", side_effect=fail):
            with self.assertRaises(KeyboardInterrupt):
                self.launch("claude", "private prompt", "--no-forward-credentials")
        self.assertEqual(
            (persistent["claude"] / ".credentials.json").read_bytes(), original
        )
        self.assertFalse(seen[0].exists())

    def test_dry_run_does_not_allocate_private_inputs(self):
        with patch.object(
            session_inputs, "allocate", side_effect=AssertionError("allocated")
        ):
            self.assertEqual(self.launch("claude", "secret prompt", "--dry-run"), 0)
        self.assertFalse(session_inputs.storage_root().exists())
        self.assertEqual(list(self.project.iterdir()), [])

    def test_prompt_file_mount_is_a_private_snapshot_and_is_cleaned(self):
        source = self.root / "request.txt"
        source.write_text("original request")
        snapshots = []

        def run(argv, **kwargs):
            staged = mounted_source(argv, "/project-sandbox-prompt") / source.name
            snapshots.append(staged)
            self.assertNotEqual(source, staged)
            self.assertEqual(staged.stat().st_mode & 0o777, 0o600)
            source.write_text("later request")
            self.assertEqual(staged.read_text(), "original request")
            return 0

        with patch.object(cli.session, "run", side_effect=run):
            self.assertEqual(
                cli._main(
                    [
                        "--agent",
                        "bash",
                        "--prompt",
                        str(source),
                        "--no-build",
                        "--no-forward-credentials",
                        str(self.project),
                        "python:3.12-slim",
                    ]
                ),
                0,
            )
        self.assertFalse(snapshots[0].exists())
        self.assertEqual(source.read_text(), "later request")

    def test_chroot_uses_session_owned_script_and_jail(self):
        roots = []

        def run(argv, **kwargs):
            script = next(Path(arg) for arg in argv if arg.endswith("/chroot-run.sh"))
            roots.append(script.parent)
            self.assertEqual(script.parent.parent, session_inputs.storage_root())
            self.assertIn(str(script.parent / "chroot-root"), argv)
            self.assertTrue(script.is_file())
            return 0

        with (
            patch.object(
                container_cli, "select_runtime", return_value=container_cli.CHROOT
            ),
            patch.object(cli.session, "run", side_effect=run),
        ):
            self.assertEqual(
                self.launch(
                    "bash", "true", "--runtime", "chroot", "--no-forward-credentials"
                ),
                0,
            )
        self.assertFalse(roots[0].exists())


class UmaskGroupWritableStateTests(TestCase):
    """Regression: under ``umask 002`` the first state write created
    ``$XDG_STATE_HOME`` and ``$XDG_STATE_HOME/project-sandbox`` group-writable
    (``Path.mkdir(parents=True, mode=0o700)`` only applies ``mode`` to the
    leaf), so every later session failed with "Unsafe ancestor of private
    mount storage"."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        old = os.umask(0o002)
        self.addCleanup(os.umask, old)

    def test_ensure_private_dirs_creates_every_missing_component_0700(self):
        existing = self.tmp / "existing"
        existing.mkdir(mode=0o755)
        os.chmod(existing, 0o755)
        leaf = existing / "a" / "b" / "c"
        self.assertEqual(mount_storage.ensure_private_dirs(leaf), leaf)
        for path in (existing / "a", existing / "a" / "b", leaf):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700, path)
        # Existing directories are never repaired or tightened.
        self.assertEqual(existing.stat().st_mode & 0o777, 0o755)
        mount_storage.ensure_private_dirs(leaf)  # idempotent

    def test_sidecar_state_then_session_inputs_on_fresh_state_home(self):
        state = self.tmp / "state"
        with patch.object(
            mount_storage,
            "storage_root",
            return_value=state / "project-sandbox" / "mounts",
        ):
            lay = egress_sidecar.layout(
                container_cli.DOCKER, root=state / "project-sandbox" / "egress"
            )
            egress_sidecar.write_state(lay, {"version": 1})
            for path in (state, state / "project-sandbox"):
                self.assertEqual(path.stat().st_mode & 0o022, 0, path)
            with session_inputs.allocate() as inputs:
                self.assertTrue(inputs.is_dir())

    def test_session_record_on_fresh_state_home_is_private(self):
        state = self.tmp / "state"
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(state)}):
            path = observability.state_dir() / "record.json"
            observability._write(path, {"ok": True})
        for directory in (state, state / "project-sandbox", path.parent):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700, directory)
