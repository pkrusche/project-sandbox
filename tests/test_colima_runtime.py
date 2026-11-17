"""Colima runtime selection and context-qualified command construction."""

import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import cli, container_cli, egress_sidecar, local_service_network
from project_sandbox.container_cli import (
    APPLE_CONTAINER,
    COLIMA_DEFAULT_PROFILE,
    DOCKER,
    PODMAN,
    build_run_argv,
    build_stop_argv,
    colima_context_name,
    colima_runtime,
    select_runtime,
)
from project_sandbox.git_identity import GitIdentity


def _run_argv(runtime):
    return build_run_argv(
        runtime=runtime,
        image="project-sandbox:test",
        project_abs=Path("/workspace"),
        claude_cfg=Path("/config/claude/settings.json"),
        codex_cfg=Path("/config/codex/config.toml"),
        codex_credentials_dir=None,
        identity=GitIdentity(None, None),
        memory="8g",
        cpus=4,
        extra_mounts=[],
        agent="bash",
        firewall_enabled=True,
        interactive=False,
    )


class ColimaSelectionTests(TestCase):
    def test_colima_uses_the_default_profile_context(self) -> None:
        runtime = select_runtime("colima", dry_run=True)
        self.assertEqual(runtime.name, "colima")
        self.assertEqual(runtime.binary, "docker")
        self.assertEqual(runtime.profile, COLIMA_DEFAULT_PROFILE)
        self.assertEqual(runtime.context, "colima-project-sandbox")

    def test_explicit_profile_selects_its_context(self) -> None:
        runtime = select_runtime("colima", dry_run=True, colima_profile="work")
        self.assertEqual(runtime.context, "colima-work")
        self.assertEqual(runtime.key, "colima-work")

    def test_colima_default_profile_uses_colimas_own_context_name(self) -> None:
        self.assertEqual(colima_context_name("default"), "colima")
        self.assertEqual(colima_runtime("default").context, "colima")

    def test_profile_flag_requires_the_colima_runtime(self) -> None:
        for requested in ("docker", "podman", "apple-container", "auto", "chroot"):
            with self.subTest(runtime=requested), self.assertRaises(SystemExit) as ctx:
                select_runtime(requested, dry_run=True, colima_profile="work")
            self.assertIn(
                "--colima-profile requires --runtime colima", str(ctx.exception)
            )

    def test_invalid_profile_names_are_rejected(self) -> None:
        for name in ("", "-x", "a b", "a/b", "../x", "x;y", "a" * 64):
            with self.subTest(name=name), self.assertRaises(SystemExit):
                colima_runtime(name)

    def test_missing_docker_cli_is_reported(self) -> None:
        with (
            patch("project_sandbox.container_cli.shutil.which", return_value=None),
            self.assertRaises(SystemExit) as ctx,
        ):
            select_runtime("colima")
        self.assertIn("docker CLI not found", str(ctx.exception))

    def test_auto_never_selects_colima(self) -> None:
        for platform in ("darwin", "linux"):
            with self.subTest(platform=platform):
                with (
                    patch("project_sandbox.container_cli.sys.platform", platform),
                    patch(
                        "project_sandbox.container_cli.shutil.which",
                        side_effect=lambda b: (
                            "/usr/bin/docker" if b == "docker" else None
                        ),
                    ),
                ):
                    self.assertEqual(select_runtime("auto"), DOCKER)

    def test_auto_keeps_apple_container_first_on_macos(self) -> None:
        with (
            patch("project_sandbox.container_cli.sys.platform", "darwin"),
            patch(
                "project_sandbox.container_cli.shutil.which",
                side_effect=lambda b: (
                    f"/usr/bin/{b}" if b in ("container", "docker") else None
                ),
            ),
        ):
            self.assertEqual(select_runtime("auto"), APPLE_CONTAINER)

    def test_cli_rejects_profile_flag_with_another_runtime(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            cli.main(
                [
                    "--dry-run",
                    "--runtime",
                    "docker",
                    "--colima-profile",
                    "work",
                    ".",
                    "python:3.12-slim",
                ]
            )
        self.assertIn("--colima-profile requires --runtime colima", str(ctx.exception))


class ContextQualifiedArgvTests(TestCase):
    def test_other_runtimes_argv_is_unchanged(self) -> None:
        for runtime in (APPLE_CONTAINER, DOCKER, PODMAN):
            with self.subTest(runtime=runtime.name):
                self.assertEqual(runtime.argv("ps", "-a"), [runtime.binary, "ps", "-a"])
                self.assertEqual(runtime.argv(), [runtime.binary])
                self.assertEqual(
                    build_stop_argv(runtime, "c1", grace=3),
                    [runtime.binary, "stop", "--time", "3", "c1"],
                )
                self.assertEqual(
                    _run_argv(runtime)[:3], [runtime.binary, "run", "--rm"]
                )

    def test_colima_argv_carries_the_context(self) -> None:
        runtime = colima_runtime()
        self.assertEqual(
            runtime.argv("ps"), ["docker", "--context", "colima-project-sandbox", "ps"]
        )
        self.assertEqual(
            build_stop_argv(runtime, "c1", grace=3),
            [
                "docker",
                "--context",
                "colima-project-sandbox",
                "stop",
                "--time",
                "3",
                "c1",
            ],
        )
        self.assertEqual(
            _run_argv(runtime)[:5],
            ["docker", "--context", "colima-project-sandbox", "run", "--rm"],
        )

    def test_sidecar_commands_carry_the_context(self) -> None:
        runtime = colima_runtime("work")
        prefix = ["docker", "--context", "colima-work"]
        for argv in (
            egress_sidecar.container_inspect_argv(runtime),
            egress_sidecar.exec_argv(runtime, "true"),
            egress_sidecar.logs_argv(runtime),
            egress_sidecar.remove_container_argv(runtime),
            egress_sidecar.remove_legacy_network_argv(runtime),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(argv[:3], prefix)

    def test_sidecar_run_argv_carries_the_context(self) -> None:
        runtime = colima_runtime("work")
        argv = egress_sidecar.sidecar_run_argv(
            runtime,
            image="img",
            sidecar_dir=Path("/state/sidecar"),
            port=3129,
            memory="1g",
            cpus=1,
            restart_fingerprint="fp",
        )
        self.assertEqual(argv[:5], ["docker", "--context", "colima-work", "run", "-d"])

    def test_every_runtime_command_in_the_package_is_context_qualified(self) -> None:
        """No call site may build a Docker command without Runtime.argv()."""
        source = Path(container_cli.__file__).parent
        offenders = []
        for path in source.glob("*.py"):
            for number, line in enumerate(path.read_text().splitlines(), 1):
                stripped = line.strip()
                if "runtime.binary" not in stripped:
                    continue
                if "shutil.which" in stripped or "CLI not found" in stripped:
                    continue
                if "not found on PATH" in stripped or path.name == "doctor.py":
                    continue  # doctor probes are covered by the doctor task
                # verify_colima_profile builds its own `context inspect` argv and
                # names the context itself; it is the one deliberate exception.
                if "runtime.binary, *args" in stripped or "Could not run" in stripped:
                    continue
                offenders.append(f"{path.name}:{number}: {stripped}")
        self.assertEqual(offenders, [])

    def test_bridge_gateway_probe_is_context_qualified(self) -> None:
        seen: list[list[str]] = []

        class Proc:
            returncode = 0
            stdout = "172.17.0.1\n"
            stderr = ""

        class InfoProc(Proc):
            stdout = "{}"

        def fake_run(argv, **_kwargs):
            seen.append(list(argv))
            return InfoProc() if "info" in argv else Proc()

        with patch("project_sandbox.local_service_network.subprocess.run", fake_run):
            local_service_network._bridge_gateway(colima_runtime())
            local_service_network._runtime_info(colima_runtime())
        for argv in seen:
            self.assertEqual(
                argv[:3], ["docker", "--context", "colima-project-sandbox"]
            )


class _Result:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_docker(*, endpoint: str | None, engine_up: bool = True):
    """A runner that answers ``context inspect`` and ``info`` and records argv."""
    calls: list[list[str]] = []

    def run(argv, **_kwargs):
        calls.append(list(argv))
        if "inspect" in argv:
            if endpoint is None:
                return _Result(1, stderr="context not found")
            return _Result(0, endpoint + "\n")
        if "info" in argv:
            return (
                _Result(0, "ID\n") if engine_up else _Result(1, stderr="cannot connect")
            )
        raise AssertionError(f"unexpected docker call: {argv}")

    return run, calls


class ProfileVerificationTests(TestCase):
    runtime = colima_runtime()
    socket = "unix:///Users/someone/.colima/project-sandbox/docker.sock"

    def test_running_profile_is_accepted_and_every_call_is_explicit(self) -> None:
        run, calls = _fake_docker(endpoint=self.socket)
        container_cli.verify_colima_profile(self.runtime, run=run)
        self.assertEqual(len(calls), 2)
        # The context lookup names the context; the engine call uses the flag.
        self.assertIn("colima-project-sandbox", calls[0])
        self.assertEqual(
            calls[1][:3], ["docker", "--context", "colima-project-sandbox"]
        )

    def test_missing_context_names_the_start_command(self) -> None:
        run, _ = _fake_docker(endpoint=None)
        with self.assertRaises(SystemExit) as ctx:
            container_cli.verify_colima_profile(self.runtime, run=run)
        self.assertIn("colima start --profile project-sandbox", str(ctx.exception))

    def test_stopped_profile_names_the_start_command(self) -> None:
        run, _ = _fake_docker(endpoint=self.socket, engine_up=False)
        with self.assertRaises(SystemExit) as ctx:
            container_cli.verify_colima_profile(self.runtime, run=run)
        message = str(ctx.exception)
        self.assertIn("not running", message)
        self.assertIn("colima start --profile project-sandbox", message)

    def test_foreign_endpoints_are_refused(self) -> None:
        for endpoint in (
            "unix:///var/run/docker.sock",
            "unix:///Users/someone/.docker/run/docker.sock",
            "unix:///Users/someone/.colima/other/docker.sock",  # another profile
            "unix:///Users/someone/.colima/project-sandbox/containerd.sock",
            "tcp://10.0.0.5:2375",
            "",
        ):
            with self.subTest(endpoint=endpoint):
                run, calls = _fake_docker(endpoint=endpoint)
                with self.assertRaises(SystemExit) as ctx:
                    container_cli.verify_colima_profile(self.runtime, run=run)
                self.assertIn("refusing to use it", str(ctx.exception))
                # No engine call is made against an unrecognized endpoint.
                self.assertEqual(len(calls), 1)

    def test_default_profile_context_is_accepted(self) -> None:
        run, _ = _fake_docker(
            endpoint="unix:///Users/someone/.colima/default/docker.sock"
        )
        container_cli.verify_colima_profile(colima_runtime("default"), run=run)

    def test_docker_cli_failure_is_reported(self) -> None:
        def run(*_args, **_kwargs):
            raise FileNotFoundError("docker")

        with self.assertRaises(SystemExit) as ctx:
            container_cli.verify_colima_profile(self.runtime, run=run)
        self.assertIn("Could not run docker", str(ctx.exception))

    def test_dry_run_performs_no_docker_or_colima_work(self) -> None:
        started: list[list[str]] = []

        def record(argv, *_args, **_kwargs):
            started.append(list(argv))
            return _Result(0, "")

        with (
            patch("subprocess.run", side_effect=record),
            patch("subprocess.Popen", side_effect=AssertionError("Popen")),
            patch("project_sandbox.container_cli.verify_colima_profile") as verify,
            patch("project_sandbox.container_cli.shutil.which", return_value=None),
        ):
            cli.main(
                [
                    "--dry-run",
                    "--runtime",
                    "colima",
                    "--agent",
                    "bash",
                    ".",
                    "python:3.12-slim",
                ]
            )
        verify.assert_not_called()
        self.assertEqual(
            [argv for argv in started if argv and argv[0] in ("docker", "colima")], []
        )

    def test_dry_run_commands_name_the_context(self) -> None:
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(
                [
                    "--dry-run",
                    "--runtime",
                    "colima",
                    "--colima-profile",
                    "work",
                    "--agent",
                    "bash",
                    ".",
                    "python:3.12-slim",
                ]
            )
        commands = [
            line
            for line in out.getvalue().splitlines()
            if " docker " in f" {line.split('&& ')[-1]}"
        ]
        self.assertTrue(commands)
        for line in commands:
            self.assertIn("docker --context colima-work ", line)


class ColimaSidecarDryRunTests(TestCase):
    """The dry run shows the private-network plan and asks Docker nothing."""

    argv = (
        "--dry-run",
        "--runtime",
        "colima",
        "--egress",
        "sidecar",
        "--agent",
        "bash",
        ".",
        "python:3.12-slim",
    )

    def run_cli(self, argv=None, *, config: str | None = None) -> str:
        import contextlib
        import io
        import os
        import tempfile

        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            env = {"XDG_CONFIG_HOME": tmp, "XDG_STATE_HOME": tmp}
            if config is not None:
                (Path(tmp) / "project-sandbox").mkdir()
                (Path(tmp) / "project-sandbox" / "egress.toml").write_text(config)
            with (
                patch.dict(os.environ, env),
                contextlib.redirect_stdout(out),
            ):
                cli.main(list(argv or self.argv))
        return out.getvalue()

    def sandbox_line(self, output: str) -> str:
        lines = [
            line
            for line in output.splitlines()
            if " run --rm " in line and "--name project-sandbox-2" in line
        ]
        self.assertEqual(len(lines), 1, output)
        return lines[0]

    def test_the_sandbox_joins_only_the_private_network_with_a_pinned_name(
        self,
    ) -> None:
        line = self.sandbox_line(self.run_cli())
        self.assertIn("--network psbx-sandbox", line)
        self.assertIn("--add-host 'egress:<sidecar address>'", line)
        self.assertNotIn("psbx-egress-uplink", line)
        self.assertNotIn("host.docker.internal", line)

    def test_proxy_variables_point_at_the_alias_on_the_fixed_port(self) -> None:
        output = self.run_cli()
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            self.assertIn(f"{name}=http://egress:3128", output)
        self.assertNotIn("host.docker.internal", output)
        self.assertNotIn("127.0.0.1:3129", output)

    def test_the_sidecar_is_planned_without_a_published_port(self) -> None:
        output = self.run_cli()
        run = next(
            line for line in output.splitlines() if line.startswith("Would run:")
        )
        self.assertNotIn("--publish", run)
        self.assertIn("--network psbx-egress-uplink", run)
        self.assertIn("net.ipv4.ip_forward=0", run)
        self.assertIn("Endpoint: egress:3128", output)
        self.assertNotIn("Published endpoint", output)

    def test_the_sidecar_is_created_attached_then_started(self) -> None:
        """isolated-sidecar-network task 3.1: Squid binds to the internal address."""
        runs = [
            line.removeprefix("Would run: ")
            for line in self.run_cli().splitlines()
            if line.startswith("Would run:")
        ]
        self.assertEqual(len(runs), 3)
        self.assertIn(" create --name project-sandbox-egress ", runs[0])
        self.assertTrue(
            runs[1].endswith(
                "network connect --alias egress psbx-sandbox project-sandbox-egress"
            )
        )
        self.assertTrue(runs[2].endswith(" start project-sandbox-egress"))

    def test_a_non_default_port_fails_before_any_work(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli(config="[egress.sidecar]\nport = 3140\n")
        message = str(ctx.exception)
        self.assertIn("[egress.sidecar] port = 3140", message)
        self.assertIn("not supported on Colima", message)

    def test_the_same_port_is_fine_on_docker(self) -> None:
        output = self.run_cli(
            ["docker" if a == "colima" else a for a in self.argv],
            config="[egress.sidecar]\nport = 3140\n",
        )
        self.assertIn("127.0.0.1:3140", output)

    def test_a_dry_run_starts_no_docker_or_colima_process(self) -> None:
        started: list[list[str]] = []

        def record(argv, *_a, **_k):
            started.append(list(argv))
            return _Result(0, "")

        with (
            patch("subprocess.run", side_effect=record),
            patch("subprocess.Popen", side_effect=AssertionError("Popen")),
        ):
            self.run_cli()
        self.assertEqual(
            [argv for argv in started if argv and argv[0] in ("docker", "colima")], []
        )


class HostDependentFeatureTests(TestCase):
    """Nothing that needs a sandbox-to-macOS path is emulated on Colima."""

    base = ("--dry-run", "--runtime", "colima", ".", "python:3.12-slim")

    def run_cli(self, *extra: str, config: str | None = None) -> str:
        import contextlib
        import io
        import os
        import tempfile

        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            if config is not None:
                (Path(tmp) / "project-sandbox").mkdir()
                (Path(tmp) / "project-sandbox" / "egress.toml").write_text(config)
            with (
                patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp, "XDG_STATE_HOME": tmp}),
                contextlib.redirect_stdout(out),
            ):
                cli.main([*self.base[:3], *extra, *self.base[3:]])
        return out.getvalue()

    def refused(self, *extra: str, config: str | None = None) -> str:
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli(*extra, config=config)
        message = str(ctx.exception)
        self.assertIn("Not supported with --runtime colima", message)
        return message

    def test_ollama_is_refused_naming_the_feature(self) -> None:
        self.assertIn("Ollama", self.refused("--agent", "pi", "--pi-ollama"))

    def test_a_gateway_on_the_mac_is_refused(self) -> None:
        message = self.refused(
            "--agent",
            "bash",
            "--model",
            "test-model",
            "--agent-proxy",
            "http://127.0.0.1:8080/v1",
        )
        self.assertIn("--agent-proxy", message)

    def test_a_loopback_internet_proxy_is_refused_in_firewall_mode(self) -> None:
        message = self.refused(
            "--agent", "bash", "--internet-proxy", "http://127.0.0.1:3128"
        )
        self.assertIn("--internet-proxy", message)

    def test_a_loopback_build_proxy_is_refused(self) -> None:
        message = self.refused(
            "--agent", "bash", "--build-proxy", "http://127.0.0.1:3128"
        )
        self.assertIn("--build-proxy", message)
        self.assertIn("cannot reach it", message)

    def test_a_network_reachable_build_proxy_is_supported(self) -> None:
        self.run_cli(
            "--agent", "bash", "--build-proxy", "http://proxy.corp.example:3128"
        )

    def test_a_loopback_parent_proxy_is_refused_before_the_sidecar_starts(self) -> None:
        message = self.refused(
            "--egress",
            "sidecar",
            "--agent",
            "bash",
            config='[egress.upstream]\nurl = "http://127.0.0.1:3128"\n',
        )
        self.assertIn("[egress.upstream]", message)
        self.assertIn("address the VM can reach", message)

    def test_every_unsupported_feature_is_listed_at_once(self) -> None:
        message = self.refused(
            "--agent",
            "bash",
            "--model",
            "test-model",
            "--agent-proxy",
            "http://localhost:8080/v1",
            "--internet-proxy",
            "http://127.0.0.1:3128",
            "--build-proxy",
            "http://127.0.0.1:3128",
        )
        for feature in ("--agent-proxy", "--internet-proxy", "--build-proxy"):
            self.assertIn(feature, message)

    def test_a_network_reachable_parent_proxy_is_supported(self) -> None:
        output = self.run_cli(
            "--egress",
            "sidecar",
            "--agent",
            "bash",
            config='[egress.upstream]\nurl = "http://proxy.corp.example:3128"\n',
        )
        self.assertIn("Parent proxy: http://proxy.corp.example:3128", output)
        self.assertIn("a direct connection from the sidecar", output)

    def test_a_remote_agent_proxy_is_not_rejected(self) -> None:
        self.run_cli(
            "--agent",
            "bash",
            "--model",
            "test-model",
            "--agent-proxy",
            "https://gateway.corp.example:8443/v1",
        )

    def test_other_runtimes_are_unaffected(self) -> None:
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(
                [
                    "--dry-run",
                    "--runtime",
                    "docker",
                    "--agent",
                    "bash",
                    "--model",
                    "test-model",
                    "--agent-proxy",
                    "http://127.0.0.1:8080/v1",
                    ".",
                    "python:3.12-slim",
                ]
            )


class FirewallModeParityTests(TestCase):
    """--egress firewall on Colima is Docker Engine's, apart from the context."""

    def dry_run(self, runtime: str, *extra: str) -> str:
        import contextlib
        import io
        import os
        import re
        import tempfile

        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            env = {"XDG_CONFIG_HOME": tmp, "XDG_STATE_HOME": tmp}
            # Colima is macOS-only; compare against Docker on the same host
            # platform. On Linux Docker builds for the host UID, which a macOS
            # Docker (Desktop/OrbStack) does not.
            with (
                patch.dict(os.environ, env),
                patch("project_sandbox.container_cli.sys.platform", "darwin"),
                contextlib.redirect_stdout(out),
            ):
                cli.main(
                    [
                        "--dry-run",
                        "--runtime",
                        runtime,
                        "--agent",
                        "bash",
                        *extra,
                        ".",
                        "python:3.12-slim",
                    ]
                )
            text = out.getvalue().replace(tmp, "TMP")
        text = text.replace(" --context colima-project-sandbox", "")
        text = re.sub(r"project-sandbox-2[0-9t]+-[0-9a-f]+", "NAME", text)
        return re.sub(r"session-inputs/[0-9a-z]+", "session-inputs/X", text)

    def test_default_firewall_mode_matches_docker(self) -> None:
        self.assertEqual(self.dry_run("colima"), self.dry_run("docker"))

    def test_explicit_firewall_mode_and_extra_domain_match_docker(self) -> None:
        extra = ("--egress", "firewall", "--extra-domain", "pypi.org")
        self.assertEqual(self.dry_run("colima", *extra), self.dry_run("docker", *extra))

    def test_a_remote_internet_proxy_matches_docker(self) -> None:
        extra = ("--internet-proxy", "http://proxy.corp.example:3128")
        self.assertEqual(self.dry_run("colima", *extra), self.dry_run("docker", *extra))

    def test_firewall_mode_uses_no_private_network_pieces(self) -> None:
        output = self.dry_run("colima")
        for piece in ("psbx-", "egress:3128", "--add-host", "--sysctl"):
            self.assertNotIn(piece, output)


class NoHostRelayTests(TestCase):
    """The Colima sidecar is reached inside the VM; the host binds nothing."""

    def configs(self):
        from project_sandbox import internet_proxy

        return internet_proxy.for_sidecar(3129), internet_proxy.for_private_sidecar()

    def test_the_private_sidecar_needs_no_host_relay(self) -> None:
        configured, sandbox = self.configs()
        self.assertIsNot(configured, sandbox)  # the condition that used to fire
        self.assertFalse(
            cli._needs_internet_proxy_relay(
                "bash", configured, sandbox, private_sidecar=True
            )
        )

    def test_apple_container_still_relays_a_proxy_that_differs(self) -> None:
        configured, sandbox = self.configs()
        self.assertTrue(
            cli._needs_internet_proxy_relay(
                "bash", configured, sandbox, private_sidecar=False
            )
        )

    def test_no_relay_without_a_proxy_a_run_or_a_difference(self) -> None:
        configured, sandbox = self.configs()
        for args in (
            ("bash", None, None),
            (None, configured, sandbox),
            ("bash", configured, configured),
        ):
            with self.subTest(args=args):
                self.assertFalse(
                    cli._needs_internet_proxy_relay(*args, private_sidecar=False)
                )


class SidecarServiceLaunchTests(TestCase):
    """isolated-sidecar-network tasks 5.1-5.3: Mac services through the sidecar."""

    SERVICES = (
        '[egress]\nallow = ["gateway.corp.example"]\n'
        "[egress.services]\nollama = { port = 11434 }\nagent-proxy = { port = 4000 }\n"
    )
    PI = ("--agent", "pi", "--pi-ollama")
    PROXY = ("--agent", "bash", "--model", "test-model", "--agent-proxy")

    def run_cli(self, *extra: str, runtime="colima", config: str | None = None) -> str:
        import contextlib
        import io
        import os
        import tempfile

        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            if config is not None:
                (Path(tmp) / "project-sandbox").mkdir()
                (Path(tmp) / "project-sandbox" / "egress.toml").write_text(config)
            with (
                patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp, "XDG_STATE_HOME": tmp}),
                contextlib.redirect_stdout(out),
            ):
                cli.main(
                    [
                        "--dry-run",
                        "--runtime",
                        runtime,
                        "--egress",
                        "sidecar",
                        *extra,
                        ".",
                        "python:3.12-slim",
                    ]
                )
        return out.getvalue().replace(tmp, "<tmp>")

    def refused(self, *extra: str, config: str | None = None, runtime="colima") -> str:
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli(*extra, config=config, runtime=runtime)
        return str(ctx.exception)

    def no_proxy(self, output: str) -> list[str]:
        line = next(
            line for line in output.splitlines() if "Would inject proxy" in line
        )
        return line.split("NO_PROXY=", 1)[1].split(", ", 1)[0].split(",")

    def assert_no_host_alias(self, output: str) -> None:
        self.assertNotIn("host.docker.internal", output)
        self.assertNotIn("host.lima.internal", output)
        self.assertNotIn("203.0.113.113", output)

    # --- 5.1 plan -------------------------------------------------------------

    def test_ollama_reaches_the_sandbox_only_through_the_sidecar(self) -> None:
        output = self.run_cli(*self.PI, config=self.SERVICES)
        self.assert_no_host_alias(output)
        self.assertIn("egress", self.no_proxy(output))
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            self.assertIn(f"{name}=http://egress:3128", output)
        sandbox = next(
            line
            for line in output.splitlines()
            if " run --rm " in line and "--name project-sandbox-2" in line
        )
        self.assertIn("--network psbx-sandbox", sandbox)
        self.assertEqual(sandbox.count("--add-host"), 1)
        self.assertIn("--add-host 'egress:<sidecar address>'", sandbox)
        plans = [line for line in output.splitlines() if line.startswith("Would use ")]
        self.assertFalse([p for p in plans if "Ollama" in p], plans)

    def test_a_loopback_gateway_is_the_sidecar_service(self) -> None:
        output = self.run_cli(
            *self.PROXY, "http://127.0.0.1:4000/v1", config=self.SERVICES
        )
        self.assert_no_host_alias(output)
        self.assertIn(
            "Would use agent proxy: http://egress:4000/v1 (sidecar service on port "
            "4000)",
            output,
        )
        self.assertIn("egress", self.no_proxy(output))

    def test_a_remote_gateway_goes_through_the_forward_proxy(self) -> None:
        output = self.run_cli(
            *self.PROXY, "https://gateway.corp.example/v1", config=self.SERVICES
        )
        self.assertIn(
            "Would use agent proxy: https://gateway.corp.example:443/v1 (through the "
            "egress sidecar's forward proxy)",
            output,
        )
        self.assertNotIn("gateway.corp.example", self.no_proxy(output))
        self.assertIn("egress", self.no_proxy(output))

    def test_other_runtimes_ignore_declared_services(self) -> None:
        for runtime in ("docker", "apple-container"):
            with self.subTest(runtime=runtime):
                plain = self.run_cli(*self.PI, runtime=runtime, config="")
                declared = self.run_cli(
                    *self.PI,
                    runtime=runtime,
                    config="[egress.services]\nollama = { port = 11434 }\n",
                )
                import re

                session = re.compile(r"\d{8}t\d{6}-[0-9a-f]{12}")
                self.assertEqual(
                    session.sub("<session>", plain), session.sub("<session>", declared)
                )
                self.assertIn("host.docker.internal", declared)
                self.assertNotIn("egress:11434", declared)

    # --- 5.2 declarations required --------------------------------------------

    def test_undeclared_ollama_names_the_command(self) -> None:
        message = self.refused(*self.PI, config="")
        self.assertIn("project-sandbox-egress service add ollama 11434", message)

    def test_undeclared_gateway_port_names_the_command(self) -> None:
        message = self.refused(
            *self.PROXY, "http://127.0.0.1:4001/v1", config=self.SERVICES
        )
        self.assertIn("project-sandbox-egress service add agent-proxy 4001", message)

    def test_a_loopback_https_gateway_is_refused(self) -> None:
        message = self.refused(
            *self.PROXY, "https://127.0.0.1:4000/v1", config=self.SERVICES
        )
        self.assertIn("HTTPS loopback endpoints are unsupported", message)

    def test_a_remote_gateway_must_be_allowlisted(self) -> None:
        message = self.refused(
            *self.PROXY, "https://other.corp.example/v1", config=self.SERVICES
        )
        self.assertIn("project-sandbox-egress allow add other.corp.example", message)

    def test_a_remote_gateway_on_another_port_is_refused(self) -> None:
        message = self.refused(
            *self.PROXY, "https://gateway.corp.example:8443/v1", config=self.SERVICES
        )
        self.assertIn("allows https only on port 443", message)

    def test_a_wildcard_allows_the_remote_gateway(self) -> None:
        self.run_cli(
            *self.PROXY,
            "https://gateway.corp.example/v1",
            config='[egress]\nallow = ["*.corp.example"]\n',
        )

    def test_services_are_not_required_on_other_runtimes(self) -> None:
        self.run_cli(*self.PI, runtime="docker", config="")

    # --- 5.3 the rejection is relaxed in sidecar mode only --------------------

    def test_firewall_mode_still_refuses_and_names_sidecar_mode(self) -> None:
        import contextlib
        import io

        for extra in (
            self.PI,
            (*self.PROXY, "http://127.0.0.1:4000/v1"),
        ):
            with (
                self.subTest(extra=extra),
                self.assertRaises(SystemExit) as ctx,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                cli.main(
                    [
                        "--dry-run",
                        "--runtime",
                        "colima",
                        *extra,
                        ".",
                        "python:3.12-slim",
                    ]
                )
            message = str(ctx.exception)
            self.assertIn("Not supported with --runtime colima", message)
            self.assertIn("--egress sidecar", message)

    def test_a_loopback_parent_stays_refused_in_sidecar_mode(self) -> None:
        message = self.refused(
            *self.PI,
            config=self.SERVICES + '[egress.upstream]\nurl = "http://127.0.0.1:3128"\n',
        )
        self.assertIn("[egress.upstream]", message)
