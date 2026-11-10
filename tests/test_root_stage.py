"""Root stage and privilege drop (isolate-firewall-helpers tasks 2.x and 3.x).

Agent sessions start as root only for the image's root stage
(``project-sandbox-init``): it installs the firewall from ``/`` with a fixed
environment, then ``setpriv`` drops to the agent irrevocably and execs the
normal entrypoint. Images built before this model are refused or rebuilt.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from project_sandbox import build_cache, cli, container_cli, dockerfile
from project_sandbox.git_identity import GitIdentity

SETPRIV_LINE = (
    'exec "$setpriv_bin" --reuid="$agent_uid" --regid="$agent_gid" --init-groups \\\n'
    "  --inh-caps=-all --ambient-caps=-all --bounding-set=-all --no-new-privs \\\n"
    '  -- /usr/local/bin/project-sandbox-entrypoint "$@"\n'
)
ROOT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
FIREWALL = "/usr/local/bin/project-sandbox-init-firewall"


def _render_root_stage() -> str:
    with tempfile.TemporaryDirectory() as tmp:
        out = dockerfile.render_root_stage(Path(tmp))
        assert out.name == dockerfile.ROOT_STAGE_NAME
        assert os.access(out, os.X_OK)
        return out.read_text(encoding="utf-8")


class RootStageTemplateTests(unittest.TestCase):
    def test_exact_privilege_drop(self) -> None:
        text = _render_root_stage()
        self.assertTrue(text.endswith(SETPRIV_LINE), text[-400:])
        code = [line for line in text.splitlines() if not line.startswith("#")]
        self.assertEqual(sum("setpriv" in line for line in code), 2)  # lookup + exec
        self.assertIn('agent_uid="$(id -u agent)"', text)
        self.assertIn('agent_gid="$(id -g agent)"', text)

    def test_changes_to_root_and_fixes_path_before_any_command(self) -> None:
        commands = [
            line
            for line in _render_root_stage().splitlines()
            if line and not line.startswith("#")
        ]
        self.assertEqual(
            commands[:6],
            [
                "set -eu",
                'SESSION_PATH="$PATH"',
                f"ROOT_PATH={ROOT_PATH}",
                'PATH="$ROOT_PATH"',
                "export PATH",
                "cd /",
            ],
        )

    def test_firewall_runs_with_a_fixed_minimal_environment(self) -> None:
        text = _render_root_stage()
        invocation = f'env -i PATH="$ROOT_PATH" HOME=/root {FIREWALL}'
        self.assertEqual(text.count(FIREWALL), 2)  # quiet and verbose branches
        self.assertEqual(text.count(invocation), 2)
        self.assertIn('if [ "${PROJECT_SANDBOX_NO_FIREWALL:-0}" != "1" ]; then', text)
        self.assertNotIn("sudo", text)


class RootStageBehaviourTests(unittest.TestCase):
    """Run the rendered root stage with stub tools in place of the image's."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "log"
        self._stub(
            "id",
            'case "$*" in "-u") echo 0;; "-u agent") echo 1000;; "-g agent") echo 1001;; esac',
        )
        self._stub("getent", "echo 'agent:x:1000:1001::/home/agent:/bin/bash'")
        self._stub(
            "setpriv",
            f'{{ echo "setpriv $*"; echo "HOME=$HOME"; echo "PATH=$PATH"; }} >> {self.log}',
        )
        self.firewall = self.root / "firewall"
        for tool in ("env", "cut"):
            (self.bin / tool).symlink_to(f"/usr/bin/{tool}")
        text = _render_root_stage()
        text = text.replace(f"ROOT_PATH={ROOT_PATH}", f"ROOT_PATH={self.bin}")
        self.script = self.root / "project-sandbox-init"
        self.script.write_text(text.replace(FIREWALL, str(self.firewall)))

    def _stub(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)

    def _firewall(self, *, exit_code: int) -> None:
        # Python, not sh: a shell would add PWD/SHLVL to what it reports.
        self.firewall.write_text(
            f"#!{sys.executable} -I\n"
            "import os, sys\n"
            f"with open({str(self.log)!r}, 'a') as log:\n"
            "    log.write(f'firewall cwd={os.getcwd()}\\n')\n"
            "    for item in sorted(os.environ.items()):\n"
            "        log.write('firewall env %s=%s\\n' % item)\n"
            "print('Firewall banner')\n"
            f"sys.exit({exit_code})\n"
        )
        self.firewall.chmod(0o755)

    def _run(self, **env: str) -> subprocess.CompletedProcess:
        base = {
            "PATH": "/image/bin:/usr/bin:/bin",
            "HOME": "/root",
            "BASH_ENV": "/workspace/evil",
            "PYTHONPATH": "/workspace",
        }
        return subprocess.run(
            ["/bin/sh", str(self.script), "project-sandbox-run", "claude"],
            cwd=self.root,
            env={**base, **env},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def _log(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_firewall_then_drop(self) -> None:
        self._firewall(exit_code=0)
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Firewall banner", result.stdout)
        log = self._log()
        self.assertEqual(log[0], "firewall cwd=/")
        firewall_env = sorted(
            line.removeprefix("firewall env ")
            for line in log
            if line.startswith("firewall env ")
            # Added by the macOS Python stub itself (locale coercion, CF).
            and not line.startswith(("firewall env LC_CTYPE=", "firewall env __CF_"))
        )
        self.assertEqual(firewall_env, ["HOME=/root", f"PATH={self.bin}"])
        self.assertEqual(
            log[-3:],
            [
                (
                    "setpriv --reuid=1000 --regid=1001 --init-groups --inh-caps=-all "
                    "--ambient-caps=-all --bounding-set=-all --no-new-privs -- "
                    "/usr/local/bin/project-sandbox-entrypoint project-sandbox-run claude"
                ),
                "HOME=/home/agent",
                "PATH=/image/bin:/usr/bin:/bin",
            ],
        )

    def test_firewall_failure_exits_before_the_drop(self) -> None:
        for quiet in ("0", "1"):
            with self.subTest(quiet=quiet):
                self.log.unlink(missing_ok=True)
                self._firewall(exit_code=3)
                result = self._run(PROJECT_SANDBOX_QUIET=quiet)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(
                    any(line.startswith("setpriv") for line in self._log())
                )
                # Quiet mode hides the banner on success but shows it on failure.
                self.assertIn("Firewall banner", result.stdout + result.stderr)

    def test_quiet_success_hides_the_banner(self) -> None:
        self._firewall(exit_code=0)
        result = self._run(PROJECT_SANDBOX_QUIET="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Firewall banner", result.stdout + result.stderr)

    def test_firewall_disabled_still_drops(self) -> None:
        self._firewall(exit_code=1)
        result = self._run(PROJECT_SANDBOX_NO_FIREWALL="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self._log()
        self.assertFalse(any(line.startswith("firewall") for line in log))
        self.assertTrue(log[0].startswith("setpriv --reuid=1000 --regid=1001"))

    def test_refuses_to_run_as_non_root(self) -> None:
        self._firewall(exit_code=0)
        self._stub("id", "echo 1000")
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must start as root", result.stderr)
        self.assertEqual(self._log(), [])


class EntrypointTests(unittest.TestCase):
    def test_entrypoint_has_no_sudo_and_returns_to_the_workspace(self) -> None:
        for sidecar in (False, True):
            with (
                self.subTest(egress_sidecar=sidecar),
                tempfile.TemporaryDirectory() as tmp,
            ):
                text = dockerfile.render_entrypoint(
                    Path(tmp), egress_sidecar=sidecar
                ).read_text()
                self.assertNotIn("sudo", text)
                self.assertNotIn("init-firewall", text)
                commands = [
                    line
                    for line in text.splitlines()
                    if line and not line.startswith("#")
                ]
                self.assertEqual(commands[:2], ["set -eu", "cd /workspace"])
                self.assertIn("export UV_OFFLINE=1", text)


class DockerfileTests(unittest.TestCase):
    def _renders(self, root: Path) -> dict[str, str]:
        texts = {}
        base = root / "base"
        base.mkdir()
        texts["base image"] = dockerfile.render(
            base, base_image="python:3.12-slim"
        ).read_text()
        project = root / "project"
        context = project / ".project-sandbox"
        context.mkdir(parents=True)
        source = project / "Dockerfile"
        source.write_text("FROM ubuntu:24.04\nRUN echo hi\n", encoding="utf-8")
        texts["project Dockerfile"] = dockerfile.render(
            context, base_dockerfile=source, build_context=project
        ).read_text()
        texts["devcontainer"] = dockerfile.render(
            context, base_dockerfile=source, build_context=project, devcontainer=True
        ).read_text()
        return texts

    def test_no_rendered_dockerfile_grants_sudo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name, text in self._renders(Path(tmp)).items():
                with self.subTest(dockerfile=name):
                    self.assertNotIn("sudoers", text)
                    self.assertNotIn("NOPASSWD", text)

    def test_session_image_uses_the_root_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            texts = self._renders(Path(tmp))
        for name in ("base image", "project Dockerfile"):
            with self.subTest(dockerfile=name):
                text = texts[name]
                self.assertIn(" util-linux ", text)
                self.assertIn(
                    "project-sandbox-init /usr/local/bin/project-sandbox-init", text
                )
                self.assertIn("    command -v setpriv\n", text)
                self.assertIn(
                    f'LABEL {dockerfile.PRIVILEGE_MODEL_LABEL}="{dockerfile.PRIVILEGE_MODEL}"',
                    text,
                )
                tail = text[text.index("LABEL org.project-sandbox") :].splitlines()
                self.assertEqual(
                    tail[1:4],
                    [
                        "USER agent",
                        "WORKDIR /workspace",
                        'ENTRYPOINT ["/usr/local/bin/project-sandbox-init"]',
                    ],
                )
        self.assertNotIn("privilege-model", texts["devcontainer"])
        self.assertNotIn("project-sandbox-init ", texts["devcontainer"])


class RunArgvTests(unittest.TestCase):
    def _argv(self, runtime) -> list[str]:
        root = Path("/tmp/ps-test")
        return container_cli.build_run_argv(
            runtime=runtime,
            image="project-sandbox:test",
            project_abs=root / "workspace",
            claude_cfg=root / "claude/settings.json",
            codex_cfg=root / "codex/config.toml",
            codex_credentials_dir=None,
            identity=GitIdentity(None, None),
            memory="8g",
            cpus=4,
            extra_mounts=[],
            agent="claude",
            firewall_enabled=True,
            interactive=False,
        )

    def _user(self, argv: list[str]) -> str:
        self.assertEqual(argv.count("--user"), 1)
        return argv[argv.index("--user") + 1]

    def test_every_container_runtime_starts_the_root_stage_as_root(self) -> None:
        for runtime in (
            container_cli.APPLE_CONTAINER,
            container_cli.DOCKER,
            container_cli.PODMAN,
        ):
            with (
                self.subTest(runtime=runtime.name),
                patch.object(container_cli, "host_build_identity", return_value=None),
            ):
                argv = self._argv(runtime)
                self.assertEqual(self._user(argv), "0:0")
                self.assertLess(
                    argv.index("--user"), argv.index("project-sandbox:test")
                )

    @patch("project_sandbox.container_cli.os.getgid", return_value=1000)
    @patch("project_sandbox.container_cli.os.getuid", return_value=1000)
    @patch("project_sandbox.container_cli.sys.platform", "linux")
    def test_rootless_podman_keeps_keep_id(self, _uid, _gid) -> None:
        argv = self._argv(container_cli.PODMAN)
        self.assertIn("--userns=keep-id", argv)
        self.assertEqual(self._user(argv), "0:0")

    def test_chroot_argv_is_unchanged(self) -> None:
        root = Path("/tmp/ps-test")
        argv = container_cli.build_chroot_argv(
            script=root / "run", jail_root=root / "jail", mounts=[], agent="bash"
        )
        self.assertEqual(
            argv,
            [
                "unshare",
                "--map-root-user",
                "--mount",
                "--",
                str(root / "run"),
                str(root / "jail"),
                "--",
                "bash",
            ],
        )


def _inspect(labels: dict | None, runtime: str) -> str:
    """``image inspect`` output shaped like each runtime's (captured 2026-11-10)."""
    if runtime == "docker":
        image = {
            "Id": "sha256:d767",
            "RepoTags": ["probe:1"],
            "Config": {"Env": ["PATH=/usr/bin"], "WorkingDir": "/", "Labels": labels},
            "Architecture": "arm64",
        }
    elif runtime == "podman":
        image = {
            "Id": "a1a2",
            "RepoTags": ["localhost/probe:1"],
            "Config": {"Env": ["PATH=/usr/bin"], "Labels": labels},
            "Labels": labels,
        }
    else:
        config = {"Cmd": ["bash"], "Env": ["PATH=/usr/bin"]}
        if labels is not None:
            config["Labels"] = labels
        image = {
            "configuration": {"name": "probe:1"},
            "id": "4d94",
            "variants": [
                {
                    "config": {"architecture": "arm64", "config": config},
                    "digest": "sha256:ab6f",
                    "platform": {"architecture": "arm64", "os": "linux"},
                    "size": 1,
                }
            ],
        }
    return json.dumps([image])


class ImageLabelTests(unittest.TestCase):
    LABEL = dockerfile.PRIVILEGE_MODEL_LABEL

    def test_reads_the_label_from_each_runtime(self) -> None:
        labels = {self.LABEL: dockerfile.PRIVILEGE_MODEL, "io.buildah.version": "1"}
        for runtime in ("docker", "podman", "apple"):
            with self.subTest(runtime=runtime):
                self.assertEqual(
                    container_cli.image_label_from_inspect(
                        _inspect(labels, runtime), self.LABEL
                    ),
                    dockerfile.PRIVILEGE_MODEL,
                )
                for missing in (None, {}, {"other": "x"}):
                    self.assertIsNone(
                        container_cli.image_label_from_inspect(
                            _inspect(missing, runtime), self.LABEL
                        )
                    )

    def test_partly_labelled_or_malformed_output_is_not_trusted(self) -> None:
        image = json.loads(_inspect({self.LABEL: "root-stage-v1"}, "apple"))
        image[0]["variants"].append(
            {"config": {"config": {"Labels": {}}}, "platform": {"os": "linux"}}
        )
        for output in (json.dumps(image), "not json", "[]", "{}", '[{"Config": 1}]'):
            with self.subTest(output=output[:30]):
                self.assertIsNone(
                    container_cli.image_label_from_inspect(output, self.LABEL)
                )

    def test_image_label_runs_one_read_only_inspect(self) -> None:
        proc = MagicMock(returncode=0, stdout=_inspect({self.LABEL: "v"}, "docker"))
        with patch.object(container_cli.subprocess, "run", return_value=proc) as run:
            value = container_cli.image_label(container_cli.DOCKER, "img:1", self.LABEL)
        self.assertEqual(value, "v")
        self.assertEqual(run.call_args.args[0], ["docker", "image", "inspect", "img:1"])
        proc.returncode = 1
        with patch.object(container_cli.subprocess, "run", return_value=proc):
            self.assertIsNone(
                container_cli.image_label(container_cli.DOCKER, "img:1", self.LABEL)
            )


class NoBuildRefusalTests(unittest.TestCase):
    def _main(self, *extra: str, labels: dict | None):
        calls: list[list[str]] = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            return MagicMock(returncode=0, stdout=_inspect(labels, "docker"), stderr="")

        fake_subprocess = MagicMock()
        fake_subprocess.run.side_effect = fake_run
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "README.md").write_text("# demo\n", encoding="utf-8")
            with (
                patch.object(
                    cli, "read_identity", return_value=GitIdentity("Ada", "a@b.c")
                ),
                patch.object(
                    cli.container_cli,
                    "select_runtime",
                    return_value=cli.container_cli.DOCKER,
                ),
                patch.object(
                    cli.container_cli, "ensure_system_started", return_value=0
                ),
                patch.object(cli.container_cli, "subprocess", fake_subprocess),
                patch.object(cli.container_cli, "run") as run_container,
                patch.object(cli.session, "run", return_value=0) as run_session,
                patch.dict(os.environ, {"XDG_STATE_HOME": str(project / "state")}),
                contextlib.redirect_stdout(out),
            ):
                try:
                    rc = cli.main(
                        [
                            "--no-build",
                            "--no-forward-credentials",
                            *extra,
                            "--agent",
                            "bash",
                            "--prompt-text",
                            "echo ok",
                            str(project),
                            "python:3.12-slim",
                        ]
                    )
                except SystemExit as exc:
                    rc = exc
        return rc, calls, run_container, run_session, out.getvalue()

    def test_no_build_refuses_an_image_without_the_label(self) -> None:
        for labels in (None, {dockerfile.PRIVILEGE_MODEL_LABEL: "sudo-v0"}):
            with self.subTest(labels=labels):
                rc, calls, run_container, run_session, _ = self._main(labels=labels)
                self.assertIsInstance(rc, SystemExit)
                self.assertIn("drop --no-build", str(rc))
                self.assertIn("older project-sandbox", str(rc))
                self.assertIn(dockerfile.PRIVILEGE_MODEL_LABEL, str(rc))
                run_container.assert_not_called()
                run_session.assert_not_called()
                self.assertTrue(calls)
                for argv in calls:
                    self.assertEqual(argv[:3], ["docker", "image", "inspect"])

    def test_no_build_accepts_a_labelled_image(self) -> None:
        labels = {dockerfile.PRIVILEGE_MODEL_LABEL: dockerfile.PRIVILEGE_MODEL}
        rc, _calls, _run_container, run_session, _ = self._main(labels=labels)
        self.assertNotIsInstance(rc, SystemExit)
        run_session.assert_called_once()
        argv = run_session.call_args.args[0]
        self.assertEqual(argv[argv.index("--user") + 1], "0:0")

    def test_dry_run_only_inspects(self) -> None:
        rc, calls, run_container, run_session, out = self._main(
            "--dry-run", labels=None
        )
        self.assertEqual(rc, 0)
        run_container.assert_not_called()
        # The session runner only prints the argv in dry-run.
        self.assertIs(run_session.call_args.kwargs["dry_run"], True)
        # At most read-only inspects (currently none: dry-run probes no image).
        for argv in calls:
            self.assertEqual(argv[:3], ["docker", "image", "inspect"])
        self.assertIn("Would require image project-sandbox-", out)
        self.assertIn(
            f"{dockerfile.PRIVILEGE_MODEL_LABEL}={dockerfile.PRIVILEGE_MODEL}", out
        )


class BuildCacheTests(unittest.TestCase):
    def test_images_from_the_pre_change_template_are_rebuilt(self) -> None:
        """The label and root stage change the fingerprint of every image."""
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            dockerfile.render(context, base_image="python:3.12-slim")
            dockerfile.render_entrypoint(context)
            (context / "init-firewall.sh").write_text("#!/bin/bash\n")
            extra = {"base_image": "python:3.12-slim", "image_tag": "img:1"}
            # The pre-change render: sudoers rule, entrypoint as ENTRYPOINT, no
            # label and no root stage.
            current = (context / "Dockerfile").read_text()
            old = current.replace(
                f'LABEL {dockerfile.PRIVILEGE_MODEL_LABEL}="{dockerfile.PRIVILEGE_MODEL}"\n',
                "",
            ).replace(
                'ENTRYPOINT ["/usr/local/bin/project-sandbox-init"]',
                'ENTRYPOINT ["/usr/local/bin/project-sandbox-entrypoint"]',
            )
            self.assertNotEqual(old, current)
            (context / "Dockerfile").write_text(old)
            old_fingerprint = build_cache.compute_fingerprint(context, extra=extra)
            build_cache.write_state(
                context, image_tag="img:1", fingerprint=old_fingerprint
            )

            (context / "Dockerfile").write_text(current)
            dockerfile.render_root_stage(context)
            fingerprint = build_cache.compute_fingerprint(context, extra=extra)
            self.assertFalse(
                build_cache.is_cache_valid(
                    context, image_tag="img:1", fingerprint=fingerprint
                )
            )

    def test_root_stage_is_a_build_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            extra = {"image_tag": "img:1"}
            dockerfile.render_root_stage(context)
            before = build_cache.compute_fingerprint(context, extra=extra)
            stage = context / dockerfile.ROOT_STAGE_NAME
            stage.write_text(stage.read_text() + "# changed\n")
            self.assertNotEqual(
                before, build_cache.compute_fingerprint(context, extra=extra)
            )


if __name__ == "__main__":
    unittest.main()
