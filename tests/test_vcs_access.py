import contextlib
import io
import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from project_sandbox import (
    cli,
    container_cli,
    devcontainer,
    jj_workspace,
    vcs_access,
    worktree,
)
from project_sandbox.git_identity import GitIdentity


class VcsAccessTests(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.project = self.root / "repo"
        self.project.mkdir()
        self.git = self.project / ".git"
        self.git.mkdir()
        (self.git / "config").write_text("[core]\n")
        (self.project / "source.py").write_text("original\n")

    def mask_dir(self):
        masks = vcs_access.new_mask_dir()
        self.addCleanup(shutil.rmtree, masks, ignore_errors=True)
        return masks

    def git_worktree(self):
        workspace = self.root / "worktree"
        workspace.mkdir()
        gitdir = self.git / "worktrees" / "task"
        gitdir.mkdir(parents=True)
        (gitdir / "commondir").write_text("../..\n")
        (workspace / ".git").write_text(f"gitdir: {gitdir}\n")
        return workspace, gitdir

    def jj_workspace(self):
        repo = self.project / ".jj" / "repo"
        (repo / "store").mkdir(parents=True)
        (repo / "store" / "git_target").write_text("../../../.git\n")
        workspace = self.root / "jj-task"
        (workspace / ".jj").mkdir(parents=True)
        (workspace / ".jj" / "repo").write_text(str(repo) + "\n")
        return workspace, repo

    def test_read_only_working_copy_and_full_mode(self):
        layout = vcs_access.inspect(self.project)
        mounts = vcs_access.mounts(layout, "read-only")
        self.assertEqual(
            mounts, [container_cli.MountSpec(self.git, "/workspace/.git", True)]
        )
        self.assertEqual(vcs_access.mounts(layout, "full"), [])
        (self.project / "source.py").write_text("edited\n")
        layout.check_roots()

    def test_git_worktree_protects_pointer_gitdir_and_common_directory(self):
        workspace, gitdir = self.git_worktree()
        layout = vcs_access.inspect(workspace)
        mounts = {m.target: m for m in vcs_access.mounts(layout, "read-only")}
        self.assertEqual(mounts["/workspace/.git"].source, workspace / ".git")
        self.assertEqual(mounts[str(gitdir)].source, gitdir)
        self.assertEqual(mounts[str(self.git)].source, self.git)
        self.assertTrue(all(m.readonly for m in mounts.values()))

    def test_jj_protects_workspace_shared_store_and_git_backend(self):
        workspace, repo = self.jj_workspace()
        layout = vcs_access.inspect(workspace)
        mounts = {m.target: m for m in vcs_access.mounts(layout, "read-only")}
        self.assertEqual(mounts["/workspace/.jj"].source, workspace / ".jj")
        self.assertEqual(mounts[str(repo)].source, repo)
        self.assertEqual(mounts[str(self.git)].source, self.git)
        self.assertTrue(all(m.readonly for m in mounts.values()))

    def test_hidden_omits_all_external_stores_and_uses_private_empty_sources(self):
        for factory, root_name, mask_name in (
            (self.git_worktree, ".git", "file"),
            (self.jj_workspace, ".jj", "directory"),
        ):
            with self.subTest(root=root_name):
                workspace, _ = factory()
                masks = self.mask_dir()
                mounts = vcs_access.mounts(
                    vcs_access.inspect(workspace), "hidden", masks
                )
                self.assertEqual(
                    mounts,
                    [
                        container_cli.MountSpec(
                            masks / mask_name, f"/workspace/{root_name}", True
                        )
                    ],
                )
                self.assertEqual(list((masks / "directory").iterdir()), [])
                self.assertEqual((masks / "file").read_bytes(), b"")
                self.assertNotIn(self.project, masks.parents)

    def test_apple_directory_metadata_supported_but_file_metadata_rejected(self):
        vcs_access.validate(
            vcs_access.inspect(self.project),
            "hidden",
            container_cli.APPLE_CONTAINER,
            [],
        )
        workspace, _ = self.git_worktree()
        for mode in ("read-only", "hidden"):
            with (
                self.subTest(mode=mode),
                self.assertRaisesRegex(ValueError, "file bind mount"),
            ):
                vcs_access.validate(
                    vcs_access.inspect(workspace),
                    mode,
                    container_cli.APPLE_CONTAINER,
                    [],
                )

    def test_symlinked_and_nested_metadata_rejected(self):
        for name in (".git", ".jj"):
            with self.subTest(name=name):
                nested = self.project / "nested"
                nested.mkdir(exist_ok=True)
                link = nested / name
                link.symlink_to(self.git, target_is_directory=True)
                with self.assertRaisesRegex(ValueError, "nested repositories"):
                    vcs_access.inspect(self.project)
                link.unlink()
        (self.project / ".jj").symlink_to(self.git, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlinked metadata"):
            vcs_access.inspect(self.project)

    def test_extra_mount_aliases_and_overrides_rejected(self):
        layout = vcs_access.inspect(self.project)
        values = [
            f"type=bind,source={self.project},target=/alias",
            f"type=bind,source={self.root},target=/alias,readonly",
            f"type=bind,source={self.git / 'config'},target=/config",
            f"type=bind,source={self.root / 'other'},target=/workspace/.git",
            f"type=bind,source={self.root / 'other'},target=/workspace",
            f"type=bind,source={self.root / 'other'},target=/",
            f"type=bind,source={self.git},target=/alias,bind-propagation=rprivate",
            "type=volume,source=unknown,target=/alias",
        ]
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                vcs_access.validate(layout, "read-only", container_cli.DOCKER, [value])
        vcs_access.validate(
            layout,
            "read-only",
            container_cli.DOCKER,
            [f"type=bind,source={self.root / 'other'},target=/data"],
        )

    def test_metadata_symlinks_cannot_redirect_to_writable_source_files(self):
        (self.git / "config").unlink()
        (self.git / "config").symlink_to(self.project / "source.py")
        with self.assertRaisesRegex(ValueError, "symlinks inside metadata"):
            vcs_access.inspect(self.project)

    def test_metadata_hardlinks_cannot_expose_a_writable_alias(self):
        os.link(self.git / "config", self.project / "config-alias")
        with self.assertRaisesRegex(ValueError, "hard-linked metadata"):
            vcs_access.inspect(self.project)

    def test_external_store_cannot_be_reexposed_by_extra_mount(self):
        workspace, _ = self.jj_workspace()
        layout = vcs_access.inspect(workspace)
        with self.assertRaisesRegex(ValueError, "protected"):
            vcs_access.validate(
                layout,
                "hidden",
                container_cli.DOCKER,
                [f"type=bind,source={self.project},target=/alias"],
            )

    def test_changed_root_layout_is_rejected_before_host_capture(self):
        layout = vcs_access.inspect(self.project)
        (self.project / ".jj").mkdir()
        with self.assertRaisesRegex(ValueError, "without automatic capture"):
            layout.check_roots()

    def test_tracked_attributes_remain_unmodified(self):
        attributes = self.project / ".gitattributes"
        attributes.write_text("*.dat filter=example\n")
        layout = vcs_access.inspect(self.project)
        mounts = vcs_access.mounts(layout, "hidden", self.mask_dir())
        self.assertNotIn(str(attributes), [str(m.source) for m in mounts])
        self.assertEqual(attributes.read_text(), "*.dat filter=example\n")

    def test_cli_mounts_and_non_mutating_branch_preview(self):
        for mode in ("full", "read-only", "hidden"):
            with self.subTest(mode=mode):
                args = cli.build_parser().parse_args(
                    [str(self.project), "--agent", "bash", "--vcs-access", mode]
                )
                output = io.StringIO()
                before = sorted(self.root.rglob("*"))
                with contextlib.redirect_stdout(output):
                    command, *_ = cli._build_session_command(
                        args,
                        project=self.project,
                        context_dir=self.project / ".project-sandbox",
                        workspace=self.project,
                        worktree=None,
                        identity=GitIdentity(None, None),
                        run_agent="bash",
                        claude_cfg=self.root / "claude/settings.json",
                        codex_cfg=self.root / "codex/config.toml",
                        credential_dirs={},
                        runtime=container_cli.DOCKER,
                        create_prompt_files=False,
                    )
                self.assertEqual(sorted(self.root.rglob("*")), before)
                mount_args = [
                    command[i + 1]
                    for i, value in enumerate(command)
                    if value == "--mount"
                ]
                metadata = [m for m in mount_args if "target=/workspace/.git," in m]
                self.assertEqual(len(metadata), int(mode != "full"))
                if metadata:
                    self.assertTrue(metadata[0].endswith(",readonly"))
                    self.assertEqual(
                        "project-sandbox-vcs-preview" in metadata[0], mode == "hidden"
                    )
                self.assertEqual("GIT_OPTIONAL_LOCKS=0" in command, mode == "read-only")

        planned = self.root / "not-created"
        for wt in (
            worktree.Worktree(planned, "task"),
            jj_workspace.JjWorkspace(planned, "task"),
        ):
            with self.subTest(kind=type(wt).__name__):
                layout = cli._vcs_layout_for_session(
                    self.project, planned, wt, preview=True
                )
                self.assertFalse(planned.exists())
                self.assertTrue(layout.roots)
                self.assertTrue(all(m.readonly for m in layout.mounts))

    def test_devcontainer_applies_the_selected_mode(self):
        (self.project / ".project-sandbox").mkdir()
        for mode in ("read-only", "hidden"):
            with (
                self.subTest(mode=mode),
                patch.object(vcs_access, "new_mask_dir", return_value=self.mask_dir()),
            ):
                devcontainer.render(
                    self.project,
                    identity=GitIdentity(None, None),
                    firewall_enabled=True,
                    memory=None,
                    cpus=None,
                    extra_mounts=[],
                    credential_dirs={},
                    forward_credentials=False,
                    vcs_mode=mode,
                )
                spec = json.loads(
                    (self.project / ".devcontainer/devcontainer.json").read_text()
                )
                metadata = [m for m in spec["mounts"] if "target=/workspace/.git," in m]
                self.assertEqual(len(metadata), 1)
                self.assertTrue(metadata[0].endswith(",readonly"))
                self.assertEqual(
                    spec["containerEnv"].get("GIT_OPTIONAL_LOCKS"),
                    "0" if mode == "read-only" else None,
                )

    def test_comma_paths_round_trip_on_docker_and_podman_only(self):
        # Regression: security e2e test_private_mounts_and_devcontainer_reopen
        # [comma,name] refused --vcs-access hidden although CSV encoding
        # round-trips the path on Docker and Podman.
        project = self.root / "comma,name"
        self.project.rename(project)
        layout = vcs_access.inspect(project)
        for runtime in (container_cli.DOCKER, container_cli.PODMAN):
            for mode in ("read-only", "hidden"):
                with self.subTest(runtime=runtime.name, mode=mode):
                    vcs_access.validate(layout, mode, runtime, [])
        mounts = vcs_access.mounts(layout, "read-only")
        for mount in mounts:
            parsed = container_cli.parse_mount(container_cli._mount_arg(mount))
            self.assertEqual(
                (parsed.source, parsed.target, parsed.readonly),
                (mount.source, mount.target, True),
            )
        self.assertIn(project / ".git", [m.source for m in mounts])
        with self.assertRaisesRegex(ValueError, "Apple container"):
            vcs_access.validate(layout, "hidden", container_cli.APPLE_CONTAINER, [])

    def test_control_characters_in_metadata_paths_are_rejected(self):
        project = self.root / "line\nbreak"
        self.project.rename(project)
        with self.assertRaisesRegex(ValueError, "control characters"):
            vcs_access.validate(
                vcs_access.inspect(project), "hidden", container_cli.DOCKER, []
            )

    def test_restricted_chroot_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "chroot"):
            vcs_access.validate(
                vcs_access.inspect(self.project), "hidden", container_cli.CHROOT, []
            )

    def test_cli_captures_branch_after_task_but_leaves_working_copy_uncommitted(self):
        workspace, _ = self.git_worktree()
        original_new_mask_dir = vcs_access.new_mask_dir

        def mask_dir():
            path = original_new_mask_dir()
            self.addCleanup(shutil.rmtree, path, ignore_errors=True)
            return path

        for mode, branch, inject_metadata in (
            ("read-only", True, False),
            ("hidden", True, False),
            ("hidden", False, False),
            ("hidden", True, True),
        ):
            with self.subTest(
                mode=mode, branch=branch, inject_metadata=inject_metadata
            ):
                active = workspace if branch else self.project
                events = []
                runtime_masks = []

                def run(
                    command,
                    *,
                    events=events,
                    mode=mode,
                    runtime_masks=runtime_masks,
                    active=active,
                    inject_metadata=inject_metadata,
                    **kwargs,
                ):
                    events.append("task")
                    self.assertEqual(command[:3], ["docker", "run", "--rm"])
                    for i, item in enumerate(command):
                        if item == "--mount":
                            spec = container_cli.parse_mount(command[i + 1])
                            if spec.target == "/workspace/.git":
                                self.assertTrue(spec.readonly)
                                if mode == "hidden":
                                    if spec.source.is_file():
                                        self.assertEqual(spec.source.read_bytes(), b"")
                                    else:
                                        self.assertEqual(
                                            list(spec.source.iterdir()), []
                                        )
                                    runtime_masks.append(spec.source.parent)
                    (active / "source.py").write_text("agent edit\n")
                    if inject_metadata:
                        (active / ".jj").mkdir()
                    return 0

                def finalize(*args, events=events, active=active, **kwargs):
                    self.assertEqual(events, ["task"])
                    self.assertEqual((active / "source.py").read_text(), "agent edit\n")
                    events.append("capture")

                with (
                    patch.object(
                        cli,
                        "read_identity",
                        return_value=GitIdentity("Test", "test@example.com"),
                    ),
                    patch.object(
                        cli.config_agents, "available_agents", return_value=("claude",)
                    ),
                    patch.object(
                        cli.config_agents, "CREDENTIALS_ROOT", self.root / "credentials"
                    ),
                    patch.object(
                        cli.container_cli,
                        "select_runtime",
                        return_value=container_cli.DOCKER,
                    ),
                    patch.object(
                        cli.container_cli, "ensure_system_started", return_value=0
                    ),
                    patch.object(cli.container_cli, "build_image", return_value=0),
                    patch.object(cli.container_cli, "run", side_effect=run),
                    patch.object(
                        cli.worktree_mod,
                        "setup",
                        return_value=worktree.Worktree(workspace, "task"),
                    ),
                    patch.object(cli.worktree_mod, "finalize", side_effect=finalize),
                    patch.object(cli.observability, "start_record", return_value=None),
                    patch.object(cli.observability, "finish_record"),
                    patch.object(vcs_access, "new_mask_dir", side_effect=mask_dir),
                    contextlib.redirect_stdout(io.StringIO()) as output,
                ):
                    rc = cli.main(
                        [
                            "--runtime",
                            "docker",
                            "--agent",
                            "bash",
                            "--vcs-access",
                            mode,
                            "--no-forward-credentials",
                            "--no-token-refresh",
                            "--force-build",
                            *(["--branch", "task"] if branch else []),
                            str(self.project),
                            "python:3.12-slim",
                        ]
                    )
                self.assertEqual(rc, 0)
                self.assertEqual(
                    events,
                    ["task", "capture"] if branch and not inject_metadata else ["task"],
                )
                self.assertTrue(all(not path.exists() for path in runtime_masks))
                if inject_metadata:
                    self.assertIn("without automatic capture", output.getvalue())
