import contextlib
import io
import tempfile
from pathlib import Path
from unittest import TestCase

from project_sandbox import build_cache, cli, dockerfile

CERTIFICATE = Path(__file__).parent / "fixtures" / "test-ca.pem"


class CaCertificateTests(TestCase):
    def test_requires_proxy_before_writing(self):
        with self.assertRaisesRegex(SystemExit, "--ca-cert requires --internet-proxy"):
            cli.main(["--ca-cert", str(CERTIFICATE), ".", "debian:bookworm"])

    def test_invalid_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, data in (
                ("empty", ""),
                (
                    "invalid",
                    "-----BEGIN CERTIFICATE-----\ninvalid\n-----END CERTIFICATE-----",
                ),
                ("bundle", CERTIFICATE.read_text() * 2),
                ("key", CERTIFICATE.read_text() + "-----BEGIN PRIVATE KEY-----"),
            ):
                path = root / name
                path.write_text(data)
                with (
                    self.subTest(name=name),
                    self.assertRaisesRegex(SystemExit, "--ca-cert"),
                ):
                    dockerfile.read_ca_certificates([str(path)])
            for path in (root, root / "missing"):
                with self.assertRaisesRegex(SystemExit, "--ca-cert"):
                    dockerfile.read_ca_certificates([str(path)])

    def test_dry_run_does_not_stage_certificates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                rc = cli.main(
                    [
                        "--dry-run",
                        "--internet-proxy",
                        "http://127.0.0.1:18080",
                        "--ca-cert",
                        str(CERTIFICATE),
                        str(root),
                        "debian:bookworm",
                    ]
                )
            self.assertEqual(rc, 0)
            self.assertIn("Would bake CA certificate", output.getvalue())
            self.assertEqual(list(root.iterdir()), [])

    def test_render_all_sources_and_devcontainer_with_cache_invalidation(self):
        certificates = dockerfile.read_ca_certificates([str(CERTIFICATE)])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = root / "sandbox with spaces"
            context.mkdir()
            source = root / "Dockerfile"
            source.write_text(
                "FROM debian:bookworm AS prefix\nRUN echo prefix\nFROM prefix\nRUN echo project-build\n"
            )
            for custom in (False, True):
                kwargs = (
                    {"base_dockerfile": source, "build_context": root}
                    if custom
                    else {"base_image": "debian:bookworm"}
                )
                dockerfile.render(context, ca_certificates=certificates, **kwargs)
                staged = list(context.glob("project-sandbox-ca-*.crt"))
                self.assertEqual(len(staged), 1)
                self.assertEqual(staged[0].read_bytes(), certificates[0])
                for name in ("Dockerfile", "Dockerfile.devcontainer"):
                    text = (context / name).read_text()
                    self.assertIn("ENV NODE_USE_SYSTEM_CA=1", text)
                    self.assertIn(
                        'COPY ["'
                        + ("sandbox with spaces/" if custom else "")
                        + staged[0].name,
                        text,
                    )
                    self.assertGreater(
                        text.index("RUN update-ca-certificates"),
                        text.index("RUN npm install"),
                    )
                    if custom:
                        self.assertGreater(
                            text.index("RUN update-ca-certificates"),
                            text.index("RUN echo project-build"),
                        )
                    self.assertLess(
                        text.index("RUN update-ca-certificates"),
                        text.index("USER agent"),
                    )
                before = build_cache.compute_fingerprint(context, extra={})
                # A changed public PEM input must change the COPY source/cache key.
                dockerfile.render(
                    context, ca_certificates=(certificates[0] + b"\n",), **kwargs
                )
                self.assertNotEqual(
                    before, build_cache.compute_fingerprint(context, extra={})
                )
                self.assertEqual(len(list(context.glob("project-sandbox-ca-*.crt"))), 1)
                dockerfile.render(context, **kwargs)
                self.assertEqual(list(context.glob("project-sandbox-ca-*.crt")), [])
                self.assertNotIn(
                    "NODE_USE_SYSTEM_CA", (context / "Dockerfile").read_text()
                )

    def test_repeatable_inputs_with_same_basename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Staging uses contents, never the host filename; duplicate inputs deduplicate.
            cert = CERTIFICATE.read_bytes()
            dockerfile.render(
                root,
                base_image="debian:bookworm",
                ca_certificates=(cert, cert + b"\n", cert),
            )
            self.assertEqual(len(list(root.glob("project-sandbox-ca-*.crt"))), 2)
            args = cli.build_parser().parse_args(
                [
                    "--ca-cert",
                    "a/ca.pem",
                    "--ca-cert",
                    "b/ca.pem",
                    ".",
                    "debian:bookworm",
                ]
            )
            self.assertEqual(args.ca_cert, ["a/ca.pem", "b/ca.pem"])
