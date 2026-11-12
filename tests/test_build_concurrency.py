"""Exercise real CLI lock boundaries with a simulated runtime image store."""

import contextlib
import fcntl
import hashlib
import tempfile
import threading
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from runtime_inputs import mounted_source

from project_sandbox import build_cache, cli, container_cli, host_locks
from project_sandbox.git_identity import GitIdentity


class BuildConcurrencyTests(TestCase):
    def setUp(self):
        self.resources = contextlib.ExitStack()
        self.addCleanup(self.resources.close)
        self.root = Path(self.resources.enter_context(tempfile.TemporaryDirectory()))
        self.project = self.root / "project"
        self.project.mkdir()
        # Host lock files live in the system temp dir and the image lock is keyed
        # by runtime, so parallel test workers would contend for (and probe) the
        # same "/docker" image lock. Keep this test's locks private.
        locks = self.root / "locks"
        locks.mkdir()
        self.resources.enter_context(patch.object(tempfile, "tempdir", str(locks)))
        self.store = {}
        self.built = []
        self.launched = []
        self.errors = []
        self.threads = []
        self.releases = []
        self.addCleanup(self.finish_threads)
        for target, name, kwargs in (
            (cli, "read_identity", {"return_value": GitIdentity(None, None)}),
            (cli.config_agents, "_agent_host_paths", {"return_value": {}}),
            (container_cli, "select_runtime", {"return_value": container_cli.DOCKER}),
            (container_cli, "ensure_system_started", {"return_value": 0}),
            (
                container_cli,
                "image_exists",
                {"side_effect": lambda _rt, tag: tag in self.store},
            ),
            (
                container_cli,
                "image_identity",
                {"side_effect": lambda _rt, tag: self.store[tag]},
            ),
        ):
            self.resources.enter_context(patch.object(target, name, **kwargs))
        self.build = self.resources.enter_context(
            patch.object(container_cli, "build_image", side_effect=self.build_image)
        )
        self.run = self.resources.enter_context(
            patch.object(container_cli, "run", side_effect=self.run_image)
        )

    def finish_threads(self):
        for event in self.releases:
            event.set()
        for thread in self.threads:
            thread.join(10)
            self.assertFalse(
                thread.is_alive(), "CLI thread failed to release its locks"
            )

    def event(self):
        event = threading.Event()
        self.releases.append(event)
        return event

    def launch(self, project=None, *extra):
        # Bypass only signal installation, which Python forbids in worker threads.
        return cli._main(
            [
                "--agent",
                "bash",
                "--no-forward-credentials",
                "--no-verify-dockerfile",
                "--image-tag",
                "shared:latest",
                *extra,
                str(project or self.project),
                "python:3.12-slim",
            ]
        )

    def start(self, name, project=None, *extra):
        def work():
            try:
                self.assertEqual(self.launch(project, *extra), 0)
            except BaseException as exc:  # noqa: BLE001 -- report worker failures in the parent
                self.errors.append(exc)

        thread = threading.Thread(target=work, name=name, daemon=True)
        self.threads.append(thread)
        thread.start()
        return thread

    def build_image(self, **kwargs):
        context = kwargs["context_dir"]
        policy = (context / "init-firewall.sh").read_bytes()
        identity = "sha256:" + hashlib.sha256(policy).hexdigest()
        self.assertFalse((context / "api-keys.env").exists())
        self.assertFalse((context / "pi/models.json").exists())
        self.assertFalse((context / "opencode/opencode.json").exists())
        self.store[kwargs["image_tag"]] = identity
        self.built.append((context, policy, identity))
        return 0

    def run_image(self, argv, **_kwargs):
        if argv[1:3] == ["image", "tag"]:
            source, alias = argv[3:]
            self.store[alias] = self.store[source]
            return 0
        if argv[1:3] in (["image", "rm"], ["image", "delete"]):
            return 0 if self.store.pop(argv[3], None) else 1
        identity = next(arg for arg in argv if arg.startswith("sha256:"))
        # Like Docker's containerd image store: once no tag names an image it
        # is deleted, and `docker run <id>` fails with "No such image".
        if identity not in self.store.values():
            return 125
        self.launched.append((threading.current_thread().name, identity))
        return 0

    def assert_finished(self):
        self.finish_threads()
        self.assertEqual(self.errors, [])

    def test_context_is_locked_through_build_and_sessions_have_private_inputs(self):
        building, running, attempted, rendered = [threading.Event() for _ in range(4)]
        finish_build, finish_session = self.event(), self.event()
        original_lock = build_cache.build_lock
        original_render = cli.dockerfile.render
        context = self.project / ".project-sandbox"
        context.mkdir()
        for name in ("api-keys.env", "pi/models.json", "opencode/opencode.json"):
            path = context / name
            path.parent.mkdir(exist_ok=True)
            path.write_text("stale-secret")

        @contextlib.contextmanager
        def lock(path):
            if threading.current_thread().name == "second":
                attempted.set()
            with original_lock(path):
                yield

        def render(*args, **kwargs):
            if threading.current_thread().name == "second":
                rendered.set()
            return original_render(*args, **kwargs)

        def build(**kwargs):
            if threading.current_thread().name == "first":
                before = (context / "init-firewall.sh").read_bytes()
                building.set()
                self.assertTrue(finish_build.wait(10))
                self.assertEqual((context / "init-firewall.sh").read_bytes(), before)
            return self.build_image(**kwargs)

        def run(argv, **kwargs):
            if threading.current_thread().name == "first" and argv[1] == "run":
                inputs = mounted_source(argv, "/project-sandbox-config/claude").parent
                (inputs / "api-keys.env").write_text("SESSION_KEY=private")
                running.set()
                self.assertTrue(finish_session.wait(10))
                self.assertEqual(
                    (inputs / "api-keys.env").read_text(), "SESSION_KEY=private"
                )
            return self.run_image(argv, **kwargs)

        with (
            patch.object(build_cache, "build_lock", side_effect=lock),
            patch.object(cli.dockerfile, "render", side_effect=render),
        ):
            self.build.side_effect, self.run.side_effect = build, run
            self.start("first", None, "--extra-domain", "strict.example")
            self.assertTrue(building.wait(10))
            second = self.start("second", None, "--extra-domain", "permissive.example")
            self.assertTrue(attempted.wait(10))
            self.assertFalse(rendered.wait(0.1))
            finish_build.set()
            self.assertTrue(running.wait(10))
            self.assertTrue(rendered.wait(10))
            second.join(10)
            self.assertFalse(second.is_alive())
            finish_session.set()
            self.assert_finished()
        self.assertEqual(len(self.built), 2)
        self.assertIn(b"strict.example", self.built[0][1])
        self.assertNotIn(b"permissive.example", self.built[0][1])
        self.assertIn(b"permissive.example", self.built[1][1])
        self.assertEqual(
            dict(self.launched), {"first": self.built[0][2], "second": self.built[1][2]}
        )

    def test_shared_tag_builds_are_locked_and_launches_keep_selected_identity(self):
        other = self.root / "other"
        other.mkdir()
        building, attempted, selected, second_built = [
            threading.Event() for _ in range(4)
        ]
        finish_build, finish_selection = self.event(), self.event()
        original_lock, original_command = (
            build_cache.image_lock,
            cli._build_session_command,
        )

        @contextlib.contextmanager
        def lock(runtime):
            if threading.current_thread().name == "second":
                attempted.set()
            with original_lock(runtime):
                yield

        def build(**kwargs):
            if threading.current_thread().name == "first":
                building.set()
                self.assertTrue(finish_build.wait(10))
            result = self.build_image(**kwargs)
            if threading.current_thread().name == "second":
                second_built.set()
            return result

        def command(*args, **kwargs):
            if threading.current_thread().name == "first":
                selected.set()
                self.assertTrue(finish_selection.wait(10))
            return original_command(*args, **kwargs)

        with (
            patch.object(build_cache, "image_lock", side_effect=lock),
            patch.object(cli, "_build_session_command", side_effect=command),
        ):
            self.build.side_effect = build
            self.start("first", None, "--extra-domain", "strict.example")
            self.assertTrue(building.wait(10))
            second = self.start("second", other, "--extra-domain", "permissive.example")
            self.assertTrue(attempted.wait(10))
            self.assertFalse(second_built.wait(0.1))
            finish_build.set()
            self.assertTrue(selected.wait(10))
            second.join(10)
            self.assertFalse(second.is_alive())
            self.assertEqual(self.store["shared:latest"], self.built[1][2])
            finish_selection.set()
            self.assert_finished()
        launched = dict(self.launched)
        self.assertEqual(launched["first"], self.built[0][2])
        self.assertEqual(launched["second"], self.built[1][2])
        self.assertNotEqual(launched["first"], launched["second"])

    def test_cache_rebuilds_when_shared_tag_identity_changes(self):
        self.assertEqual(self.launch(), 0)
        self.assertEqual(self.launch(), 0)
        self.assertEqual(len(self.built), 1)
        self.store["shared:latest"] = "sha256:" + "f" * 64
        self.assertEqual(self.launch(), 0)
        self.assertEqual(len(self.built), 2)

    def test_cache_cannot_cross_runtimes(self):
        self.assertEqual(self.launch(), 0)
        with (
            patch.object(
                container_cli, "select_runtime", return_value=container_cli.PODMAN
            ),
            patch.object(
                cli.local_service_network, "uses_local_pasta", return_value=False
            ),
        ):
            self.assertEqual(self.launch(), 0)
        self.assertEqual(len(self.built), 2)

    def test_session_image_repository_is_reserved_before_writes(self):
        with self.assertRaisesRegex(SystemExit, "reserved"):
            self.launch(None, "--image-tag", "localhost/project-sandbox-session:chosen")
        self.assertEqual(list(self.project.iterdir()), [])

    def test_no_build_pins_existing_image(self):
        identity = "sha256:" + "a" * 64
        self.store["shared:latest"] = identity
        original = cli._build_session_command

        def replace_tag(*args, **kwargs):
            self.store["shared:latest"] = "sha256:" + "b" * 64
            return original(*args, **kwargs)

        # Regression: another launch moving the shared tag after the image lock
        # is released must not delete this session's image before it runs.
        with patch.object(cli, "_build_session_command", side_effect=replace_tag):
            self.assertEqual(self.launch(None, "--no-build"), 0)
        self.build.assert_not_called()
        self.assertEqual(self.launched[0][1], identity)
        self.assertEqual(
            [tag for tag in self.store if "project-sandbox-session" in tag], [],
            "the session alias outlived the session",
        )

    def test_apple_cli_pins_under_lock_and_retains_alias_through_run(self):
        aliases = []
        launched = []

        def run(argv, **_kwargs):
            if argv[1:3] == ["image", "tag"]:
                with host_locks.lock_path(
                    "image", Path("/apple-container")
                ).open() as handle:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                source, alias = argv[3:]
                self.store[alias] = self.store[source]
                aliases.append(alias)
            elif argv[1:3] == ["image", "delete"]:
                self.assertEqual(launched, [self.built[0][2]])
                del self.store[argv[3]]
            else:
                self.assertEqual(argv[1], "run")
                self.assertIn(aliases[0], argv)
                # Another project can now replace the public tag without
                # changing the alias this session launches.
                self.store["shared:latest"] = "sha256:" + "b" * 64
                launched.append(self.store[aliases[0]])
            return 0

        with patch.object(
            container_cli, "select_runtime", return_value=container_cli.APPLE_CONTAINER
        ):
            self.run.side_effect = run
            self.assertEqual(self.launch(), 0)
        self.assertNotIn(aliases[0], self.store)
        self.assertEqual(launched, [self.built[0][2]])

    def test_interrupted_workspace_lock_does_not_clean_another_session_inputs(self):
        other = self.root / "branch"
        other_context = other / ".project-sandbox"
        other_context.mkdir(parents=True)
        secret = other_context / "api-keys.env"
        secret.write_text("ACTIVE_SESSION_KEY=private")
        original_lock = build_cache.build_lock

        @contextlib.contextmanager
        def lock(context):
            if context == other_context:
                raise KeyboardInterrupt
            with original_lock(context):
                yield

        with (
            patch.object(cli, "_validate_worktree_project"),
            patch.object(cli, "_setup_worktree", return_value=(None, other)),
            patch.object(
                cli,
                "_resolve_build_source",
                return_value=("python:3.12-slim", None, other),
            ),
            patch.object(build_cache, "build_lock", side_effect=lock),
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.launch(None, "--python-uv", "--branch", "feature")
        self.assertEqual(secret.read_text(), "ACTIVE_SESSION_KEY=private")
        self.build.assert_not_called()

    def test_build_failure_or_identity_failure_releases_locks_without_launch(self):
        for fail_inspection in (False, True):
            with self.subTest(fail_inspection=fail_inspection):
                self.build.side_effect = None
                self.build.return_value = 0 if fail_inspection else 42
                with patch.object(
                    container_cli,
                    "image_identity",
                    side_effect=SystemExit("inspect failed"),
                ):
                    if fail_inspection:
                        with self.assertRaises(SystemExit):
                            self.launch()
                    else:
                        self.assertEqual(self.launch(), 42)
                self.run.assert_not_called()
                for kind, target in (
                    ("build", self.project / ".project-sandbox"),
                    ("image", Path("/docker")),
                ):
                    with host_locks.lock_path(kind, target).open() as handle:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_dry_run_does_not_lock_inspect_or_write(self):
        self.build.side_effect = None
        self.build.return_value = 0
        self.run.side_effect = None
        self.run.return_value = 0
        with (
            patch.object(build_cache, "build_lock", side_effect=AssertionError("lock")),
            patch.object(build_cache, "image_lock", side_effect=AssertionError("lock")),
            patch.object(
                container_cli, "image_identity", side_effect=AssertionError("inspect")
            ),
        ):
            self.assertEqual(self.launch(None, "--dry-run"), 0)
        self.assertEqual(list(self.project.iterdir()), [])
        self.assertTrue(self.build.call_args.kwargs["dry_run"])
        self.assertTrue(self.run.call_args.kwargs["dry_run"])
