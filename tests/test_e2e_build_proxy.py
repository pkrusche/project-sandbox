"""Exercise the Squid acceptance harness without running external services."""

import argparse
import contextlib
import hashlib
import importlib.util
import io
import itertools
import json
import os
import runpy
import shlex
import ssl
import subprocess
import tempfile
import tomllib
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

from project_sandbox import container_cli, internet_proxy

ROOT = Path(__file__).parents[1]
CERTIFICATE = Path(__file__).parent / "fixtures/test-ca.pem"


def checker():
    spec = importlib.util.spec_from_file_location(
        "build_proxy_e2e", ROOT / "scripts/e2e-build-proxy.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture_source(root):
    source = root / "original"
    (source / "src/internet_proxy_locally").mkdir(parents=True)
    (source / "src/internet_proxy_locally/__init__.py").touch()
    (source / "pyproject.toml").write_text(
        '[project]\nname = "internet-proxy-locally"\nversion = "0.1.0"\n'
    )
    (source / "config.toml").write_text(
        '[policy]\nallow = ["github.com"]\n[fixture]\ncontrol = "unchanged"\n'
    )
    (source / "state/ca").mkdir(parents=True)
    (source / "state/ca/ca-key.pem").write_text("original private key")
    return source


class BuildProxyHarnessTests(TestCase):
    def test_isolated_configuration_preserves_original_and_omits_private_keys(self):
        module = checker()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = fixture_source(root)
            original = (source / "config.toml").read_bytes()
            module.prepare_fixture(source, root / "fixture")
            self.assertEqual((source / "config.toml").read_bytes(), original)
            self.assertFalse((root / "fixture/state").exists())
            config = tomllib.loads((root / "fixture/config.toml").read_text())
            self.assertTrue(set(module.BUILD_HOSTS) <= set(config["policy"]["allow"]))
            self.assertEqual(config["fixture"]["control"], "unchanged")

    def test_failures_must_be_specific_and_uncached(self):
        module = checker()
        module.check_build(0, "", "success")
        module.check_build(100, "Certificate verification failed", "ca")
        module.check_build(7, "Failed to connect to proxy", "proxy")
        for expected in ("ca", "proxy"):
            for code, output in (
                (0, "cached"),
                (1, "unrelated error"),
                (1, "Dockerfile changed"),
            ):
                with (
                    self.subTest(expected=expected, code=code, output=output),
                    self.assertRaises(RuntimeError),
                ):
                    module.check_build(code, output, expected)
        with self.assertRaises(RuntimeError):
            module.check_build(1, "failed", "success")

    def test_addresses_and_environment_cannot_reuse_an_existing_instance(self):
        module = checker()
        for value in (
            "127.0.0.1",
            "::1",
            "0.0.0.0",
            "ff02::1",
            "fe80::1%eth0",
            "localhost",
        ):
            with (
                self.subTest(value=value),
                self.assertRaises(argparse.ArgumentTypeError),
            ):
                module.fixture_host(value)
        with patch.dict(
            os.environ,
            {
                "http_proxy": "unrelated",
                "ALL_PROXY": "unrelated",
                "IPL_ROOT": "/original",
                "IPL_ENDPOINT": "original",
                "PATH": "/bin",
            },
            clear=True,
        ):
            self.assertEqual(module.clean_environment(), {"PATH": "/bin"})

    def test_orchestration_success_and_failure_always_clean_up_squid(self):
        for runtime, failure in itertools.product(
            (container_cli.DOCKER, container_cli.APPLE_CONTAINER),
            (None, "up", "build", "allowed-build", "logs", "down"),
        ):
            with (
                self.subTest(runtime=runtime.name, failure=failure),
                tempfile.TemporaryDirectory() as tmp,
            ):
                module = checker()
                root = Path(tmp)
                source = fixture_source(root)
                artifacts = root / "artifacts"
                artifacts.mkdir()
                calls = []
                scenarios = []
                built_policies = {}
                successful_source = []

                def run(
                    command,
                    log,
                    *,
                    calls=calls,
                    artifacts=artifacts,
                    module=module,
                    failure=failure,
                    scenarios=scenarios,
                    built_policies=built_policies,
                    successful_source=successful_source,
                    runtime=runtime,
                    **kwargs,
                ):
                    calls.append(command)
                    code, output = 0, ""
                    if "--engine" in command:
                        self.assertEqual(
                            command[command.index("--engine") + 1], "squid"
                        )
                        self.assertEqual(
                            command[command.index("--ip") + 1],
                            "127.0.0.1"
                            if runtime == container_cli.APPLE_CONTAINER
                            else "192.0.2.1",
                        )
                        self.assertEqual(
                            kwargs["env"]["IPL_ROOT"], str(artifacts / "squid")
                        )
                        self.assertNotIn("HTTP_PROXY", kwargs["env"])
                        if "--out" in command:
                            Path(command[command.index("--out") + 1]).write_bytes(
                                CERTIFICATE.read_bytes()
                            )
                        if command[-1] == "logs":
                            output = "\n".join(
                                "https://" + host + "/probe"
                                for host in module.BUILD_HOSTS
                            )
                        if failure in ("up", "logs", "down") and failure in command:
                            code = 1
                    elif "--build-only" in command:
                        self.assertNotIn("--ca-cert", command)
                        self.assertIn("--no-verify-dockerfile", command)
                        allowed = "--internet-proxy" in command
                        if not allowed:
                            self.assertIn("--force-build", command)
                            scenarios.append(command)
                        source_text = Path(
                            command[command.index("--dockerfile") + 1]
                        ).read_text()
                        if len(scenarios) == 1:
                            successful_source.append(source_text)
                        if allowed:
                            self.assertEqual(source_text, successful_source[0])
                            self.assertIn("--build-ca-cert", command)
                        built_policies[command[command.index("--image-tag") + 1]] = (
                            allowed
                        )
                        self.assertNotIn("HTTP_PROXY", kwargs["env"])
                        self.assertNotIn("HTTPS_PROXY", kwargs["env"])
                        if len(scenarios) != 3 or allowed:
                            self.assertEqual(
                                command[command.index("--build-proxy") + 1],
                                "http://192.168.64.9:3128"
                                if runtime == container_cli.APPLE_CONTAINER
                                else "http://192.0.2.1:3128",
                            )
                        if allowed:
                            if failure == "allowed-build":
                                code, output = 1, "allowed-policy build failed"
                        elif len(scenarios) == 2:
                            self.assertNotIn("--build-ca-cert", command)
                            code, output = 100, "Certificate verification failed"
                        elif len(scenarios) == 3:
                            code, output = 7, "Failed to connect"
                        elif failure == "build":
                            code, output = 1, "an unrelated build error"
                        else:
                            self.assertIn("--build-ca-cert", command)
                    elif "--prompt-text" in command:
                        audit_args = shlex.split(
                            command[command.index("--prompt-text") + 1]
                        )
                        phase = audit_args[2]
                        image = command[command.index("--image-tag") + 1]
                        self.assertEqual(built_policies[image], phase == "allowed")
                        expected = internet_proxy.for_runtime(
                            internet_proxy.parse(audit_args[3]), runtime
                        )
                        assert expected is not None
                        self.assertEqual(audit_args[4], expected.forwarded_url)
                        self.assertEqual(audit_args[6], "198.51.100.7")
                        self.assertIn("--no-build", command)
                        self.assertEqual(
                            "--internet-proxy" in command, phase == "allowed"
                        )
                        self.assertNotIn("HTTP_PROXY", kwargs["env"])
                        Path(
                            command[-2], f"build-proxy-runtime-{phase}.json"
                        ).write_text('{"ok": true}')
                    with log.open("a") as handle:
                        handle.write(output + "\n")
                    return subprocess.CompletedProcess(command, code)

                reservation = MagicMock()
                reservation.__enter__.return_value.getsockname.return_value = (
                    "192.0.2.1",
                    3128,
                )
                with (
                    patch.object(
                        module.container_cli,
                        "select_runtime",
                        return_value=runtime,
                    ),
                    patch.object(module.shutil, "which", return_value="/bin/uv"),
                    patch.object(
                        module.tempfile, "mkdtemp", return_value=str(artifacts)
                    ),
                    patch.object(module, "reserved_port", return_value=reservation),
                    patch.object(module, "run_logged", side_effect=run),
                    patch.object(
                        module, "squid_address", return_value="192.168.64.9"
                    ) as address,
                    patch.object(module, "direct_address", return_value="198.51.100.7"),
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    result = module.main(
                        [
                            "--runtime",
                            runtime.name,
                            "--internet-proxy-dir",
                            str(source),
                            "--fixture-host",
                            "192.0.2.1",
                            "--keep",
                        ]
                    )
                self.assertEqual(result, 0 if failure is None else 1)
                if runtime == container_cli.APPLE_CONTAINER and failure != "up":
                    address.assert_called_once()
                    self.assertEqual(address.call_args.args[0], ("127.0.0.1", 3128))
                else:
                    address.assert_not_called()
                self.assertTrue(
                    any(
                        command[-2:] == ["setup", "--tls-interception"]
                        for command in calls
                    )
                )
                self.assertTrue(
                    any(
                        command[-2:] == ["up", "--tls-interception"]
                        for command in calls
                    )
                )
                self.assertTrue(any(command[-1] == "down" for command in calls))
                self.assertEqual(
                    json.loads((artifacts / "result.json").read_text())["ok"],
                    failure is None,
                )
                self.assertEqual(
                    (source / "state/ca/ca-key.pem").read_text(), "original private key"
                )

    def test_squid_address_asks_ipl_for_the_published_instance(self):
        module = checker()
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "squid.log"
            for stdout, code in (("192.168.64.9\n", 0), ("", 0), ("x\n", 1)):
                with (
                    self.subTest(stdout=stdout, code=code),
                    patch.object(
                        module.subprocess,
                        "run",
                        return_value=subprocess.CompletedProcess([], code, stdout, ""),
                    ) as run,
                ):
                    if code or not stdout:
                        with self.assertRaises(RuntimeError):
                            module.squid_address(
                                ("127.0.0.1", 41000), log, cwd=Path(tmp), env={}
                            )
                        continue
                    self.assertEqual(
                        module.squid_address(
                            ("127.0.0.1", 41000), log, cwd=Path(tmp), env={}
                        ),
                        "192.168.64.9",
                    )
                    self.assertEqual(run.call_args.args[0][-2:], ["127.0.0.1", "41000"])
                    self.assertEqual(run.call_args.kwargs["cwd"], Path(tmp))

    def test_runtime_audit_rejects_proxy_access_and_direct_bypass(self):
        certificate = ssl.PEM_cert_to_DER_cert(CERTIFICATE.read_text())
        fingerprint = hashlib.sha256(certificate).hexdigest()
        for runtime, (phase, codes, ok) in itertools.product(
            (container_cli.DOCKER, container_cli.APPLE_CONTAINER),
            (
                ("blocked", [7], True),
                ("blocked", [0], False),
                ("blocked", [60], False),
                ("allowed", [0, 7, 7], True),
                ("allowed", [0, 6, 28], True),
                ("allowed", [0, 6, 0], False),
                ("allowed", [0, 6, 6], False),
                ("allowed", [0, 0, 7], False),
                ("allowed", [60, 7, 7], False),
            ),
        ):
            with (
                self.subTest(phase=phase, codes=codes),
                tempfile.TemporaryDirectory() as tmp,
            ):
                proxy = "http://192.0.2.1:3128"
                runtime_config = internet_proxy.for_runtime(
                    internet_proxy.parse(proxy), runtime
                )
                assert runtime_config is not None
                runtime_proxy = runtime_config.forwarded_url
                context = MagicMock()
                context.get_ca_certs.return_value = [certificate]
                with (
                    contextlib.chdir(tmp),
                    patch.dict(
                        os.environ,
                        {"https_proxy": runtime_proxy} if phase == "allowed" else {},
                        clear=True,
                    ),
                    patch(
                        "sys.argv",
                        [
                            "audit.py",
                            phase,
                            proxy,
                            runtime_proxy,
                            fingerprint,
                            "198.51.100.7",
                        ],
                    ),
                    patch("ssl.create_default_context", return_value=context),
                    patch(
                        "subprocess.run",
                        side_effect=[
                            subprocess.CompletedProcess([], code, stderr="probe")
                            for code in codes
                        ],
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                    self.assertRaises(SystemExit) as exc,
                ):
                    runpy.run_path(
                        str(ROOT / "scripts/fixtures/build-proxy-audit.py"),
                        run_name="__main__",
                    )
                self.assertEqual(exc.exception.code, not ok)
                self.assertEqual(
                    json.loads(
                        Path(tmp, f"build-proxy-runtime-{phase}.json").read_text()
                    )["ok"],
                    ok,
                )

    def test_runner_requires_builds_and_dispatches_the_selected_suite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_uv = root / "uv"
            fake_uv.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$@" > "$BUILD_PROXY_ARGUMENT_LOG"\n'
            )
            fake_uv.chmod(0o755)
            arguments = root / "arguments"
            command = [
                "bash",
                str(ROOT / "scripts/run-e2e-tests.sh"),
                "--only",
                "build-proxy",
                "--runtime",
                "docker",
                "--internet-proxy-dir",
                str(root),
                "--proxy-fixture-host",
                "192.0.2.1",
            ]
            env = {
                **os.environ,
                "PATH": str(root) + os.pathsep + os.environ["PATH"],
                "BUILD_PROXY_ARGUMENT_LOG": str(arguments),
            }
            result = subprocess.run(
                [*command, "--no-build"],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertFalse(arguments.exists())
            result = subprocess.run(
                command, env=env, capture_output=True, text=True, check=False
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(
                arguments.read_text().splitlines(),
                [
                    "run",
                    "python",
                    str(ROOT / "scripts/e2e-build-proxy.py"),
                    "--runtime",
                    "docker",
                    "--base-image",
                    "python:3.12-slim",
                    "--internet-proxy-dir",
                    str(root),
                    "--fixture-host",
                    "192.0.2.1",
                ],
            )
