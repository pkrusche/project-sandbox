"""Build access is early, explicit, cache-sensitive, and independent of runtime policy."""

import contextlib
import io
import json
import os
import shlex
import ssl
import subprocess
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from project_sandbox import build_cache, build_network, cli, dockerfile, templating

CERTIFICATE = Path(__file__).parent / "fixtures/test-ca.pem"


class BuildEnvironmentTests(TestCase):
    def test_normalization_and_empty_lowercase_override(self):
        values = build_network.from_environment(
            {
                "http_proxy": "http://proxy.example:3128",
                "HTTP_PROXY": "http://other.example",
                "https_proxy": "",
                "HTTPS_PROXY": "https://ignored.example",
                "ALL_PROXY": "https://127.0.0.1:3129",
                "NO_PROXY": "localhost,.example.test",
                "UNRELATED_SECRET": "private",
            }
        )
        self.assertEqual(len(values), 8)
        for name in build_network.PROXY_NAMES:
            self.assertEqual(values[name], values[name.upper()])
        self.assertEqual(values["http_proxy"], "http://proxy.example:3128")
        self.assertEqual(values["https_proxy"], "")
        self.assertEqual(values["all_proxy"], "https://127.0.0.1:3129")
        self.assertEqual(values["no_proxy"], "localhost,.example.test")

    def test_invalid_environment_does_not_echo_input(self):
        for value in (
            "socks5://proxy.example",
            "http://user:private@proxy.example",
            "http://proxy.example:99999",
            "http://proxy.example/path",
            "http://proxy.example\nRUN evil",
            "http://proxy.example?private",
            "http://bad<host",
        ):
            with self.subTest(value=value), self.assertRaises(SystemExit) as exc:
                build_network.from_environment({"HTTP_PROXY": value})
            self.assertIn("--build-proxy-from-env", str(exc.exception))
            self.assertNotIn("private", str(exc.exception))
        for values in (
            {},
            {"NO_PROXY": "localhost"},
            {"http_proxy": "", "HTTP_PROXY": "http://proxy.example"},
        ):
            with (
                self.subTest(values=values),
                self.assertRaisesRegex(SystemExit, "no proxy URL"),
            ):
                build_network.from_environment(values)
        with self.assertRaisesRegex(SystemExit, "control characters"):
            build_network.from_environment(
                {"HTTP_PROXY": "http://proxy.example", "NO_PROXY": "localhost\nENV bad"}
            )

    def test_cli_validates_before_writing_and_dry_run_is_non_mutating(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for options in (
                ["--build-ca-cert", str(root / "missing")],
                ["--build-proxy-from-env"],
            ):
                with (
                    patch.dict(os.environ, {}, clear=True),
                    self.assertRaises(SystemExit),
                ):
                    cli.main([*options, str(root), "debian:bookworm"])
                self.assertEqual(list(root.iterdir()), [])
            with (
                patch.dict(
                    os.environ, {"HTTP_PROXY": "http://proxy.example:3128"}, clear=True
                ),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(
                    cli.main(
                        [
                            "--dry-run",
                            "--build-proxy-from-env",
                            "--build-ca-cert",
                            str(CERTIFICATE),
                            str(root),
                            "debian:bookworm",
                        ]
                    ),
                    0,
                )
            self.assertIn("before downloads", output.getvalue())
            self.assertIn("clear proxy settings", output.getvalue())
            self.assertNotIn("proxy.example", output.getvalue())
            self.assertEqual(list(root.iterdir()), [])

    def test_build_ca_bundle_is_split_and_dry_run_is_non_mutating(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "bundle.pem"
            pem = CERTIFICATE.read_text()
            bundle.write_text(pem + "\n\t" + pem)
            certificates = dockerfile.read_ca_certificates(
                [str(bundle)], option="--build-ca-cert", allow_bundle=True
            )
            self.assertEqual(certificates, (pem.encode(), pem.encode()))
            project = root / "project"
            project.mkdir()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    cli.main(
                        [
                            "--dry-run",
                            "--build-ca-cert",
                            str(bundle),
                            str(project),
                            "debian:bookworm",
                        ]
                    ),
                    0,
                )
            self.assertEqual(list(project.iterdir()), [])
            context = root / "context"
            context.mkdir()
            dockerfile.render(
                context,
                base_image="debian:bookworm",
                build_ca_certificates=certificates,
            )
            staged = list(context.glob("project-sandbox-build-ca-*.crt"))
            self.assertEqual(len(staged), 1)  # Identical certificates are deduplicated.
            self.assertEqual(staged[0].read_bytes(), pem.encode())

    def test_build_ca_bundle_validates_every_certificate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "bundle.pem"
            pem = CERTIFICATE.read_text()
            for invalid in (
                CERTIFICATE.with_name("test-leaf.pem").read_text(),
                "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----",
                "-----BEGIN CERTIFICATE-----\ninvalid\n-----END CERTIFICATE-----",
                "unrelated trailing data",
                ssl.DER_cert_to_PEM_cert(
                    ssl.PEM_cert_to_DER_cert(pem) + b"trailing DER data"
                ),
            ):
                for contents in (pem + invalid, invalid + pem):
                    bundle.write_text(contents)
                    with (
                        self.subTest(invalid=invalid),
                        self.assertRaisesRegex(SystemExit, "--build-ca-cert"),
                    ):
                        cli.main(
                            [
                                "--build-ca-cert",
                                str(bundle),
                                str(root / "project"),
                                "debian:bookworm",
                            ]
                        )
                    self.assertFalse((root / "project").exists())

    def test_build_ca_independent_and_chroot_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    cli.main(
                        [
                            "--dry-run",
                            "--build-ca-cert",
                            str(CERTIFICATE),
                            tmp,
                            "debian:bookworm",
                        ]
                    ),
                    0,
                )
            for option in (
                ["--build-ca-cert", str(CERTIFICATE)],
                ["--build-proxy-from-env"],
            ):
                with (
                    patch.dict(os.environ, {"HTTP_PROXY": "http://proxy.example"}),
                    self.assertRaisesRegex(SystemExit, "image-based runtime"),
                ):
                    cli.main(["--runtime", "chroot", "--agent", "bash", *option, tmp])
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_cli_build_settings_do_not_change_firewall_or_runtime_proxy_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plain = root / "plain"
            corporate = root / "corporate"
            plain.mkdir()
            corporate.mkdir()
            with (
                contextlib.redirect_stdout(io.StringIO()),
                patch.dict(os.environ, {"HTTP_PROXY": "http://proxy.example:3128"}),
            ):
                self.assertEqual(
                    cli.main(
                        ["--no-forward-credentials", str(plain), "debian:bookworm"]
                    ),
                    0,
                )
                self.assertEqual(
                    cli.main(
                        [
                            "--no-forward-credentials",
                            "--build-proxy-from-env",
                            "--build-ca-cert",
                            str(CERTIFICATE),
                            str(corporate),
                            "debian:bookworm",
                        ]
                    ),
                    0,
                )
            for name in ("init-firewall.sh", "init-firewall-devcontainer.sh"):
                self.assertEqual(
                    (plain / ".project-sandbox" / name).read_bytes(),
                    (corporate / ".project-sandbox" / name).read_bytes(),
                )
            devcontainer = (corporate / ".devcontainer/devcontainer.json").read_text()
            self.assertNotIn("proxy.example", devcontainer)


class BuildNetworkRendererTests(TestCase):
    def test_bootstrap_does_not_use_or_replace_inherited_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.Dockerfile"
            source.write_text(
                "FROM debian:bookworm AS prefix\n"
                'SHELL ["/bin/bash", "-euc"]\n'
                "RUN echo prefix\n"
                "FROM prefix AS final\n"
                "RUN echo final\n"
            )
            dockerfile.render(
                root,
                base_dockerfile=source,
                build_proxy_environment=build_network.from_environment(
                    {"HTTP_PROXY": "http://proxy.example"}
                ),
                build_ca_certificates=(CERTIFICATE.read_bytes(),),
            )
            for name in ("Dockerfile", "Dockerfile.devcontainer"):
                text = (root / name).read_text()
                self.assertIn('SHELL ["/bin/bash", "-euc"]', text)
                self.assertNotIn('SHELL ["/bin/sh"', text)
                bootstraps = [
                    json.loads(block[0].removeprefix("RUN "))
                    for block in dockerfile._dockerfile_blocks(text)
                    if block and block[0].startswith('RUN ["/bin/sh",')
                ]
                self.assertEqual(len(bootstraps), 3)
                for argv in bootstraps:
                    self.assertEqual(argv[:2], ["/bin/sh", "-c"])
                    self.assertIn("update-ca-certificates", argv[2])
                    self.assertIn("99project-sandbox-build-proxy", argv[2])

    def test_all_stages_early_setup_late_cleanup_and_separate_runtime_ca(self):
        certificates = dockerfile.read_ca_certificates([str(CERTIFICATE)])
        proxy = build_network.from_environment(
            {
                "HTTPS_PROXY": "https://proxy.example",
                "ALL_PROXY": "http://fallback.example:3128",
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = root / "sandbox with spaces"
            context.mkdir()
            source = root / "Dockerfile"
            source.write_text(
                "FROM scratch AS untouched\nFROM python:3.12-slim AS prefix\nRUN echo early\nFROM prefix AS final\nRUN echo late\n"
            )
            dockerfile.render(
                context,
                base_dockerfile=source,
                build_context=root,
                build_ca_certificates=certificates,
                build_proxy_environment=proxy,
                ca_certificates=certificates,
            )
            for name in ("Dockerfile", "Dockerfile.devcontainer"):
                text = (context / name).read_text()
                self.assertNotIn(
                    "ENV", text[text.index("FROM scratch") : text.index("FROM python")]
                )
                self.assertLess(
                    text.index("update-ca-certificates;"),
                    text.index("RUN echo early"),
                )
                self.assertLess(
                    text.index("NODE_EXTRA_CA_CERTS"), text.index("RUN npm install")
                )
                self.assertLess(
                    text.index("RUN echo late"), text.index("# Build proxy access")
                )
                self.assertLess(
                    text.index("# Build proxy access"), text.index("# Session trust")
                )
                self.assertGreater(
                    text.index("# Session trust"), text.index("RUN echo late")
                )
                self.assertIn(
                    'Acquire::http::Proxy "http://fallback.example:3128";',
                    text.replace('\\"', '"'),
                )
                self.assertIn(
                    'Acquire::https::Proxy "https://proxy.example";',
                    text.replace('\\"', '"'),
                )
                self.assertIn(
                    'COPY ["sandbox with spaces/project-sandbox-build-ca-', text
                )
                self.assertEqual(text.count("# Corporate access"), 3)
                for key in proxy:
                    self.assertIn(
                        f'ENV {key}=""', text[text.index("# Build proxy access") :]
                    )
            self.assertEqual(
                len(list(context.glob("project-sandbox-build-ca-*.crt"))), 1
            )
            self.assertEqual(len(list(context.glob("project-sandbox-ca-*.crt"))), 1)

    def test_cache_changes_and_stale_certificates_cleanup(self):
        cert = CERTIFICATE.read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def render(proxy="http://proxy.example", certificates=(cert,)):
                dockerfile.render(
                    root,
                    base_image="debian:bookworm",
                    build_ca_certificates=certificates,
                    build_proxy_environment=build_network.from_environment(
                        {"HTTP_PROXY": proxy}
                    ),
                )
                return build_cache.compute_fingerprint(root, extra={})

            original = render()
            self.assertNotEqual(original, render(proxy="http://different.example"))
            self.assertNotEqual(original, render(certificates=(cert + b"\n",)))
            self.assertEqual(len(list(root.glob("project-sandbox-build-ca-*.crt"))), 1)
            dockerfile.render(root, base_image="debian:bookworm")
            self.assertEqual(list(root.glob("project-sandbox-build-ca-*.crt")), [])
            self.assertNotIn("Corporate access", (root / "Dockerfile").read_text())

    def test_generated_languages_and_numeric_stage_references(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            python = dockerfile.render_python_uv_dockerfile(root, "3.12", True, True)
            rust = dockerfile.render_rust_cargo_dockerfile(root, None, True, True)
            node = dockerfile.render_node_npm_dockerfile(
                root, lockfiles=["package-lock.json"]
            )
            custom = root / "custom.Dockerfile"
            custom.write_text(
                "FROM debian AS tools\nRUN echo tool\nFROM debian\nCOPY --from=0 /tmp/tool /tmp/tool\nRUN echo final\n"
            )
            for source in (python, rust, node, custom):
                with self.subTest(source=source):
                    dockerfile.render(
                        root,
                        base_dockerfile=source,
                        build_context=root,
                        build_proxy_environment=build_network.from_environment(
                            {"HTTP_PROXY": "http://proxy.example"}
                        ),
                    )
                    text = (root / "Dockerfile").read_text()
                    self.assertLess(
                        text.index("ENV http_proxy="), text.index("apt-get update")
                    )
                    if source == python:
                        self.assertNotIn(
                            "Corporate access",
                            text[
                                text.index("FROM ghcr.io") : text.index("FROM python")
                            ],
                        )
                    if source == custom:
                        self.assertIn("COPY --from=0 /tmp/tool /tmp/tool", text)

    def test_no_proxy_data_uses_docker_escaping(self):
        value = 'localhost,$HOME,"literal",\\path,`literal`'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dockerfile.render(
                root,
                base_image="debian:bookworm",
                build_proxy_environment=build_network.from_environment(
                    {"HTTP_PROXY": "http://proxy.example", "NO_PROXY": value}
                ),
            )
            text = (root / "Dockerfile").read_text()
            self.assertIn(
                'ENV no_proxy="localhost,\\$HOME,\\"literal\\",\\\\path,`literal`"',
                text,
            )

    def test_bootstrap_works_without_existing_ca_tools_or_bundle(self):
        # Execute the actual rendered bootstrap in a fake root. The fake apt
        # verifies CA trust and proxy configuration exist before any download.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bindir = root / "bin"
            bindir.mkdir()
            for name in ("mkdir", "touch", "cat", "chmod", "rm"):
                path = Path("/usr/bin") / name
                (bindir / name).symlink_to(path)
            certdir = root / "usr/local/share/ca-certificates"
            certdir.mkdir(parents=True)
            (certdir / "project-sandbox-build-ca-test.crt").write_bytes(
                CERTIFICATE.read_bytes()
            )
            apt = bindir / "apt-get"
            apt.write_text("""#!/bin/sh
set -eu
test -s "$FAKE_ROOT/etc/ssl/certs/ca-certificates.crt"
test -s "$FAKE_ROOT/etc/apt/apt.conf.d/99project-sandbox-build-ca"
test -s "$FAKE_ROOT/etc/apt/apt.conf.d/99project-sandbox-build-proxy"
printf '%s\\n' "$*" >> "$FAKE_ROOT/apt-calls"
if [ "$1" = install ]; then
  printf '#!/bin/sh\\nprintf updated > "$FAKE_ROOT/updated"\\n' > "$FAKE_ROOT/bin/update-ca-certificates"
  chmod +x "$FAKE_ROOT/bin/update-ca-certificates"
fi
""")
            apt.chmod(0o755)
            setup = templating.get_template("_build-network.Dockerfile.j2").render(
                build_proxy_environment=build_network.from_environment(
                    {"HTTP_PROXY": "http://proxy.example"}
                ),
                build_ca_certificate_sources=["cert.crt"],
            )
            blocks = dockerfile._dockerfile_blocks(setup)
            instruction = next(
                dockerfile._flatten_dockerfile_block(block).removeprefix("RUN ")
                for block in blocks
                if block and block[0].startswith("RUN ")
            )
            argv = json.loads(instruction)
            self.assertEqual(argv[:2], ["/bin/sh", "-c"])
            command = argv[2]
            for path in ("/etc/", "/usr/local/", "/var/lib/"):
                command = command.replace(path, shlex.quote(str(root)) + path)
            result = subprocess.run(
                [*argv[:2], command],
                env={"PATH": str(bindir), "FAKE_ROOT": str(root)},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / "updated").read_text(), "updated")
            self.assertEqual(
                (root / "apt-calls").read_text().splitlines(),
                ["update", "install -y --no-install-recommends ca-certificates"],
            )
