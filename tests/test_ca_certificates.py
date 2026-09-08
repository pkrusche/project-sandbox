import contextlib
import io
import ssl
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
                (
                    "trailing-der-content",
                    ssl.DER_cert_to_PEM_cert(
                        ssl.PEM_cert_to_DER_cert(CERTIFICATE.read_text())
                        + b"sensitive trailing data"
                    ),
                ),
                (
                    "trailing-base64-content",
                    CERTIFICATE.read_text().replace(
                        "-----END CERTIFICATE-----",
                        "c2Vuc2l0aXZl\n-----END CERTIFICATE-----",
                    ),
                ),
                ("key", CERTIFICATE.read_text() + "-----BEGIN PRIVATE KEY-----"),
                (
                    "trailing-content",
                    CERTIFICATE.read_text()
                    + "sensitive trailing text\n-----END CERTIFICATE-----",
                ),
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

    def test_accepts_pem_whitespace_and_crlf(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ca.pem"
            pem = CERTIFICATE.read_text().replace("\n", "\r\n")
            path.write_bytes((" \r\n" + pem + "\t \r\n").encode("ascii"))
            certificates = dockerfile.read_ca_certificates([str(path)])
            self.assertEqual(len(certificates), 1)
            self.assertEqual(
                ssl.PEM_cert_to_DER_cert(certificates[0].decode("ascii")),
                ssl.PEM_cert_to_DER_cert(CERTIFICATE.read_text()),
            )

    def test_rejects_non_ca_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(SystemExit, "expected a CA certificate"):
                cli.main(
                    [
                        "--internet-proxy",
                        "http://127.0.0.1:18080",
                        "--ca-cert",
                        str(CERTIFICATE.with_name("test-leaf.pem")),
                        str(root),
                        "debian:bookworm",
                    ]
                )
            self.assertEqual(list(root.iterdir()), [])

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

                def render(
                    ca_certificates: tuple[bytes, ...] = (), *, custom: bool = custom
                ) -> None:
                    if custom:
                        dockerfile.render(
                            context,
                            ca_certificates=ca_certificates,
                            base_dockerfile=source,
                            build_context=root,
                        )
                    else:
                        dockerfile.render(
                            context,
                            ca_certificates=ca_certificates,
                            base_image="debian:bookworm",
                        )

                render(ca_certificates=certificates)
                staged = list(context.glob("project-sandbox-ca-*.crt"))
                self.assertEqual(len(staged), 1)
                self.assertEqual(staged[0].read_bytes(), certificates[0])
                for name in ("Dockerfile", "Dockerfile.devcontainer"):
                    text = (context / name).read_text()
                    self.assertIn("ENV NODE_USE_SYSTEM_CA=1", text)
                    self.assertIn(
                        "RUN chmod 0644 /usr/local/share/ca-certificates/"
                        "project-sandbox-ca-*.crt\nRUN update-ca-certificates",
                        text,
                    )
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
                render(ca_certificates=(certificates[0] + b"\n",))
                self.assertNotEqual(
                    before, build_cache.compute_fingerprint(context, extra={})
                )
                self.assertEqual(len(list(context.glob("project-sandbox-ca-*.crt"))), 1)
                render()
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
