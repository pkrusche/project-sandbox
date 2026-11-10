"""Policy edits and live application share one lock across all runtimes."""

import contextlib
import threading
from unittest.mock import patch

from test_egress_sidecar import IMAGE, MUTATING, Base, FakeRelay, FakeRuntime, _policy

from project_sandbox import egress_policy, egress_sidecar
from project_sandbox.container_cli import PODMAN


class PolicyConcurrencyTests(Base):
    def setUp(self):
        super().setUp()
        self.threads = []
        self.errors = []
        self.releases = []
        self.path = self.tmp / "egress.toml"

    def tearDown(self):
        for event in self.releases:
            event.set()
        for thread in self.threads:
            thread.join(5)
        super().tearDown()

    def release_event(self):
        event = threading.Event()
        self.releases.append(event)
        return event

    def start(self, name, action):
        def work():
            try:
                action()
            except BaseException as exc:  # noqa: BLE001 -- propagate worker failures
                self.errors.append(exc)

        thread = threading.Thread(target=work, name=name, daemon=True)
        self.threads.append(thread)
        thread.start()
        return thread

    def join(self):
        for thread in self.threads:
            thread.join(5)
            self.assertFalse(thread.is_alive(), "Policy lock was not released")
        self.assertEqual(self.errors, [])

    def apply(self, operation, policy):
        return getattr(egress_sidecar, operation)(
            self.runtime, policy, image=IMAGE, root=self.root, deps=self.deps()
        )

    def test_concurrent_additions_and_removal_preserve_every_edit(self):
        for remove in (False, True):
            with self.subTest(remove=remove):
                self.check_edits(remove)

    def check_edits(self, remove):
        _policy(self.tmp, '# keep\n[egress]\nallow = ["revoked.example"]\n')
        alias = self.tmp / "alias.toml"
        alias.unlink(missing_ok=True)
        alias.symlink_to(self.path)
        replacing, attempted, second_replacing = [threading.Event() for _ in range(3)]
        release = self.release_event()
        original_replace = egress_policy._replace_atomically
        original_lock = egress_policy.policy_lock

        def replace(path, text):
            if threading.current_thread().name == "first":
                replacing.set()
                self.assertTrue(release.wait(5))
            else:
                second_replacing.set()
            original_replace(path, text)

        @contextlib.contextmanager
        def lock(path):
            if threading.current_thread().name == "second":
                attempted.set()
            with original_lock(path) as canonical:
                yield canonical

        with (
            patch.object(egress_policy, "_replace_atomically", side_effect=replace),
            patch.object(egress_policy, "policy_lock", side_effect=lock),
        ):
            self.start(
                "first",
                lambda: egress_policy.add_allowed_hosts(alias, ["first.example"]),
            )
            self.assertTrue(replacing.wait(5))
            edit = (
                egress_policy.remove_allowed_hosts
                if remove
                else egress_policy.add_allowed_hosts
            )
            hosts = ["revoked.example"] if remove else ["second.example"]
            self.start("second", lambda: edit(self.path, hosts))
            self.assertTrue(attempted.wait(5))
            self.assertFalse(second_replacing.wait(0.1))
            release.set()
            self.join()
        expected = (
            {"first.example"}
            if remove
            else {"revoked.example", "first.example", "second.example"}
        )
        self.assertEqual(set(egress_policy.load(self.path).allow), expected)
        self.assertTrue(alias.is_symlink())
        self.assertIn("# keep", self.path.read_text())

    def test_delayed_ensure_reload_and_restart_cannot_restore_revoked_host(self):
        for operation in ("ensure", "reload", "restart"):
            with self.subTest(operation=operation):
                self.check_delayed(operation)

    def check_delayed(self, operation):
        stale = _policy(self.tmp, '[egress]\nallow = ["revoked.example"]\n')
        self.apply("ensure", stale)
        ready = threading.Event()
        release = self.release_event()

        def delayed():
            ready.set()
            self.assertTrue(release.wait(5))
            self.apply(operation, stale)

        self.start("delayed", delayed)
        self.assertTrue(ready.wait(5))
        fresh, _ = egress_policy.remove_allowed_hosts(self.path, ["revoked.example"])
        self.apply("reload", fresh)
        release.set()
        self.join()
        conf = (self.lay.sidecar / "squid.conf").read_text()
        self.assertNotIn("revoked.example", conf)
        self.assertEqual(
            self.apply("ensure", stale).state["reload_fingerprint"],
            egress_sidecar.reload_fingerprint(conf),
        )

    def test_edits_wait_until_in_flight_application_completes(self):
        initial = _policy(self.tmp)
        self.apply("ensure", initial)
        allowed, _ = egress_policy.add_allowed_hosts(self.path, ["revoked.example"])
        reloading, attempted, edited = [threading.Event() for _ in range(3)]
        release = self.release_event()
        original_lock = egress_policy.policy_lock

        def runner(argv):
            if self.fake.kind(argv) == "reload":
                reloading.set()
                self.assertTrue(release.wait(5))
            return self.fake(argv)

        @contextlib.contextmanager
        def lock(path):
            if threading.current_thread().name == "editor":
                attempted.set()
            with original_lock(path) as canonical:
                yield canonical

        def remove():
            fresh, _ = egress_policy.remove_allowed_hosts(
                self.path, ["revoked.example"]
            )
            edited.set()
            self.apply("reload", fresh)

        with patch.object(egress_policy, "policy_lock", side_effect=lock):
            self.start(
                "apply",
                lambda: egress_sidecar.reload(
                    self.runtime,
                    allowed,
                    image=IMAGE,
                    root=self.root,
                    deps=self.deps(runner=runner),
                ),
            )
            self.assertTrue(reloading.wait(5))
            self.start("editor", remove)
            self.assertTrue(attempted.wait(5))
            self.assertFalse(edited.wait(0.1))
            self.assertIn("revoked.example", egress_policy.load(self.path).allow)
            release.set()
            self.join()
        self.assertNotIn(
            "revoked.example", (self.lay.sidecar / "squid.conf").read_text()
        )

    def test_policy_application_is_serialized_across_runtimes(self):
        policy = _policy(self.tmp)
        applying, attempted, second_runtime_started = [
            threading.Event() for _ in range(3)
        ]
        release = self.release_event()
        podman = FakeRuntime(PODMAN)
        original_lock = egress_policy.policy_lock

        @contextlib.contextmanager
        def lock(path):
            if threading.current_thread().name == "podman":
                attempted.set()
            with original_lock(path) as canonical:
                yield canonical

        def docker_runner(argv):
            if self.fake.kind(argv) == "run":
                applying.set()
                self.assertTrue(release.wait(5))
            return self.fake(argv)

        def podman_runner(argv):
            second_runtime_started.set()
            return podman(argv)

        with patch.object(egress_policy, "policy_lock", side_effect=lock):
            self.start(
                "docker",
                lambda: egress_sidecar.ensure(
                    self.runtime,
                    policy,
                    image=IMAGE,
                    root=self.root,
                    deps=self.deps(runner=docker_runner),
                ),
            )
            self.assertTrue(applying.wait(5))
            self.start(
                "podman",
                lambda: egress_sidecar.ensure(
                    PODMAN,
                    policy,
                    image=IMAGE,
                    root=self.root,
                    deps=self.deps(runner=podman_runner, relay=FakeRelay()),
                ),
            )
            self.assertTrue(attempted.wait(5))
            self.assertFalse(second_runtime_started.wait(0.1))
            release.set()
            self.join()
        self.assertEqual(self.fake.kinds().count("run"), 1)
        self.assertEqual(podman.kinds().count("run"), 1)

    def test_missing_defaults_remember_source_and_pick_up_created_file(self):
        with patch.object(egress_policy, "default_policy_path", return_value=self.path):
            defaults = egress_policy.load_global()
        self.assertIsNone(defaults.path)
        egress_policy.add_allowed_hosts(self.path, ["created.example"])
        self.apply("ensure", defaults)
        self.assertIn("created.example", (self.lay.sidecar / "squid.conf").read_text())

    def test_deleted_policy_uses_defaults_instead_of_old_allowlist(self):
        stale = _policy(self.tmp, '[egress]\nallow = ["revoked.example"]\n')
        self.apply("ensure", stale)
        self.path.unlink()
        self.apply("reload", stale)
        self.assertNotIn(
            "revoked.example", (self.lay.sidecar / "squid.conf").read_text()
        )

    def test_invalid_current_policy_aborts_before_mutation_and_releases_lock(self):
        for operation in ("ensure", "reload", "restart"):
            with self.subTest(operation=operation):
                stale = _policy(self.tmp)
                self.apply("ensure", stale)
                self.path.write_text("[invalid TOML")
                self.fake.calls.clear()
                with self.assertRaises(egress_policy.EgressPolicyError):
                    self.apply(operation, stale)
                self.assertFalse(set(self.fake.kinds()) & MUTATING)
                self.path.write_text("[egress]\nallow = []\n")
                self.start(
                    "after-failure",
                    lambda: egress_policy.add_allowed_hosts(
                        self.path, ["valid.example"]
                    ),
                )
                self.join()

    def test_status_does_not_take_mutating_policy_lock(self):
        policy = _policy(self.tmp)
        self.apply("ensure", policy)
        with patch.object(
            egress_policy, "policy_lock", side_effect=AssertionError("lock")
        ):
            egress_sidecar.status(
                self.runtime, policy, image=IMAGE, root=self.root, deps=self.deps()
            )
