"""Rendered Squid config and egress sidecar image (egress-sidecar-macos group 2)."""

import json
import subprocess
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from project_sandbox import build_network, sidecar_image, squid_config


class SidecarImageRendererTests(TestCase):
    """Squid egress sidecar build context (egress-sidecar-macos task 2.2)."""

    def setUp(self) -> None:
        self.files = sidecar_image.render_files()

    def test_dockerfile_pins_base_and_squid(self) -> None:
        dockerfile_text = self.files["Dockerfile"]
        self.assertIn(f"FROM {sidecar_image.BASE_IMAGE}\n", dockerfile_text)
        self.assertRegex(
            sidecar_image.BASE_IMAGE, r"^alpine:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}$"
        )
        self.assertIn(
            f"apk add --no-cache {sidecar_image.SQUID_PACKAGE} su-exec \\\n",
            dockerfile_text,
        )
        self.assertRegex(sidecar_image.SQUID_PACKAGE, r"^squid~\d+\.\d+$")
        self.assertIn("security_file_certgen", dockerfile_text)
        self.assertIn("COPY sni-match /usr/local/libexec/sni-match", dockerfile_text)

    def test_copy_sources_are_files_not_directories(self) -> None:
        # Regression: Apple container 1.4.1's builder silently copies nothing
        # for a directory source, which left the labelled error pages out.
        for line in self.files["Dockerfile"].splitlines():
            if not line.startswith("COPY "):
                continue
            spec = line[len("COPY ") :]
            sources = (
                json.loads(spec)[:-1] if spec.startswith("[") else spec.split()[:-1]
            )
            for source in sources:
                self.assertIn(
                    source, self.files, f"COPY source {source!r} is not a rendered file"
                )
        pages = [n for n in self.files if n.startswith("errors/")]
        self.assertEqual(len(pages), 6)
        self.assertIn(
            json.dumps([*sorted(pages), "/etc/squid/sbx-errors/"]),
            self.files["Dockerfile"],
        )

    def test_entrypoint_stages_ca_and_drops_privileges(self) -> None:
        entry = self.files["entrypoint.sh"]
        self.assertIn('cp "$INPUT/ca.pem" "$INPUT/ca-key.pem" "$STATE/"', entry)
        self.assertIn("STATE=/run/egress-sidecar", entry)
        self.assertIn('chmod 0600 "$STATE/ca-key.pem"', entry)
        self.assertIn("security_file_certgen -c -s", entry)
        # Squid runs as PID 1 under the squid user, logging into a FIFO it owns.
        self.assertIn('mkfifo -m 0600 "$STATE/log"', entry)
        self.assertTrue(
            entry.rstrip().endswith(
                'exec su-exec squid squid -N -f /etc/squid/squid.conf >"$STATE/log" 2>&1'
            )
        )

    def test_reload_parses_before_reconfigure(self) -> None:
        # Task 2.2: copy, squid -k parse, then squid -k reconfigure.
        script = self.files["sbx-reload"]
        copy = script.index('cp "$INPUT/squid.conf" "$NEXT"')
        parse = script.index('squid -k parse -f "$NEXT"')
        replace = script.index('mv "$NEXT" "$LIVE"')
        reconfigure = script.index('squid -k reconfigure -f "$LIVE"')
        self.assertLess(copy, parse)
        self.assertLess(parse, replace)
        self.assertLess(replace, reconfigure)
        self.assertIn("exit 65", script)
        dockerfile_text = self.files["Dockerfile"]
        self.assertIn("COPY sbx-reload /usr/local/bin/sbx-reload", dockerfile_text)

    def test_reconfigure_finds_squid_through_its_pid_file(self) -> None:
        from project_sandbox import egress_policy

        conf = squid_config.render(egress_policy.effective(egress_policy.PolicyFile()))
        self.assertIn(f"pid_filename {squid_config.PID_FILE}\n", conf)
        self.assertTrue(squid_config.PID_FILE.startswith(squid_config.STATE_DIR + "/"))

    def test_no_forwarders_in_the_image(self) -> None:
        # Decision 1: nothing forwards inside the sidecar any more.
        self.assertNotIn("sbx-forwarders", self.files)
        for name, text in self.files.items():
            with self.subTest(name=name):
                for needle in ("socat", "sbx-forwarders", "SBX_INTERNAL_SUBNET"):
                    self.assertNotIn(needle, text)

    def test_image_tag_changes_with_any_template(self) -> None:
        tag = sidecar_image.image_tag(self.files)
        for name in self.files:
            with self.subTest(name=name):
                changed = {**self.files, name: self.files[name] + "# changed\n"}
                self.assertNotEqual(sidecar_image.image_tag(changed), tag)
                without = {k: v for k, v in self.files.items() if k != name}
                self.assertNotEqual(sidecar_image.image_tag(without), tag)

    def test_entrypoint_is_valid_posix_sh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("entrypoint.sh", "sni-match", "sbx-reload"):
                path = Path(tmp) / name
                path.write_text(self.files[name])
                result = subprocess.run(
                    ["sh", "-n", str(path)], capture_output=True, text=True, check=False
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_sni_match_helper_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / "sni-match"
            helper.write_text(self.files["sni-match"])
            cases = [
                ("example.com example.com", "OK"),
                ("Example.COM example.com", "OK"),
                ("evil.example example.com", "ERR message=sni-mismatch"),
                ("- example.com", "ERR message=sni-mismatch"),
                ("api.github.com example.com", "ERR message=sni-mismatch"),
                ("example.com.evil example.com", "ERR message=sni-mismatch"),
            ]
            result = subprocess.run(
                ["sh", str(helper)],
                input="".join(line + "\n" for line, _ in cases),
                capture_output=True,
                text=True,
                check=True,
            )
        self.assertEqual(
            result.stdout.splitlines(), [expected for _, expected in cases]
        )

    def test_image_tag_is_content_addressed(self) -> None:
        tag = sidecar_image.image_tag(self.files)
        self.assertRegex(tag, r"^project-sandbox-egress-sidecar:[0-9a-f]{16}$")
        self.assertEqual(tag, sidecar_image.image_tag())
        with patch.object(sidecar_image, "SQUID_PACKAGE", "squid~6.13"):
            self.assertNotEqual(tag, sidecar_image.image_tag())

    def test_proxy_args_are_declared_before_package_downloads(self) -> None:
        # Regression: Podman/Buildah only exports declared proxy build-args.
        text = sidecar_image.render_files()["Dockerfile"]
        first_run = text.index("\nRUN ")
        for name in build_network.ARG_NAMES:
            self.assertLess(text.index(f"\nARG {name}\n"), first_run)
        self.assertNotRegex(text, r"(?i)_proxy=")
        self.assertNotIn("ENV ", text)

    def test_build_certificates_bootstrap_apk_and_change_image_tag(self) -> None:
        fixtures = Path(__file__).parent / "fixtures"
        certificates = ((fixtures / "test-ca.pem").read_bytes(),)
        files = sidecar_image.render_files(build_ca_certificates=certificates)
        text = files["Dockerfile"]
        source = "project-sandbox-build-ca-0.crt"
        self.assertEqual(files[source].encode(), certificates[0])
        self.assertIn(json.dumps([source, "/usr/local/share/ca-certificates/"]), text)
        self.assertLess(text.index("COPY"), text.index("RUN cat"))
        self.assertLess(
            text.index(">> /etc/ssl/certs/ca-certificates.crt"),
            text.index("apk add --no-cache ca-certificates"),
        )
        self.assertLess(
            text.index("update-ca-certificates"),
            text.index(f"apk add --no-cache {sidecar_image.SQUID_PACKAGE}"),
        )
        self.assertNotIn("--no-check-certificate", text)
        self.assertNotIn("ENV ", text)
        self.assertNotRegex(text, r"(?i)_proxy=")
        self.assertLess(text.index("ARG http_proxy"), text.index("apk add"))
        tag = sidecar_image.image_tag(files)
        self.assertNotEqual(tag, sidecar_image.image_tag())
        changed = sidecar_image.render_files(
            build_ca_certificates=(certificates[0] + b"\n",)
        )
        self.assertNotEqual(tag, sidecar_image.image_tag(changed))
        with tempfile.TemporaryDirectory() as tmp:
            context, rendered_tag = sidecar_image.render(
                Path(tmp), build_ca_certificates=certificates
            )
            self.assertEqual(rendered_tag, tag)
            self.assertEqual((context / source).read_bytes(), certificates[0])
            plain, plain_tag = sidecar_image.render(Path(tmp))
            self.assertNotEqual(plain_tag, tag)
            self.assertFalse((plain / source).exists())
            self.assertNotIn(
                "update-ca-certificates", (plain / "Dockerfile").read_text()
            )

    def test_render_writes_context_with_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context, tag = sidecar_image.render(Path(tmp))
            self.assertEqual(context.name, tag.rsplit(":", 1)[1])
            self.assertEqual(
                sorted(p.name for p in context.iterdir()),
                [
                    "Dockerfile",
                    "entrypoint.sh",
                    "errors",
                    "sbx-internal-address",
                    "sbx-reload",
                    "sni-match",
                ],
            )
            self.assertEqual(
                sorted(p.name for p in (context / "errors").iterdir()),
                sorted(c.page for c in squid_config.CAUSES.values() if c.page),
            )
            self.assertEqual((context / "entrypoint.sh").stat().st_mode & 0o777, 0o755)
            self.assertEqual((context / "sni-match").stat().st_mode & 0o777, 0o755)
            self.assertEqual((context / "sbx-reload").stat().st_mode & 0o777, 0o755)
            self.assertEqual(
                (context / "sbx-internal-address").stat().st_mode & 0o777, 0o755
            )
            self.assertEqual((context / "Dockerfile").stat().st_mode & 0o777, 0o644)
            # Idempotent: a second render reuses the same directory and tag.
            self.assertEqual(sidecar_image.render(Path(tmp)), (context, tag))


class SquidConfigRendererTests(TestCase):
    """Sidecar squid.conf (egress-sidecar-macos task 2.1, design.md Decision 2)."""

    @staticmethod
    def _policy(*, copilot: bool = True, allow=("pypi.org", "*.npmjs.org")):
        from project_sandbox import egress_policy

        return egress_policy.effective(
            egress_policy.PolicyFile(allow=allow, copilot=copilot)
        )

    @staticmethod
    def _lines(conf: str) -> list[str]:
        return [line for line in conf.splitlines() if line and not line.startswith("#")]

    def _acl_values(self, conf: str, name: str) -> list[str]:
        for line in self._lines(conf):
            parts = line.split()
            if parts[:2] == ["acl", name]:
                return [p for p in parts[3:] if not p.startswith("-")]
        self.fail(f"acl {name} not rendered")

    # --- pipeline -----------------------------------------------------------

    def test_core_settings(self) -> None:
        conf = squid_config.render(self._policy())
        lines = self._lines(conf)
        self.assertIn(
            "http_port 3128 ssl-bump tls-cert=/run/egress-sidecar/ca.pem "
            "tls-key=/run/egress-sidecar/ca-key.pem generate-host-certificates=on "
            "dynamic_cert_mem_cache_size=4MB",
            lines,
        )
        self.assertIn("cache deny all", lines)
        self.assertIn("access_log stdio:/dev/stdout sbx", lines)
        self.assertIn("cache_log stdio:/dev/stderr", lines)
        self.assertIn("shutdown_lifetime 1 seconds", lines)
        self.assertIn("forwarded_for delete", lines)
        self.assertIn("host_verify_strict on", lines)
        self.assertIn("error_directory /etc/squid/sbx-errors", lines)
        logformat = next(line for line in lines if line.startswith("logformat sbx "))
        self.assertTrue(logformat.startswith("logformat sbx SBX "))
        # client= is the published port's forwarder; kept for debugging only.
        self.assertTrue(logformat.endswith(" peer=%<a client=%>a"), logformat)
        self.assertNotIn("\n\n\n", conf)

    def test_connect_denials_are_resets_before_any_allow_or_bump(self) -> None:
        conf = squid_config.render(self._policy())
        lines = self._lines(conf)
        self.assertIn("deny_info TCP_RESET reset", lines)
        connect_denies = [
            i
            for i, line in enumerate(lines)
            if line.startswith("http_access deny CONNECT")
        ]
        self.assertEqual(len(connect_denies), 4)  # port, ip literal, host, private
        for index in connect_denies:
            self.assertTrue(lines[index].endswith(" reset"), lines[index])
        allow_connect = lines.index("http_access allow CONNECT")
        first_bump = next(
            i for i, line in enumerate(lines) if line.startswith("ssl_bump")
        )
        self.assertLess(max(connect_denies), allow_connect)
        self.assertLess(allow_connect, first_bump)
        first_allow = next(
            i for i, line in enumerate(lines) if line.startswith("http_access allow")
        )
        self.assertEqual(first_allow, allow_connect)

    def test_cause_notes_cover_connect_denials_in_order(self) -> None:
        lines = self._lines(squid_config.render(self._policy()))
        notes = [
            line.split()[2] for line in lines if line.startswith("note sbx_cause ")
        ]
        self.assertEqual(
            notes,
            [
                "port-not-allowed",
                "port-not-allowed",
                "ip-literal",
                "private-address",
                "host-not-allowed",
                "plain-http-intercepted",
                "path-not-allowed",
                "path-not-allowed",
            ],
        )
        self.assertLess(
            lines.index("note sbx_cause ip-literal ip_literal"),
            lines.index("note sbx_cause host-not-allowed !allowed"),
        )

    def test_cause_notes_in_parent_mode_skip_private_address(self) -> None:
        lines = self._lines(
            squid_config.render(
                self._policy(copilot=False),
                upstream=squid_config.Upstream("proxy.corp.example", 3128),
            )
        )
        notes = [
            line.split()[2] for line in lines if line.startswith("note sbx_cause ")
        ]
        self.assertEqual(
            notes,
            ["port-not-allowed", "port-not-allowed", "ip-literal", "host-not-allowed"],
        )

    def test_listen_port_is_the_container_port(self) -> None:
        # The host port ([egress.sidecar] port) is a publish mapping only.
        from project_sandbox import egress_policy

        policy = egress_policy.effective(egress_policy.PolicyFile(port=3140))
        conf = squid_config.render(policy)
        self.assertIn("http_port 3128 ssl-bump ", conf)
        self.assertNotIn("3140", conf)

    def test_sni_and_raw_tunnel_handling(self) -> None:
        conf = squid_config.render(self._policy())
        lines = self._lines(conf)
        self.assertIn(
            "external_acl_type sni_match ttl=60 negative_ttl=60 children-max=4 "
            "%ssl::>sni %>rd /usr/local/libexec/sni-match",
            lines,
        )
        bump = [line for line in lines if line.startswith("ssl_bump")]
        self.assertEqual(
            bump,
            [
                "ssl_bump peek step1",
                "ssl_bump terminate step2 !sni_ok",
                "ssl_bump bump step2 intercepted",
                "ssl_bump splice step2 allowed",
                "ssl_bump terminate all",
            ],
        )
        self.assertIn("on_unsupported_protocol respond all", lines)
        self.assertNotIn("on_unsupported_protocol tunnel", conf)

    def test_labelled_pages_for_plain_http_and_intercepted_denials(self) -> None:
        lines = self._lines(squid_config.render(self._policy()))
        for cause in squid_config.CAUSES.values():
            if cause.page:
                marker = "page_" + cause.label.replace("-", "_")
                self.assertIn(f"deny_info {cause.page} {marker}", lines)
        self.assertEqual(lines[-1], "on_unsupported_protocol respond all")
        self.assertIn("http_access deny all page_host_not_allowed", lines)

    def test_error_pages_are_labelled(self) -> None:
        pages = squid_config.error_pages()
        self.assertEqual(
            set(pages), {c.page for c in squid_config.CAUSES.values() if c.page}
        )
        for page, html in pages.items():
            cause = next(c for c in squid_config.CAUSES.values() if c.page == page)
            self.assertIn(f"project-sandbox-egress: {cause.label}", html)
            self.assertIn("%U", html)

    def test_direct_mode_denies_private_destinations(self) -> None:
        conf = squid_config.render(self._policy())
        values = self._acl_values(conf, "private_dst")
        for cidr in (
            "169.254.0.0/16",
            "127.0.0.0/8",
            "10.0.0.0/8",
            "fc00::/7",
            "::1/128",
        ):
            self.assertIn(cidr, values)
        # Squid stores IPv4 as mapped addresses: ::ffff:0:0/96 would be all of IPv4.
        self.assertNotIn("::ffff:0:0/96", values)
        self.assertIn("http_access deny CONNECT private_dst reset", conf)
        self.assertNotIn("never_direct", conf)

    def test_apple_localhost_alias_range_is_a_private_destination(self) -> None:
        """isolated-sidecar-network task 3.2 (spike S5, design.md D8)."""
        import ipaddress

        values = self._acl_values(squid_config.render(self._policy()), "private_dst")
        self.assertIn("203.0.113.0/24", values)
        # An allowlisted name that resolves to the alias is caught by the
        # resolved-address check, like any other private destination.
        alias = ipaddress.ip_address("203.0.113.113")
        self.assertTrue(
            any(
                alias in ipaddress.ip_network(cidr)
                for cidr in squid_config.PRIVATE_DESTINATIONS
            )
        )
        # Ordinary public documentation-adjacent space stays allowed.
        self.assertFalse(
            any(
                ipaddress.ip_address("203.0.114.1") in ipaddress.ip_network(cidr)
                for cidr in squid_config.PRIVATE_DESTINATIONS
            )
        )

    def test_allowlist_rendering(self) -> None:
        from project_sandbox import egress_policy

        conf = squid_config.render(self._policy(copilot=False))
        providers = list(egress_policy.PROVIDER_HOSTS)
        self.assertEqual(
            sorted(self._acl_values(conf, "allowed")),
            sorted([".npmjs.org", "pypi.org", *providers]),
        )
        self.assertNotIn("acl intercepted ", conf)
        self.assertNotIn("ssl_bump bump", conf)
        empty = squid_config.render(egress_policy.EffectivePolicy(None, (), False, ()))
        self.assertEqual(self._acl_values(empty, "allowed"), ["invalid"])

    def test_rejects_injection(self) -> None:
        from project_sandbox import egress_policy

        bad_host = egress_policy.EffectivePolicy(
            policy_path=None,
            allow=("pypi.org\nhttp_access allow all",),
            copilot=False,
            bump_set=(),
        )
        with self.assertRaises(ValueError):
            squid_config.render(bad_host)
        for host in ("evil host", "*.*.example.com", "-bad.example", "a..b"):
            with self.subTest(host=host):
                policy = egress_policy.EffectivePolicy(None, (host,), False, ())
                with self.assertRaises(ValueError):
                    squid_config.render(policy)
        with self.assertRaises(ValueError):
            squid_config.render(self._policy(), api_paths=("^/ok", "x y"))
        with self.assertRaises(ValueError):
            squid_config.render(self._policy(), api_paths=())
        with self.assertRaises(ValueError):
            squid_config.render(
                self._policy(), upstream=squid_config.Upstream("proxy;evil", 3128)
            )

    # --- Copilot profile -------------------------------------------------

    def test_copilot_paths_evaluated_against_urls(self) -> None:
        import re

        conf = squid_config.render(self._policy())
        self.assertEqual(self._acl_values(conf, "intercepted"), ["api.github.com"])
        patterns = self._acl_values(conf, "intercepted_paths")

        def allowed(path: str) -> bool:  # Squid urlpath_regex -i semantics
            return any(re.search(p, path, re.IGNORECASE) for p in patterns)

        for path in (
            "/copilot_internal/user",
            "/copilot_internal/v2/token",
            "/copilot_internal/managed_settings",
        ):
            self.assertTrue(allowed(path), path)
        for path in (
            "/user",
            "/repos/o/r",
            "/repos/o/r/contents/x",
            "/gists",
            "/graphql",
            "/user/repos",
            "/repos/github/copilot-cli/releases",
            "/x/copilot_internal/",
        ):
            self.assertFalse(allowed(path), path)

    def test_ambiguous_copilot_paths_are_denied_before_the_path_allow(self) -> None:
        """Regression: Squid matches urlpath_regex on the percent-decoded path
        but forwards the raw one, so ``/copilot_internal/../repos/secret`` (and
        encoded spellings) passed ``^/copilot_internal/`` and the origin
        served ``/repos/secret``."""
        import re
        from urllib.parse import unquote

        conf = squid_config.render(self._policy())
        allow = self._acl_values(conf, "intercepted_paths")
        ambiguous = self._acl_values(conf, "ambiguous_paths")

        def permitted(raw: str) -> bool:  # rfc1738_unescape, then -i regex
            path = unquote(raw)
            hit = lambda ps: any(re.search(p, path, re.IGNORECASE) for p in ps)
            return hit(allow) and not hit(ambiguous)

        for raw in (
            "/copilot_internal/../repos/secret",
            "/copilot_internal/%2e%2e/repos/secret",
            "/copilot_internal/%2E%2E/repos/secret",
            "/copilot_internal%2f..%2frepos/secret",
            "/copilot_internal/./../repos/secret",
            "/copilot_internal/..",
            "/copilot_internal/..;/repos/secret",
            "/copilot_internal/..\\repos/secret",
            "/copilot_internal/%5c..%5crepos/secret",
            "/copilot_internal/%252e%252e/repos/secret",
            "/copilot_internal/%3f/../../repos/secret",
            "/copilot_internal/x?/../../repos/secret",
        ):
            self.assertFalse(permitted(raw), raw)
        for raw in (
            "/copilot_internal/user",
            "/copilot_internal/v2/token",
            "/copilot_internal/managed_settings",
            "/copilot_internal/user?token=abc&x=1.2",
            "/copilot_internal/.well-known/x",
            "/copilot_internal/a..b",
        ):
            self.assertTrue(permitted(raw), raw)
        lines = self._lines(conf)
        self.assertLess(
            lines.index(
                "http_access deny intercepted ambiguous_paths page_path_not_allowed"
            ),
            lines.index("http_access allow intercepted intercepted_paths"),
        )

    def test_no_other_github_repository_host_in_any_acl(self) -> None:
        from project_sandbox import egress_policy

        conf = squid_config.render(self._policy())
        acl_lines = [line for line in self._lines(conf) if line.startswith("acl ")]
        tokens = {token for line in acl_lines for token in line.split()[3:]}
        for host in egress_policy.GITHUB_REPOSITORY_HOSTS:
            if host == "api.github.com":
                continue
            self.assertNotIn(host, tokens)
            self.assertNotIn("." + host, tokens)
        self.assertFalse([t for t in tokens if "telemetry" in t])

    # --- parent proxy -------------------------------------------------------

    def test_http_parent(self) -> None:
        conf = squid_config.render(
            self._policy(), upstream=squid_config.Upstream("proxy.corp.example", 3128)
        )
        lines = self._lines(conf)
        self.assertIn(
            "cache_peer proxy.corp.example parent 3128 0 no-query no-digest default name=upstream",
            lines,
        )
        self.assertIn("never_direct allow all", lines)
        self.assertNotIn("private_dst", conf)  # hostname-only policy in parent mode
        self.assertNotIn(" dst ", conf)
        self.assertNotIn("tls_outgoing_options", conf)
        # IP-literal and port denials stay.
        self.assertIn("http_access deny CONNECT ip_literal reset", lines)
        self.assertIn("http_access deny CONNECT !tls_port reset", lines)

    def test_https_parent_with_ca(self) -> None:
        conf = squid_config.render(
            self._policy(),
            upstream=squid_config.Upstream(
                "proxy.corp.example", 8443, tls=True, ca_file=True
            ),
        )
        lines = self._lines(conf)
        self.assertIn(
            "cache_peer proxy.corp.example parent 8443 0 no-query no-digest default "
            "name=upstream tls tls-cafile=/run/egress-sidecar/upstream-ca.pem",
            lines,
        )
        self.assertIn(
            "tls_outgoing_options cafile=/run/egress-sidecar/upstream-ca.pem", lines
        )

    def test_https_parent_without_ca_uses_system_trust(self) -> None:
        conf = squid_config.render(
            self._policy(),
            upstream=squid_config.Upstream("proxy.corp.example", 443, tls=True),
        )
        self.assertIn("name=upstream tls\n", conf)
        self.assertNotIn("tls-cafile", conf)
        self.assertNotIn("tls_outgoing_options", conf)

    def test_upstream_for_endpoints(self) -> None:
        from project_sandbox import proxy_endpoint

        remote = proxy_endpoint.parse(
            "https://proxy.corp.example:8443", option="--internet-proxy"
        )
        local = proxy_endpoint.parse(
            "http://127.0.0.1:18080", option="--internet-proxy"
        )
        self.assertEqual(
            squid_config.upstream_for(remote, ca_file=True),
            squid_config.Upstream("proxy.corp.example", 8443, tls=True, ca_file=True),
        )
        self.assertEqual(
            squid_config.upstream_for(local),
            squid_config.Upstream("host.docker.internal", 18080),
        )
        # Apple relay: plain HTTP to the host relay, which re-originates TLS.
        relay = squid_config.upstream_for(remote, relay_port=41234)
        self.assertEqual(relay, squid_config.Upstream("host.docker.internal", 41234))
        conf = squid_config.render(self._policy(), upstream=relay)
        self.assertIn(
            "cache_peer host.docker.internal parent 41234 0 no-query no-digest default name=upstream\n",
            conf,
        )


class InternalBindingTests(TestCase):
    """isolated-sidecar-network task 3.1: Squid listens only on the internal network."""

    HELPER = (
        Path(__file__).resolve().parents[1]
        / "src/project_sandbox/templates/egress-sidecar/sbx-internal-address"
    )

    def _effective(self):
        from project_sandbox import egress_policy

        return egress_policy.effective(egress_policy.PolicyFile())

    def test_internal_only_binds_the_placeholder_and_the_default_is_unchanged(
        self,
    ) -> None:
        plain = squid_config.render(self._effective())
        bound = squid_config.render(self._effective(), internal_only=True)
        self.assertIn("\nhttp_port 3128 ssl-bump", "\n" + plain)
        self.assertNotIn(squid_config.BIND_PLACEHOLDER, plain)
        self.assertIn(
            f"\nhttp_port {squid_config.BIND_PLACEHOLDER}:3128 ssl-bump", "\n" + bound
        )
        self.assertEqual(plain, bound.replace(f"{squid_config.BIND_PLACEHOLDER}:", ""))

    def test_entrypoint_and_reload_substitute_the_address(self) -> None:
        files = sidecar_image.render_files()
        for name, target in (
            ("entrypoint.sh", "/etc/squid/squid.conf"),
            ("sbx-reload", '"$NEXT"'),
        ):
            with self.subTest(name):
                text = files[name]
                self.assertIn(
                    f"grep -q '{squid_config.BIND_PLACEHOLDER}' {target}", text
                )
                self.assertIn("/usr/local/libexec/sbx-internal-address", text)
                self.assertIn(
                    f's/{squid_config.BIND_PLACEHOLDER}/$address/g" {target}', text
                )
        # Reload substitutes before the parse check, so a bad address never goes live.
        reload = files["sbx-reload"]
        self.assertLess(reload.index("sbx-internal-address"), reload.index("-k parse"))
        self.assertIn(
            "COPY sbx-internal-address /usr/local/libexec/sbx-internal-address",
            files["Dockerfile"],
        )

    def _run_helper(self, routes: str, addrs: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as tmp:
            fake_ip = Path(tmp) / "ip"
            fake_ip.write_text(
                "#!/bin/sh\n"
                'case "$*" in\n'
                f"  *route*) cat <<'OUT'\n{routes}OUT\n;;\n"
                f"  *addr*) cat <<'OUT'\n{addrs}OUT\n;;\n"
                "esac\n"
            )
            fake_ip.chmod(0o755)
            return subprocess.run(
                ["/bin/sh", str(self.HELPER)],
                capture_output=True,
                text=True,
                check=False,
                env={"PATH": f"{tmp}:/usr/bin:/bin"},
            )

    def test_helper_prints_the_address_without_the_default_route(self) -> None:
        routes = "default via 172.18.0.1 dev eth0 \n"
        for name in ("eth1", "eth1@if12"):
            with self.subTest(name):
                addrs = (
                    "2: eth0    inet 172.18.0.2/16 brd 172.18.255.255 scope global eth0\n"
                    f"3: {name}    inet 172.19.0.2/16 brd 172.19.255.255 scope global eth1\n"
                )
                result = self._run_helper(routes, addrs)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "172.19.0.2\n")

    def test_helper_fails_without_an_internal_interface(self) -> None:
        result = self._run_helper(
            "default via 172.18.0.1 dev eth0 \n",
            "2: eth0    inet 172.18.0.2/16 brd 172.18.255.255 scope global eth0\n",
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("eth0", result.stderr)


class ServiceRouteTests(TestCase):
    """isolated-sidecar-network task 4.1: service routes in squid.conf (design.md D3).

    The layout matches spike S1, checked again against the sidecar image's
    Squid: an absolute URI on a service port is rewritten to the service,
    CONNECT there gets 405, and forward-proxy requests for the host are denied.
    """

    HOST = "192.168.5.2"

    def setUp(self) -> None:
        from project_sandbox import egress_policy

        self.egress_policy = egress_policy
        self.services = (
            egress_policy.Service("agent-proxy", 4000),
            egress_policy.Service("ollama", 11434),
        )

    def _render(self, *, upstream=None, services=None) -> str:
        return squid_config.render(
            self.egress_policy.effective(
                self.egress_policy.PolicyFile(allow=("pypi.org",))
            ),
            upstream=upstream,
            internal_only=True,
            services=self.services if services is None else services,
            service_host=self.HOST,
        )

    @staticmethod
    def _lines(conf: str) -> list[str]:
        return [line for line in conf.splitlines() if line and not line.startswith("#")]

    def test_no_services_leaves_the_config_unchanged(self) -> None:
        bound = squid_config.render(
            self.egress_policy.effective(
                self.egress_policy.PolicyFile(allow=("pypi.org",))
            ),
            internal_only=True,
        )
        self.assertEqual(self._render(services=()), bound)
        for token in ("name=fwd", "accel", "originserver", "myportname", "svc_"):
            self.assertNotIn(token, bound)

    def test_accel_ports_bound_to_the_internal_address(self) -> None:
        lines = self._lines(self._render())
        bind = squid_config.BIND_PLACEHOLDER
        self.assertTrue(lines[0].startswith(f"http_port {bind}:3128 ssl-bump "))
        self.assertTrue(lines[0].endswith(" name=fwd"), lines[0])
        self.assertEqual(
            lines[1:3],
            [
                (
                    f"http_port {bind}:4000 accel defaultsite=agent-proxy.service "
                    "no-vhost name=svc_agent-proxy"
                ),
                (
                    f"http_port {bind}:11434 accel defaultsite=ollama.service "
                    "no-vhost name=svc_ollama"
                ),
            ],
        )
        self.assertEqual(
            [line for line in lines if line.startswith("http_port")], lines[:3]
        )

    def test_originserver_peers_at_the_host_address(self) -> None:
        lines = self._lines(self._render())
        for name, port in (("agent-proxy", 4000), ("ollama", 11434)):
            self.assertIn(
                f"cache_peer {self.HOST} parent {port} 0 no-query no-digest "
                f"originserver forceddomain=localhost name=svc_{name}",
                lines,
            )
            allow = lines.index(f"cache_peer_access svc_{name} allow svc_port_{name}")
            self.assertEqual(lines[allow + 1], f"cache_peer_access svc_{name} deny all")
            self.assertIn(f"acl svc_port_{name} myportname svc_{name}", lines)
            self.assertIn(f"acl svc_site_{name} dstdomain {name}.service", lines)
        self.assertIn("acl fwd_port myportname fwd", lines)
        self.assertIn("acl svc_ports myportname svc_agent-proxy svc_ollama", lines)

    def test_service_ports_never_go_direct_and_are_not_cached(self) -> None:
        lines = self._lines(self._render())
        self.assertIn("never_direct allow svc_ports", lines)
        self.assertIn("cache deny svc_ports", lines)
        self.assertIn("cache deny all", lines)
        self.assertNotIn("never_direct allow all", lines)  # direct mode

    def test_service_rules_precede_and_fence_off_the_forward_rules(self) -> None:
        lines = self._lines(self._render())
        access = [line for line in lines if line.startswith("http_access ")]
        self.assertEqual(
            access[:5],
            [
                "http_access deny svc_ports CONNECT reset",
                "http_access allow svc_port_agent-proxy svc_site_agent-proxy",
                "http_access allow svc_port_ollama svc_site_ollama",
                "http_access deny svc_ports reset",
                "http_access deny !fwd_port reset",
            ],
        )
        # Every later rule only sees the forward port, so private_dst still
        # denies the Mac's address (192.168.0.0/16) there.
        self.assertIn("http_access deny CONNECT private_dst reset", access[5:])
        self.assertIn("http_access deny private_dst page_private_address", access[5:])
        self.assertFalse([a for a in access[5:] if "svc_" in a])

    def test_service_denials_are_labelled(self) -> None:
        lines = self._lines(self._render())
        notes = [line for line in lines if line.startswith("note sbx_cause ")]
        self.assertEqual(notes[0], "note sbx_cause service-route svc_ports")
        self.assertIn("service-route", squid_config.CAUSES)
        self.assertIsNone(squid_config.CAUSES["service-route"].page)

    def test_upstream_never_receives_service_traffic(self) -> None:
        lines = self._lines(
            self._render(upstream=squid_config.Upstream("proxy.corp.example", 8080))
        )
        self.assertIn("never_direct allow all", lines)
        deny = lines.index("cache_peer_access upstream deny svc_ports")
        self.assertEqual(lines[deny + 1], "cache_peer_access upstream allow all")
        # The upstream peer and every ACL are declared before their access lines.
        self.assertLess(
            next(i for i, line in enumerate(lines) if "name=upstream" in line), deny
        )
        self.assertLess(
            lines.index("acl svc_ports myportname svc_agent-proxy svc_ollama"), deny
        )

    def test_direct_mode_has_no_upstream_access_lines(self) -> None:
        self.assertNotIn("cache_peer_access upstream", self._render())

    def test_refuses_unsafe_service_input(self) -> None:
        effective = self.egress_policy.effective(self.egress_policy.PolicyFile())
        Service = self.egress_policy.Service
        cases: dict[str, tuple[bool, str | None]] = {
            "not internal": (False, self.HOST),
            "no host": (True, None),
            "host name": (True, "host.lima.internal"),
            "host injection": (True, "192.168.5.2\nhttp_access allow all"),
        }
        for label, (internal_only, service_host) in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                squid_config.render(
                    effective,
                    services=(Service("ollama", 11434),),
                    internal_only=internal_only,
                    service_host=service_host,
                )
        for label, services in {
            "bad name": (Service("Ollama x", 11434),),
            "reserved port": (Service("ollama", 3128),),
            "low port": (Service("ollama", 80),),
            "duplicate port": (Service("a", 4000), Service("b", 4000)),
        }.items():
            with self.subTest(label), self.assertRaises(ValueError):
                squid_config.render(
                    effective,
                    services=services,
                    internal_only=True,
                    service_host=self.HOST,
                )
