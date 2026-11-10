"""Security finding 4: implicit sources must not be repository-controlled."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from project_sandbox import (
    cli,
    container_cli,
    devcontainer,
    implicit_mounts,
    mount_storage,
    paths,
)
from project_sandbox.git_identity import GitIdentity


class ImplicitMountTests(TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name).resolve() / "project"
        self.project.mkdir()
        self.context = self.project / ".project-sandbox"
        self.context.mkdir()
        self.root = mount_storage.storage_root()
        self.outside = self.project.parent / "outside"
        self.outside.mkdir()
        (self.outside / "keep").write_text("untouched")

    def command(
        self,
        *,
        runtime=container_cli.DOCKER,
        create=True,
        headless=False,
        token="session-a",
        extra_mounts=(),
    ):
        args = cli.build_parser().parse_args(
            [
                "--no-forward-credentials",
                "--no-build",
                str(self.project),
                "debian:bookworm",
            ]
        )
        args.image_tag = "project-sandbox:test"
        args.extra_mounts = list(extra_mounts)
        args.prompt_text = "echo test" if headless else None
        return cli._build_session_command(
            args,
            project=self.project,
            context_dir=self.context,
            workspace=self.project,
            worktree=None,
            identity=GitIdentity(None, None),
            run_agent="bash",
            claude_cfg=self.context / "claude/settings.json",
            credential_dirs={},
            codex_cfg=self.context / "codex/config.toml",
            runtime=runtime,
            create_prompt_files=create,
            session_id=token,
        )[0]

    def render(self, *, extra_mounts=()):
        devcontainer.render(
            self.project,
            identity=GitIdentity(None, None),
            firewall_enabled=False,
            memory=None,
            cpus=None,
            extra_mounts=list(extra_mounts),
            forward_credentials=False,
        )
        return json.loads(
            (self.project / ".devcontainer/devcontainer.json").read_text()
        )

    def initialize_command(self, spec):
        return [
            arg.replace("${localWorkspaceFolder}", str(self.project))
            for arg in spec["initializeCommand"]
        ]

    def test_planted_repository_links_never_become_implicit_sources(self) -> None:
        (self.context / "history").symlink_to(self.outside)
        (self.context / "workspace-mask").symlink_to(self.outside)
        for runtime in (
            container_cli.DOCKER,
            container_cli.PODMAN,
            container_cli.APPLE_CONTAINER,
        ):
            for headless in (False, True):
                with self.subTest(runtime=runtime.name, headless=headless):
                    command = self.command(runtime=runtime, headless=headless)
                    mounts = [
                        container_cli.parse_mount(command[i + 1])
                        for i, arg in enumerate(command)
                        if arg == "--mount"
                    ]
                    implicit = [
                        m
                        for m in mounts
                        if m.target
                        in (
                            paths.HISTORY_SHELL_TARGET,
                            paths.HISTORY_CLAUDE_PROJECTS_TARGET,
                            paths.WORKSPACE_SANDBOX_TARGET,
                        )
                    ]
                    self.assertEqual(len(implicit), 1 if headless else 3)
                    for mount in implicit:
                        self.assertTrue(mount.source.is_relative_to(self.root))
                        self.assertFalse(mount.source.is_relative_to(self.project))
                        self.assertTrue(mount.source.is_dir())
                    self.assertTrue(implicit[-1].readonly)
                    self.assertEqual(list(implicit[-1].source.iterdir()), [])
        self.assertEqual((self.outside / "keep").read_text(), "untouched")
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()), ["keep"])

    def test_repository_swap_during_command_construction_cannot_redirect_sources(
        self,
    ) -> None:
        history = self.context / "history"
        history.mkdir()
        original = paths.ensure_history_paths

        def swap(project, *, create):
            result = original(project, create=create)
            history.rmdir()
            history.symlink_to(self.outside)
            (self.context / "workspace-mask").symlink_to(self.outside)
            return result

        with patch.object(cli, "ensure_history_paths", side_effect=swap):
            command = self.command()
        self.assertIn(
            f"type=bind,source={mount_storage.history_paths(self.project)[0]},target={paths.HISTORY_SHELL_TARGET}",
            command,
        )
        self.assertIn(
            f"type=bind,source={mount_storage.mask_path(self.project, 'session-a')},target={paths.WORKSPACE_SANDBOX_TARGET},readonly",
            command,
        )
        self.assertNotIn(str(self.outside), " ".join(command))

    def test_history_persists_and_sessions_have_distinct_masks(self) -> None:
        self.command(token="one")
        shell, _ = mount_storage.history_paths(self.project)
        (shell / "bash_history").write_text("history")
        # Host setup must not follow or touch history contents supplied by an agent.
        (shell / "link").symlink_to(self.outside / "keep")
        self.command(token="two")
        self.assertEqual((shell / "bash_history").read_text(), "history")
        first = mount_storage.mask_path(self.project, "one")
        second = mount_storage.mask_path(self.project, "two")
        mount_storage.cleanup_mask(first, root=self.root)
        self.assertFalse(first.exists())
        self.assertTrue(second.is_dir())
        self.assertEqual((self.outside / "keep").read_text(), "untouched")

    def test_storage_directories_reject_symlinks_and_unsafe_permissions(self) -> None:
        for component in (
            self.root,
            mount_storage.history_paths(self.project)[0],
            mount_storage.mask_path(self.project, "session-a"),
        ):
            with self.subTest(component=component):
                component.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                # mkdir(parents=True) doesn't propagate the requested mode.
                for path in (component.parent, *component.parents):
                    if path.is_relative_to(self.root):
                        path.chmod(0o700)
                component.symlink_to(self.outside)
                with self.assertRaises(OSError):
                    self.command()
                component.unlink()
        self.root.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "Unsafe private mount directory"):
            mount_storage.prepare_history(self.project)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o755)

    def test_wrong_owner_is_rejected_without_chmod(self) -> None:
        self.command()
        original = os.fstat
        inode = self.root.stat().st_ino

        def foreign(fd):
            info = original(fd)
            if info.st_ino == inode:
                return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1)
            return info

        with patch.object(mount_storage.os, "fstat", side_effect=foreign):
            with self.assertRaisesRegex(ValueError, "expected owner"):
                mount_storage.prepare_history(self.project)

    def test_restrictive_umask_keeps_private_parents_and_readable_mask(self) -> None:
        old_umask = os.umask(0o077)
        try:
            mask = mount_storage.prepare_mask(self.project, "private-umask")
        finally:
            os.umask(old_umask)
        self.assertEqual(mask.stat().st_mode & 0o777, 0o755)
        self.assertEqual(mask.parent.stat().st_mode & 0o777, 0o700)

    def test_storage_component_swap_before_open_cannot_redirect_creation(self) -> None:
        original_open = os.open
        moved = self.root.with_name("original-mounts")
        swapped = False

        def swap(path, flags, *args, **kwargs):
            nonlocal swapped
            if path == self.root.name and not swapped:
                swapped = True
                self.root.rename(moved)
                self.root.symlink_to(self.outside)
            return original_open(path, flags, *args, **kwargs)

        with patch.object(mount_storage.os, "open", side_effect=swap):
            with self.assertRaises(OSError):
                mount_storage.prepare_history(self.project)
        self.assertTrue(swapped)
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()), ["keep"])
        self.assertEqual(list(moved.iterdir()), [])

    def test_nonempty_mask_is_rejected_without_removing_its_contents(self) -> None:
        mask = mount_storage.prepare_mask(self.project, "session-a")
        (mask / "unexpected").write_text("keep")
        with self.assertRaisesRegex(ValueError, "not empty"):
            self.command()
        self.assertEqual((mask / "unexpected").read_text(), "keep")

    def test_storage_inside_workspace_or_build_context_is_rejected_before_creation(
        self,
    ) -> None:
        for root in (self.project / "state", self.outside / "state"):
            with (
                self.subTest(root=root),
                patch.object(mount_storage, "storage_root", return_value=root),
            ):
                with self.assertRaisesRegex(ValueError, "overlaps"):
                    implicit_mounts.validate_sources(
                        self.project, build_context=self.outside
                    )
                self.assertFalse(root.exists())

    def test_extra_mounts_cannot_expose_storage_or_its_ancestors(self) -> None:
        for source in (self.root, self.root.parent, self.root / "projects"):
            for alias in ("source", "src"):
                with self.subTest(source=source, alias=alias):
                    with self.assertRaisesRegex(SystemExit, "overlaps"):
                        self.command(
                            extra_mounts=[f"type=bind,{alias}={source},target=/exposed"]
                        )
        self.assertFalse(self.root.exists())

    def test_ambiguous_and_unmodeled_extra_mounts_fail_closed(self) -> None:
        for mount in (
            f"source={self.root},src={self.outside},target=/x,type=bind",
            f"source={self.root},target=/x,type=bind,bind-propagation=rprivate",
            "type=volume,source=named,target=/x",
        ):
            with self.subTest(mount=mount), self.assertRaises(SystemExit):
                self.command(extra_mounts=[mount])
        self.assertFalse(self.root.exists())

    def test_dry_run_has_no_private_state_side_effects(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            self.command(create=False)
        self.assertFalse(self.root.exists())
        self.assertEqual(list(self.context.iterdir()), [])

    def test_devcontainer_reopens_using_private_sources_and_isolated_helper(
        self,
    ) -> None:
        spec = self.render()
        command = self.initialize_command(spec)
        self.assertEqual(command[1], "-I")
        self.assertEqual(command[2], "-S")
        self.assertTrue(Path(command[3]).is_relative_to(self.root))
        (self.project / "argparse.py").write_text(
            "raise RuntimeError('workspace import')"
        )
        (self.context / "history").symlink_to(self.outside)
        (self.context / "workspace-mask").symlink_to(self.outside)
        history = mount_storage.history_paths(self.project)[0].parent
        shutil.rmtree(history)
        mask = mount_storage.mask_path(self.project, "devcontainer")
        mask.rmdir()
        subprocess.run(command, cwd=self.project, check=True, capture_output=True)
        self.assertTrue(history.is_dir())
        self.assertEqual(list(mask.iterdir()), [])
        for value in spec["mounts"]:
            if any(
                f"target={target}," in value
                for target in (
                    paths.HISTORY_SHELL_TARGET,
                    paths.HISTORY_CLAUDE_PROJECTS_TARGET,
                    paths.WORKSPACE_SANDBOX_TARGET,
                    paths.WORKSPACE_DEVCONTAINER_TARGET,
                )
            ):
                self.assertTrue(
                    container_cli.parse_mount(value).source.is_relative_to(self.root)
                )

    def test_devcontainer_reopen_refuses_substituted_private_history_root(self) -> None:
        spec = self.render()
        shell, _ = mount_storage.history_paths(self.project)
        shell.rmdir()
        shell.symlink_to(self.outside)
        proc = subprocess.run(
            self.initialize_command(spec),
            cwd=self.project,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Cannot prepare private container mounts", proc.stderr)
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()), ["keep"])

    def test_devcontainer_reopen_refuses_nonempty_mask(self) -> None:
        spec = self.render()
        mask = mount_storage.mask_path(self.project, "devcontainer")
        (mask / "unexpected").write_text("keep")
        proc = subprocess.run(
            self.initialize_command(spec), capture_output=True, text=True, check=False
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not empty", proc.stderr)

    def test_copied_devcontainer_cannot_reuse_another_projects_history(self) -> None:
        spec = self.render()
        command = [
            arg.replace("${localWorkspaceFolder}", str(self.outside))
            for arg in spec["initializeCommand"]
        ]
        proc = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Workspace moved or changed", proc.stderr)

    def test_devcontainer_rechecks_extra_mount_symlink_on_reopen(self) -> None:
        alias = self.project.parent / "alias"
        alias.symlink_to(self.outside)
        spec = self.render(extra_mounts=[f"source={alias},target=/extra,type=bind"])
        alias.unlink()
        alias.symlink_to(self.root)
        proc = subprocess.run(
            self.initialize_command(spec), capture_output=True, text=True, check=False
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("overlaps", proc.stderr)

    def test_helper_interpreter_cannot_live_in_the_exposed_workspace(self) -> None:
        with patch.object(
            implicit_mounts.sys, "executable", str(self.project / "python")
        ):
            with self.assertRaisesRegex(ValueError, "overlaps"):
                self.render()
        self.assertFalse((self.project / ".devcontainer/devcontainer.json").exists())

    def test_cli_cleans_mask_after_exit_and_startup_failure(self) -> None:
        # Exercise the full CLI lifecycle with all runtime execution mocked.
        for failure in (False, True):
            created = []

            def run(command, *, created=created, failure=failure, **kwargs):
                if command[:2] == ["docker", "run"]:
                    mask = next(
                        container_cli.parse_mount(command[i + 1]).source
                        for i, arg in enumerate(command)
                        if arg == "--mount"
                        and f"target={paths.WORKSPACE_SANDBOX_TARGET},"
                        in command[i + 1]
                    )
                    created.append(mask)
                    self.assertTrue(mask.is_dir())
                    if failure:
                        raise OSError("container start failed")
                return 0

            with (
                patch.object(
                    container_cli, "select_runtime", return_value=container_cli.DOCKER
                ),
                patch.object(container_cli, "run", side_effect=run),
                patch.object(container_cli, "image_exists", return_value=True),
                patch.object(
                    cli, "read_identity", return_value=GitIdentity(None, None)
                ),
                patch.object(
                    cli.config_agents, "available_agents", return_value=("bash",)
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                argv = [
                    "--no-build",
                    "--no-firewall",
                    "--no-forward-credentials",
                    "--agent",
                    "bash",
                    str(self.project),
                    "debian:bookworm",
                ]
                if failure:
                    with self.assertRaisesRegex(OSError, "container start failed"):
                        cli.main(argv)
                else:
                    self.assertEqual(cli.main(argv), 0)
            self.assertEqual(len(created), 1)
            self.assertFalse(created[0].exists())
