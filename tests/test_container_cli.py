import sys
import tempfile
from pathlib import Path
from unittest import TestCase
import contextlib
import io

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unittest.mock import patch

from project_sandbox.container_cli import (
    CHROOT,
    DOCKER,
    MountSpec,
    PODMAN,
    _mount_arg,
    _run_quietable,
    build_image,
    build_chroot_argv,
    build_mount_specs,
    build_run_argv,
    build_stop_argv,
    ensure_system_started,
    image_exists,
    parse_mount,
    run,
    select_runtime,
)
from project_sandbox.git_identity import GitIdentity


class ContainerCliTests(TestCase):
    def test_build_run_argv_adds_explicit_host_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cmd = build_run_argv(
                image="project-sandbox:test",
                project_abs=root / "workspace",
                claude_cfg=root / "claude/settings.json",
                codex_cfg=root / "codex/config.toml",
                codex_credentials_dir=None,
                identity=GitIdentity(None, None),
                memory="8g",
                cpus=4,
                extra_mounts=[],
                agent="pi",
                firewall_enabled=True,
                interactive=False,
                add_hosts=["ollama.project-sandbox.internal:host-gateway"],
            )
        index = cmd.index("--add-host")
        self.assertEqual(
            cmd[index + 1], "ollama.project-sandbox.internal:host-gateway"
        )

    def test_select_runtime_chroot_is_explicit_linux_only(self) -> None:
        with (
            patch("project_sandbox.container_cli.sys.platform", "linux"),
            patch(
                "project_sandbox.container_cli.shutil.which",
                return_value="/usr/bin/unshare",
            ),
        ):
            self.assertEqual(select_runtime("chroot"), CHROOT)
        with patch("project_sandbox.container_cli.sys.platform", "darwin"):
            with self.assertRaisesRegex(SystemExit, "Linux only"):
                select_runtime("chroot", dry_run=True)

    def test_auto_never_selects_chroot(self) -> None:
        with (
            patch("project_sandbox.container_cli.sys.platform", "linux"),
            patch(
                "project_sandbox.container_cli.shutil.which",
                side_effect=lambda binary: (
                    "/usr/bin/unshare" if binary == "unshare" else None
                ),
            ),
        ):
            with self.assertRaisesRegex(SystemExit, "No supported container runtime"):
                select_runtime("auto")

    def test_chroot_argv_consumes_shared_mount_specs(self) -> None:
        root = Path("/tmp/layout")
        mounts = build_mount_specs(
            project_abs=root / "workspace",
            claude_cfg=root / "config/claude/settings.json",
            claude_credentials_dir=root / "secrets/claude",
            codex_cfg=root / "config/codex/config.toml",
            codex_credentials_dir=root / "secrets/codex",
            opencode_credentials_dir=None,
            extra_mounts=[
                "type=bind,source=/tmp/prompt,target=/project-sandbox-prompt,readonly"
            ],
        )
        argv = build_chroot_argv(
            script=root / "run",
            jail_root=root / "root",
            mounts=mounts,
            agent="bash-headless",
            extra_env=("PROJECT_SANDBOX_PROMPT_FILE=/prompt",),
        )
        self.assertEqual(argv[:4], ["unshare", "--map-root-user", "--mount", "--"])
        self.assertIn(
            MountSpec((root / "workspace").resolve(strict=False), "/workspace"), mounts
        )
        self.assertIn("/project-sandbox-prompt", argv)
        self.assertEqual(
            argv[-3:],
            ["--", "bash-headless", "PROJECT_SANDBOX_PROMPT_FILE=/prompt"],
        )

    def test_chroot_argv_rejects_target_outside_jail(self) -> None:
        with self.assertRaisesRegex(ValueError, "absolute jail path"):
            build_chroot_argv(
                script=Path("/tmp/run"),
                jail_root=Path("/tmp/root"),
                mounts=[MountSpec(Path("/tmp/source"), "../outside")],
            )

    def test_mount_parser_rejects_relative_target(self) -> None:
        with self.assertRaisesRegex(SystemExit, "absolute jail path"):
            build_mount_specs(
                project_abs=Path("/tmp/workspace"),
                claude_cfg=Path("/tmp/claude/settings.json"),
                claude_credentials_dir=None,
                codex_cfg=Path("/tmp/codex/config.toml"),
                codex_credentials_dir=None,
                opencode_credentials_dir=None,
                extra_mounts=["type=bind,source=/tmp/source,target=relative"],
            )

    def test_chroot_image_build_is_noop(self) -> None:
        with patch("project_sandbox.container_cli.subprocess.run") as run_mock:
            rc = build_image(
                runtime=CHROOT, context_dir=Path("/missing"), image_tag="unused"
            )
        self.assertEqual(rc, 0)
        run_mock.assert_not_called()

    def test_chroot_is_not_a_container_runtime(self) -> None:
        self.assertFalse(CHROOT.is_container)
        for runtime in (DOCKER, PODMAN):
            self.assertTrue(runtime.is_container)

    def test_parse_mount_honors_docker_ro_shorthand(self) -> None:
        mount = parse_mount("type=bind,source=/x,target=/y,ro")
        self.assertTrue(mount.readonly)

    def test_parse_mount_honors_src_dst_aliases(self) -> None:
        mount = parse_mount("type=bind,src=/x,dst=/y")
        self.assertIsNone(mount.raw)
        self.assertEqual(mount.target, "/y")

    def test_parse_mount_passes_through_non_bind_mounts_unchanged(self) -> None:
        value = "type=tmpfs,target=/scratch"
        mount = parse_mount(value)
        self.assertEqual(mount.raw, value)
        self.assertEqual(_mount_arg(mount), value)

    def test_parse_mount_passes_through_unrecognized_bind_options_unchanged(
        self,
    ) -> None:
        value = "type=bind,source=/x,target=/y,bind-propagation=rshared"
        mount = parse_mount(value)
        self.assertEqual(mount.raw, value)
        self.assertEqual(_mount_arg(mount), value)

    def test_build_chroot_argv_rejects_raw_passthrough_mounts(self) -> None:
        with self.assertRaisesRegex(ValueError, "only supports bind mounts"):
            build_chroot_argv(
                script=Path("/tmp/run"),
                jail_root=Path("/tmp/root"),
                mounts=[parse_mount("type=tmpfs,target=/scratch")],
            )

    def test_build_chroot_argv_injects_git_identity_env(self) -> None:
        argv = build_chroot_argv(
            script=Path("/tmp/run"),
            jail_root=Path("/tmp/root"),
            mounts=[],
            identity=GitIdentity("Ada Lovelace", "ada@example.com"),
            agent="bash",
            extra_env=("PROJECT_SANDBOX_QUIET=1",),
        )
        self.assertIn("GIT_AUTHOR_NAME=Ada Lovelace", argv)
        self.assertIn("GIT_AUTHOR_EMAIL=ada@example.com", argv)
        self.assertIn("GIT_COMMITTER_NAME=Ada Lovelace", argv)
        # extra_env still arrives, after the identity vars.
        self.assertEqual(argv[-1], "PROJECT_SANDBOX_QUIET=1")

    def test_build_run_argv_uses_arg_list_for_headless_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cmd = build_run_argv(
                image="project-sandbox:test",
                project_abs=root / "workspace",
                claude_cfg=root / "claude/settings.json",
                claude_credentials_dir=root / "claude-secrets",
                codex_cfg=root / "codex/config.toml",
                codex_credentials_dir=None,
                identity=GitIdentity("Ada Lovelace", "ada@example.com"),
                memory="8g",
                cpus=4,
                extra_mounts=[
                    "type=bind,source=/tmp/prompt.txt,target=/workspace/prompt,readonly"
                ],
                agent="claude-headless",
                firewall_enabled=True,
                interactive=False,
                extra_env=["PROJECT_SANDBOX_QUIET=1"],
            )

        self.assertNotIn("-it", cmd)
        self.assertIn("--cap-add", cmd)
        self.assertIn("NET_ADMIN", cmd)
        self.assertIn("PROJECT_SANDBOX_QUIET=1", cmd)
        self.assertNotIn("CLAUDE_CONFIG_DIR=/home/agent/.claude", cmd)
        self.assertIn("CLAUDE_SECURESTORAGE_CONFIG_DIR=/home/agent/.claude", cmd)
        self.assertIn(
            f"type=bind,source={(root / 'claude').resolve(strict=False)},target=/project-sandbox-config/claude,readonly",
            cmd,
        )
        self.assertIn(
            f"type=bind,source={(root / 'claude-secrets').resolve(strict=False)},target=/project-sandbox-secrets/claude,readonly",
            cmd,
        )
        self.assertIn(
            f"type=bind,source={(root / 'codex').resolve(strict=False)},target=/project-sandbox-config/codex,readonly",
            cmd,
        )
        self.assertNotIn(
            f"type=bind,source={(root / 'claude/settings.json').resolve(strict=False)},target=/home/agent/.claude/settings.json,readonly",
            cmd,
        )
        self.assertEqual(
            cmd[-3:], ["project-sandbox:test", "project-sandbox-run", "claude-headless"]
        )

    def test_build_run_argv_mounts_staged_agent_credentials_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_secrets = root / "secrets" / "codex"
            opencode_secrets = root / "secrets" / "opencode"
            pi_secrets = root / "secrets" / "pi"

            cmd = build_run_argv(
                image="project-sandbox:test",
                project_abs=root / "workspace",
                claude_cfg=root / "claude/settings.json",
                claude_credentials_dir=root / "claude-secrets",
                codex_cfg=root / "codex/config.toml",
                codex_credentials_dir=codex_secrets,
                opencode_credentials_dir=opencode_secrets,
                pi_credentials_dir=pi_secrets,
                identity=GitIdentity("Ada Lovelace", "ada@example.com"),
                memory="8g",
                cpus=4,
                extra_mounts=[],
                agent="opencode",
                firewall_enabled=False,
                interactive=True,
            )

        self.assertIn(
            f"type=bind,source={codex_secrets.resolve(strict=False)},target=/project-sandbox-secrets/codex,readonly",
            cmd,
        )
        self.assertIn(
            f"type=bind,source={opencode_secrets.resolve(strict=False)},target=/project-sandbox-secrets/opencode,readonly",
            cmd,
        )
        self.assertIn(
            f"type=bind,source={pi_secrets.resolve(strict=False)},target=/project-sandbox-secrets/pi,readonly",
            cmd,
        )
        # Pi has no baked config file, so no /project-sandbox-config/pi mount.
        self.assertNotIn("/project-sandbox-config/pi", "".join(cmd))

    def test_build_run_argv_mounts_pi_config_only_when_pi_cfg_given(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pi_cfg = root / "pi" / "models.json"

            cmd = build_run_argv(
                image="project-sandbox:test",
                project_abs=root / "workspace",
                claude_cfg=root / "claude/settings.json",
                codex_cfg=root / "codex/config.toml",
                codex_credentials_dir=None,
                pi_cfg=pi_cfg,
                identity=GitIdentity("Ada Lovelace", "ada@example.com"),
                memory="8g",
                cpus=4,
                extra_mounts=[],
                agent="pi",
                firewall_enabled=False,
                interactive=True,
            )

        self.assertIn(
            f"type=bind,source={pi_cfg.parent.resolve(strict=False)},"
            "target=/project-sandbox-config/pi,readonly",
            cmd,
        )

    def test_build_mount_specs_omits_pi_config_mount_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mounts = build_mount_specs(
                project_abs=root / "workspace",
                claude_cfg=root / "claude/settings.json",
                claude_credentials_dir=None,
                codex_cfg=root / "codex/config.toml",
                codex_credentials_dir=None,
                opencode_credentials_dir=None,
            )
        self.assertFalse(any(m.target == "/project-sandbox-config/pi" for m in mounts))

    def test_no_forward_credentials_omits_secrets_but_keeps_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cmd = build_run_argv(
                image="project-sandbox:test",
                project_abs=root / "workspace",
                claude_cfg=root / "claude/settings.json",
                claude_credentials_dir=root / "claude-secrets",
                codex_cfg=root / "codex/config.toml",
                codex_credentials_dir=root / "codex-secrets",
                opencode_credentials_dir=root / "opencode-secrets",
                pi_credentials_dir=root / "pi-secrets",
                identity=GitIdentity("Ada Lovelace", "ada@example.com"),
                memory="8g",
                cpus=4,
                extra_mounts=[],
                agent="claude",
                firewall_enabled=False,
                interactive=True,
                forward_credentials=False,
            )

        # No staged host tokens are forwarded...
        self.assertNotIn("/project-sandbox-secrets/claude,readonly", "".join(cmd))
        self.assertNotIn("/project-sandbox-secrets/codex,readonly", "".join(cmd))
        self.assertNotIn("/project-sandbox-secrets/opencode,readonly", "".join(cmd))
        self.assertNotIn("/project-sandbox-secrets/pi,readonly", "".join(cmd))
        # ...but generated, non-secret config still is.
        self.assertIn(
            f"type=bind,source={(root / 'claude').resolve(strict=False)},target=/project-sandbox-config/claude,readonly",
            cmd,
        )
        self.assertIn(
            f"type=bind,source={(root / 'codex').resolve(strict=False)},target=/project-sandbox-config/codex,readonly",
            cmd,
        )

    def test_build_image_can_use_generated_dockerfile_with_project_context(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = root / ".project-sandbox"
            context.mkdir()
            out = io.StringIO()

            with contextlib.redirect_stdout(out):
                rc = build_image(
                    context_dir=context,
                    image_tag="project-sandbox:test",
                    build_context=root,
                    dockerfile_path=context / "Dockerfile",
                    dry_run=True,
                )

        self.assertEqual(rc, 0)
        self.assertEqual(
            out.getvalue().strip(),
            f"cd {root.resolve()} && container build -t project-sandbox:test "
            "-f .project-sandbox/Dockerfile .",
        )

    @patch("project_sandbox.container_cli.host_build_identity", return_value=None)
    def test_build_image_uses_selected_docker_runtime(self, _identity) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = root / ".project-sandbox"
            context.mkdir()
            out = io.StringIO()

            with contextlib.redirect_stdout(out):
                rc = build_image(
                    runtime=DOCKER,
                    context_dir=context,
                    image_tag="project-sandbox:test",
                    build_context=root,
                    dockerfile_path=context / "Dockerfile",
                    dry_run=True,
                )

        self.assertEqual(rc, 0)
        self.assertEqual(
            out.getvalue().strip(),
            f"cd {root.resolve()} && docker build -t project-sandbox:test "
            "-f .project-sandbox/Dockerfile .",
        )

    @patch("project_sandbox.container_cli.os.getgid", return_value=2345)
    @patch("project_sandbox.container_cli.os.getuid", return_value=1234)
    @patch("project_sandbox.container_cli.sys.platform", "linux")
    def test_build_image_matches_linux_host_identity(self, _getuid, _getgid) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = build_image(
                    runtime=DOCKER,
                    context_dir=context,
                    image_tag="project-sandbox:test",
                    dry_run=True,
                )

        self.assertEqual(rc, 0)
        self.assertIn(
            "--build-arg AGENT_UID=1234 --build-arg AGENT_GID=2345",
            out.getvalue(),
        )

    @patch("project_sandbox.container_cli.os.getgid", return_value=4567)
    @patch("project_sandbox.container_cli.os.getuid", return_value=3456)
    @patch("project_sandbox.container_cli.sys.platform", "linux")
    def test_podman_build_matches_linux_host_identity(self, _getuid, _getgid) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = build_image(
                    runtime=PODMAN,
                    context_dir=Path(tmp),
                    image_tag="project-sandbox:test",
                    dry_run=True,
                )

        self.assertEqual(rc, 0)
        self.assertIn(
            "podman build -t project-sandbox:test"
            " -f Dockerfile --build-arg AGENT_UID=3456"
            " --build-arg AGENT_GID=4567 .",
            out.getvalue(),
        )

    @patch("project_sandbox.container_cli.os.getgid", return_value=0)
    @patch("project_sandbox.container_cli.os.getuid", return_value=1234)
    @patch("project_sandbox.container_cli.sys.platform", "linux")
    def test_build_image_matches_host_gid_zero(self, _getuid, _getgid) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = build_image(
                    runtime=DOCKER,
                    context_dir=Path(tmp),
                    image_tag="project-sandbox:test",
                    dry_run=True,
                )

        self.assertEqual(rc, 0)
        self.assertIn(
            "--build-arg AGENT_UID=1234 --build-arg AGENT_GID=0",
            out.getvalue(),
        )

    def test_build_image_runs_from_the_build_context_dir(self) -> None:
        # apple/container mounts the working directory as the BuildKit context,
        # so the build must run with cwd set to the build context — otherwise
        # COPY instructions resolve against whatever cwd the caller happened to
        # have and fail with "<file>: not found".
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp) / ".project-sandbox"
            context.mkdir()
            with patch("project_sandbox.container_cli.subprocess.run") as run_mock:
                run_mock.return_value.returncode = 0
                rc = build_image(
                    context_dir=context,
                    image_tag="project-sandbox:test",
                    build_context=context,
                    dockerfile_path=context / "Dockerfile",
                    verbose=True,
                )

        self.assertEqual(rc, 0)
        self.assertEqual(run_mock.call_args.kwargs["cwd"], str(context.resolve()))
        # Context must be passed as "." (apple/container ignores absolute paths).
        self.assertEqual(run_mock.call_args.args[0][-1], ".")

    def test_image_exists_inspects_tag_and_maps_returncode(self) -> None:
        with patch("project_sandbox.container_cli.subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            self.assertTrue(image_exists(DOCKER, "project-sandbox:test"))
        self.assertEqual(
            run_mock.call_args.args[0],
            ["docker", "image", "inspect", "project-sandbox:test"],
        )

    def test_image_exists_false_on_nonzero_exit(self) -> None:
        with patch("project_sandbox.container_cli.subprocess.run") as run_mock:
            run_mock.return_value.returncode = 1
            self.assertFalse(image_exists(DOCKER, "project-sandbox:missing"))

    def test_image_exists_false_when_binary_absent(self) -> None:
        with patch(
            "project_sandbox.container_cli.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            self.assertFalse(image_exists(PODMAN, "project-sandbox:test"))

    def test_image_exists_dry_run_does_not_invoke_runtime(self) -> None:
        with patch("project_sandbox.container_cli.subprocess.run") as run_mock:
            self.assertFalse(image_exists(DOCKER, "project-sandbox:test", dry_run=True))
        run_mock.assert_not_called()

    def test_build_run_argv_uses_selected_podman_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cmd = build_run_argv(
                runtime=PODMAN,
                image="project-sandbox:test",
                project_abs=root / "workspace",
                claude_cfg=root / "claude/settings.json",
                claude_credentials_dir=root / "claude-secrets",
                codex_cfg=root / "codex/config.toml",
                codex_credentials_dir=None,
                identity=GitIdentity(None, None),
                memory="8g",
                cpus=4,
                extra_mounts=[],
                agent="bash",
                firewall_enabled=False,
                interactive=True,
            )

        self.assertEqual(cmd[:2], ["podman", "run"])
        self.assertIn("-it", cmd)
        self.assertNotIn("--cap-add", cmd)

    def test_docker_runtime_does_not_start_apple_system_service(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ensure_system_started(runtime=DOCKER, dry_run=True)
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), "")

    def test_select_runtime_prefers_docker_on_linux_auto(self) -> None:
        def fake_which(binary: str) -> str | None:
            return f"/usr/bin/{binary}" if binary == "docker" else None

        with (
            patch("project_sandbox.container_cli.sys.platform", "linux"),
            patch("project_sandbox.container_cli.shutil.which", side_effect=fake_which),
        ):
            runtime = select_runtime("auto")

        self.assertEqual(runtime, DOCKER)

    def test_select_runtime_dry_run_does_not_require_binary(self) -> None:
        with patch("project_sandbox.container_cli.shutil.which", return_value=None):
            runtime = select_runtime("podman", dry_run=True)
        self.assertEqual(runtime, PODMAN)

    def test_select_runtime_explicit_missing_binary_raises(self) -> None:
        with patch("project_sandbox.container_cli.shutil.which", return_value=None):
            with self.assertRaises(SystemExit) as raised:
                select_runtime("docker")
        self.assertIn("docker CLI not found", str(raised.exception))

    def test_run_quietable_swallows_output_on_success(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = _run_quietable(["sh", "-c", "echo hello"], verbose=False)
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")

    def test_run_quietable_surfaces_output_on_failure(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = _run_quietable(["sh", "-c", "echo oops >&2; exit 7"], verbose=False)
        self.assertEqual(rc, 7)
        self.assertIn("oops", err.getvalue())

    def test_run_quietable_returns_127_when_container_not_on_path_verbose(self) -> None:
        out = io.StringIO()
        with patch("subprocess.run", side_effect=FileNotFoundError):
            with contextlib.redirect_stdout(out):
                rc = _run_quietable(["container", "system", "start"], verbose=True)
        self.assertEqual(rc, 127)
        self.assertIn("container CLI not found on PATH", out.getvalue())

    def test_run_quietable_returns_127_when_container_not_on_path_quiet(self) -> None:
        out = io.StringIO()
        with patch("subprocess.run", side_effect=FileNotFoundError):
            with contextlib.redirect_stdout(out):
                rc = _run_quietable(
                    ["container", "build", "-t", "x", "."], verbose=False
                )
        self.assertEqual(rc, 127)
        self.assertIn("container CLI not found on PATH", out.getvalue())

    def test_build_stop_argv_uses_bounded_graceful_stop(self) -> None:
        argv = build_stop_argv(DOCKER, "project-sandbox-abc123")
        self.assertEqual(
            argv, ["docker", "stop", "--time", "5", "project-sandbox-abc123"]
        )

    def test_build_stop_argv_honours_custom_grace_and_runtime(self) -> None:
        argv = build_stop_argv(PODMAN, "project-sandbox-xyz", grace=12)
        self.assertEqual(argv[0], "podman")
        self.assertIn("stop", argv)
        self.assertIn("--time", argv)
        self.assertIn("12", argv)
        self.assertIn("project-sandbox-xyz", argv)
        self.assertNotIn("kill", argv)

    def test_run_returns_127_when_container_not_on_path(self) -> None:
        out = io.StringIO()
        with patch("subprocess.run", side_effect=FileNotFoundError):
            with contextlib.redirect_stdout(out):
                rc = run(["container", "run", "--rm", "some-image", "cmd"])
        self.assertEqual(rc, 127)
        self.assertIn("container CLI not found on PATH", out.getvalue())

    def test_run_without_env_inherits_parent_environment(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            rc = run(["docker", "run", "image"])
        self.assertEqual(rc, 0)
        self.assertIsNone(mock_run.call_args.kwargs["env"])

    def test_run_merges_extra_env_without_leaking_into_argv(self) -> None:
        import os

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            argv = ["docker", "run", "--env", "MY_SECRET", "image"]
            rc = run(argv, env={"MY_SECRET": "top-secret-value"})

        self.assertEqual(rc, 0)
        called_argv, called_kwargs = mock_run.call_args.args, mock_run.call_args.kwargs
        self.assertEqual(called_argv[0], argv)
        self.assertFalse(any("top-secret-value" in token for token in called_argv[0]))
        merged_env = called_kwargs["env"]
        self.assertEqual(merged_env["MY_SECRET"], "top-secret-value")
        # The rest of the parent environment must still be present.
        self.assertEqual(merged_env.get("PATH"), os.environ.get("PATH"))
