"""Regression coverage for security finding 3, including deterministic races."""

import contextlib
import hashlib
import io
import os
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from project_sandbox import (
    build_cache,
    chroot,
    cli,
    config_agents,
    devcontainer,
    dockerfile,
    firewall,
    generated_files,
    session,
)
from project_sandbox.git_identity import GitIdentity


class GeneratedFilesTests(TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.context = self.project / ".project-sandbox"
        self.context.mkdir()
        self.outside = self.root / "outside"
        self.outside.mkdir()
        self.victim = self.outside / "victim"
        self.victim.write_text("outside contents\n")
        self.victim.chmod(0o640)

    def assert_victim_unchanged(self) -> None:
        self.assertEqual(self.victim.read_text(), "outside contents\n")
        self.assertEqual(self.victim.stat().st_mode & 0o777, 0o640)

    def render_devcontainer(self) -> None:
        devcontainer.render(
            self.project,
            identity=GitIdentity("Test", "test@example.com"),
            firewall_enabled=True,
            memory=None,
            cpus=None,
            extra_mounts=[],
            forward_credentials=False,
        )

    def test_renderers_replace_leaf_links_without_writing_or_chmodding_target(
        self,
    ) -> None:
        certificate = b"test public certificate"
        cert_name = f"project-sandbox-ca-{hashlib.sha256(certificate).hexdigest()}.crt"
        cases = [
            (
                "Dockerfile",
                lambda: dockerfile.render(self.context, base_image="debian:bookworm"),
            ),
            (
                "Dockerfile.devcontainer",
                lambda: dockerfile.render(self.context, base_image="debian:bookworm"),
            ),
            ("entrypoint.sh", lambda: dockerfile.render_entrypoint(self.context)),
            (
                "project-sandbox-devcontainer-init",
                lambda: dockerfile.render_devcontainer_entrypoint(self.context),
            ),
            (
                "init-firewall.sh",
                lambda: firewall.render(self.context, extra_domains=[]),
            ),
            (
                "init-firewall-devcontainer.sh",
                lambda: firewall.render(self.context, extra_domains=[]),
            ),
            ("chroot-run.sh", lambda: chroot.render(self.context)),
            ("claude/settings.json", lambda: config_agents.render(self.context)),
            ("codex/config.toml", lambda: config_agents.render(self.context)),
            ("pi/settings.json", lambda: config_agents.render(self.context)),
            ("copilot/config.json", lambda: config_agents.render(self.context)),
            (".gitignore", lambda: cli._write_project_sandbox_gitignore(self.context)),
            (
                ".build-state.json",
                lambda: build_cache.write_state(
                    self.context,
                    image_tag="test",
                    fingerprint="test",
                    image_id="identity",
                ),
            ),
            (
                cert_name,
                lambda: dockerfile.render(
                    self.context,
                    base_image="debian:bookworm",
                    ca_certificates=(certificate,),
                ),
            ),
        ]
        for name, render in cases:
            for dangling in (False, True):
                with self.subTest(name=name, dangling=dangling):
                    path = self.context / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.unlink(missing_ok=True)
                    target = self.outside / "absent" if dangling else self.victim
                    path.symlink_to(target)
                    render()
                    self.assertFalse(path.is_symlink())
                    self.assertTrue(path.is_file())
                    self.assertFalse((self.outside / "absent").exists())
                    self.assert_victim_unchanged()

    def test_devcontainer_replaces_json_link_and_preserves_intentional_links(
        self,
    ) -> None:
        dc = self.project / ".devcontainer"
        dc.mkdir()
        (dc / "devcontainer.json").symlink_to(self.victim)
        (dc / "Dockerfile").symlink_to(self.victim)
        self.render_devcontainer()
        self.assertFalse((dc / "devcontainer.json").is_symlink())
        self.assertEqual(
            (dc / "Dockerfile").readlink(),
            Path("../.project-sandbox/Dockerfile.devcontainer"),
        )
        self.assert_victim_unchanged()

    def test_symlinked_managed_directories_are_rejected_before_generation(self) -> None:
        for name in (
            ".project-sandbox",
            ".devcontainer",
            ".project-sandbox/codex",
            ".project-sandbox/sessions",
        ):
            with self.subTest(name=name):
                path = self.project / name
                if path == self.context:
                    path.rmdir()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to(self.outside, target_is_directory=True)
                with self.assertRaises((OSError, RuntimeError)):
                    cli._validate_generated_dirs(self.project)
                self.assertEqual(list(self.outside.iterdir()), [self.victim])
                path.unlink()
                self.context.mkdir(exist_ok=True)

    def test_renderers_reject_symlinked_parent_even_without_cli_preflight(self) -> None:
        self.context.rmdir()
        self.context.symlink_to(self.outside, target_is_directory=True)
        for render in (
            lambda: dockerfile.render_entrypoint(self.context),
            lambda: firewall.render(self.context, extra_domains=[]),
            lambda: config_agents.render(self.context),
        ):
            with self.assertRaises((OSError, RuntimeError)):
                render()
        dc = self.project / ".devcontainer"
        dc.symlink_to(self.outside, target_is_directory=True)
        with self.assertRaises(OSError):
            self.render_devcontainer()
        self.assertEqual(list(self.outside.iterdir()), [self.victim])

    def test_gitignore_rejects_existing_and_dangling_links(self) -> None:
        path = self.project / ".gitignore"
        for target in (self.victim, self.outside / "absent"):
            path.symlink_to(target)
            with self.assertRaises(OSError):
                cli._update_project_gitignore(self.project)
            self.assertTrue(path.is_symlink())
            path.unlink()
        self.assertFalse((self.outside / "absent").exists())
        self.assert_victim_unchanged()

    def test_hard_link_is_replaced_without_modifying_target(self) -> None:
        path = self.context / "entrypoint.sh"
        os.link(self.victim, path)
        dockerfile.render_entrypoint(self.context)
        self.assertNotEqual(path.stat().st_ino, self.victim.stat().st_ino)
        self.assertEqual(path.stat().st_mode & 0o777, 0o755)
        self.assert_victim_unchanged()

    def test_parent_swap_before_directory_open_is_rejected(self) -> None:
        original_open = os.open
        moved = self.project / "original"
        swapped = False

        def swap(path, flags, *args, **kwargs):
            nonlocal swapped
            if path == ".project-sandbox" and not swapped:
                swapped = True
                self.context.rename(moved)
                self.context.symlink_to(self.outside, target_is_directory=True)
            return original_open(path, flags, *args, **kwargs)

        with patch.object(generated_files.os, "open", side_effect=swap):
            with self.assertRaises(OSError):
                generated_files.write_text(self.context / "victim", "bad")
        self.assertTrue(swapped)
        self.assertEqual(list(moved.iterdir()), [])
        self.assert_victim_unchanged()

    def test_parent_and_leaf_swaps_after_open_cannot_redirect_write(self) -> None:
        original_replace = os.replace
        moved = self.project / "original"

        def swap(source, destination, **kwargs):
            self.context.rename(moved)
            self.context.symlink_to(self.outside, target_is_directory=True)
            (moved / "victim").symlink_to(self.victim)
            return original_replace(source, destination, **kwargs)

        with patch.object(generated_files.os, "replace", side_effect=swap):
            generated_files.write_text(self.context / "victim", "generated", mode=0o755)
        self.assertEqual((moved / "victim").read_text(), "generated")
        self.assertFalse((moved / "victim").is_symlink())
        self.assert_victim_unchanged()

    def test_directory_swap_during_certificate_cleanup_uses_open_directory(
        self,
    ) -> None:
        name = "project-sandbox-ca-stale.crt"
        (self.context / name).write_text("stale")
        external = self.outside / name
        external.write_text("keep")
        moved = self.project / "original"
        original_listdir = os.listdir

        def swap(fd):
            self.context.rename(moved)
            self.context.symlink_to(self.outside, target_is_directory=True)
            return original_listdir(fd)

        with patch.object(generated_files.os, "listdir", side_effect=swap):
            generated_files.remove_matching(
                self.context, "project-sandbox-ca-*.crt", keep=set()
            )
        self.assertFalse((moved / name).exists())
        self.assertEqual(external.read_text(), "keep")

    def test_recursive_cleanup_unlinks_nested_symlinks_without_traversal(self) -> None:
        stale = self.context / "opencode"
        stale.mkdir()
        (stale / "nested").symlink_to(self.outside, target_is_directory=True)
        config_agents._remove_stale_project_agent_credentials(
            self.context, "opencode", None
        )
        self.assertFalse(stale.exists())
        self.assert_victim_unchanged()

    def test_cleanup_rejects_parent_swap_after_old_directory_check(self) -> None:
        stale = self.context / "claude"
        stale.mkdir()
        external = self.outside / ".credentials.json"
        external.write_text("keep")
        original_check = config_agents._reject_symlinked_project_path

        def swap(path):
            original_check(path)
            stale.rmdir()
            stale.symlink_to(self.outside, target_is_directory=True)

        with patch.object(
            config_agents, "_reject_symlinked_project_path", side_effect=swap
        ):
            with self.assertRaises(OSError):
                config_agents._remove_stale_project_credentials(self.context)
        self.assertEqual(external.read_text(), "keep")

    def test_log_replaces_link_and_stream_stays_on_open_file_after_swap(self) -> None:
        path = self.context / "session.log"
        path.symlink_to(self.victim)
        with generated_files.open_log(path) as handle:
            handle.write("first\n")
            path.rename(self.context / "original.log")
            path.symlink_to(self.victim)
            handle.write("second\n")
        self.assertEqual((self.context / "original.log").read_text(), "first\nsecond\n")
        self.assertEqual((self.context / "original.log").stat().st_mode & 0o777, 0o600)
        self.assert_victim_unchanged()

    def test_session_replaces_log_link(self) -> None:
        path = self.context / "session.log"
        path.symlink_to(self.victim)
        self.assertEqual(
            session.run([sys.executable, "-c", "print('logged')"], log_path=path), 0
        )
        self.assertEqual(path.read_text(), "logged\n")
        self.assert_victim_unchanged()

    def test_private_configs_have_restrictive_mode_when_published(self) -> None:
        original_replace = os.replace
        modes = []

        def inspect(source, destination, **kwargs):
            if destination in ("models.json", "opencode.json"):
                modes.append(
                    os.stat(source, dir_fd=kwargs["src_dir_fd"]).st_mode & 0o777
                )
            return original_replace(source, destination, **kwargs)

        with patch.object(generated_files.os, "replace", side_effect=inspect):
            config_agents.render(
                self.context, agent_proxy=("http://proxy", ["model"], "secret", "bash")
            )
        self.assertEqual(modes, [0o600, 0o600])

    def test_dry_run_with_planted_links_does_not_mutate(self) -> None:
        (self.context / "entrypoint.sh").symlink_to(self.victim)
        before = sorted(
            str(p.relative_to(self.project)) for p in self.project.rglob("*")
        )
        with (
            patch.object(
                cli,
                "read_identity",
                return_value=GitIdentity("Test", "test@example.com"),
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = cli.main(
                ["--dry-run", "--no-build", str(self.project), "debian:bookworm"]
            )
        self.assertEqual(result, 0)
        self.assertEqual(
            sorted(str(p.relative_to(self.project)) for p in self.project.rglob("*")),
            before,
        )
        self.assertTrue((self.context / "entrypoint.sh").is_symlink())
        self.assert_victim_unchanged()
