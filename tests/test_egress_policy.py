import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from project_sandbox import egress_policy, firewall
from project_sandbox.egress_policy import EgressPolicyError


def _write(directory: Path, text: str, name: str = "egress.toml") -> Path:
    path = directory / name
    path.write_text(text)
    return path


class LoaderTests(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_loads_full_policy(self) -> None:
        path = _write(
            self.tmp,
            """
[egress]
allow = ["pypi.org", "files.pythonhosted.org", "*.npmjs.org", "PyPI.org."]

[egress.copilot]
enabled = true
""",
        )
        policy = egress_policy.load(path)
        self.assertEqual(policy.path, path)
        self.assertEqual(
            policy.allow,
            ("pypi.org", "files.pythonhosted.org", "*.npmjs.org", "pypi.org"),
        )
        self.assertTrue(policy.copilot)

    def test_empty_file_is_empty_policy(self) -> None:
        policy = egress_policy.load(_write(self.tmp, ""))
        self.assertEqual(policy.allow, ())
        self.assertFalse(policy.copilot)

    def test_unknown_keys_are_rejected_and_named(self) -> None:
        cases = {
            "[other]\n": "'other'",
            "[egress]\ndeny = []\n": "egress.'deny'",
            "[egress.copilot]\nhosts = []\n": "egress.copilot.'hosts'",
        }
        for text, needle in cases.items():
            with self.subTest(text=text):
                with self.assertRaises(EgressPolicyError) as ctx:
                    egress_policy.load(_write(self.tmp, text))
                self.assertIn(needle, str(ctx.exception))

    def test_wrong_types_are_rejected(self) -> None:
        for text in (
            '[egress]\nallow = "pypi.org"\n',
            "[egress]\nallow = [1]\n",
            "egress = 1\n",
            "[egress.copilot]\nenabled = 1\n",
        ):
            with self.subTest(text=text), self.assertRaises(EgressPolicyError):
                egress_policy.load(_write(self.tmp, text))

    def test_github_table_is_rejected_with_roadmap_pointer(self) -> None:
        for text in (
            '[egress.github]\nread = ["acme/*"]\n',
            "[egress.github]\n",
            "[egress]\ngithub = {}\n",
        ):
            with self.subTest(text=text):
                with self.assertRaises(EgressPolicyError) as ctx:
                    egress_policy.load(_write(self.tmp, text))
                self.assertIn("[egress.github]", str(ctx.exception))
                self.assertIn("roadmap", str(ctx.exception))

    def test_repository_hosts_cannot_be_allowlisted(self) -> None:
        for host in (
            "github.com",
            "api.github.com",
            "raw.githubusercontent.com",
            "*.github.com",
            "*.githubusercontent.com",
            "GitHub.com.",
        ):
            with self.subTest(host=host):
                with self.assertRaises(EgressPolicyError) as ctx:
                    egress_policy.load(
                        _write(self.tmp, f'[egress]\nallow = ["{host}"]\n')
                    )
                self.assertIn("GitHub repository access", str(ctx.exception))

    def test_invalid_toml_and_missing_file(self) -> None:
        with self.assertRaises(EgressPolicyError):
            egress_policy.load(_write(self.tmp, "[egress\n"))
        with self.assertRaises(EgressPolicyError):
            egress_policy.load(self.tmp / "missing.toml")


class ValidationTests(TestCase):
    def test_accepts_hosts_and_wildcards(self) -> None:
        for value, expected in (
            ("pypi.org", "pypi.org"),
            ("Registry.NPMJS.org.", "registry.npmjs.org"),
            ("*.npmjs.org", "*.npmjs.org"),
            ("localhost", "localhost"),
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    egress_policy.normalize_host_pattern(value, source="test"), expected
                )

    def test_rejects_unsafe_hosts(self) -> None:
        for value in (
            "",
            "*",
            "*.com",
            "a.*.example.com",
            "**.example.com",
            ".example.com",
            "example.com:443",
            "https://example.com",
            "exa mple.com",
            "example.com\nhttp_access allow all",
            "$(id).example.com",
            "-bad.example.com",
            "1.2.3.4",
            "169.254.169.254",
            "::1",
            "[::1]",
            "10.0.0.1.",
            None,
        ):
            with self.subTest(value=value), self.assertRaises(EgressPolicyError):
                egress_policy.normalize_host_pattern(value, source="test")


class EffectivePolicyTests(TestCase):
    def test_provider_hosts_are_always_allowed(self) -> None:
        # Task 1.2: a policy allowing only pypi.org yields every provider host.
        effective = egress_policy.effective(
            egress_policy.PolicyFile(allow=("pypi.org",))
        )
        self.assertIn("pypi.org", effective.allow)
        for host in egress_policy.PROVIDER_HOSTS:
            self.assertIn(host, effective.allow)
        self.assertNotIn("github.com", effective.allow)
        self.assertEqual(effective.bump_set, ())
        self.assertFalse(effective.interception)
        self.assertEqual(effective.name_constraints, ())

    def test_dedupes_and_drops_hosts_covered_by_wildcards(self) -> None:
        policy = egress_policy.PolicyFile(
            allow=(
                "registry.npmjs.org",
                "npmjs.org",
                "*.npmjs.org",
                "*.a.npmjs.org",
                "pypi.org",
                "notnpmjs.org",
            )
        )
        allow = egress_policy.effective(policy).allow
        user = tuple(h for h in allow if h not in egress_policy.PROVIDER_HOSTS)
        self.assertEqual(user, ("notnpmjs.org", "*.npmjs.org", "pypi.org"))

    def test_copilot_profile_without_repositories(self) -> None:
        effective = egress_policy.effective(egress_policy.PolicyFile(copilot=True))
        self.assertTrue(effective.copilot)
        for host in egress_policy.COPILOT_HOSTS:
            self.assertIn(host, effective.allow)
        # api.github.com is admitted only for the intercepted token exchange; no
        # other repository host is admitted.
        self.assertIn("api.github.com", effective.allow)
        for host in egress_policy.GITHUB_REPOSITORY_HOSTS:
            if host != "api.github.com":
                self.assertNotIn(host, effective.allow)
        self.assertEqual(effective.bump_set, ("api.github.com",))
        self.assertEqual(effective.name_constraints, ("github.com",))

    def test_copilot_api_deny_mode_intercepts_nothing(self) -> None:
        with patch.object(egress_policy, "COPILOT_API_ACCESS", "deny"):
            effective = egress_policy.effective(egress_policy.PolicyFile(copilot=True))
        self.assertNotIn("api.github.com", effective.allow)
        self.assertEqual(effective.bump_set, ())
        self.assertFalse(effective.interception)

    def test_wildcard_cannot_admit_repository_hosts(self) -> None:
        # Task 1.2: *.githubusercontent.com is still rejected.
        with tempfile.TemporaryDirectory() as tmp:
            for host in (
                "*.githubusercontent.com",
                "codeload.github.com",
                "*.github.com",
            ):
                with self.subTest(host=host):
                    path = _write(Path(tmp), f'[egress]\nallow = ["{host}"]\n')
                    with self.assertRaisesRegex(EgressPolicyError, "GitHub repository"):
                        egress_policy.load(path)

    def test_blocked_repository_hosts(self) -> None:
        self.assertEqual(egress_policy.blocked_repository_hosts("pypi.org"), ())
        self.assertEqual(
            egress_policy.blocked_repository_hosts("github.com"), ("github.com",)
        )
        covered = egress_policy.blocked_repository_hosts("*.github.com")
        self.assertIn("github.com", covered)  # Squid's .github.com matches the apex
        self.assertIn("codeload.github.com", covered)
        # Copilot's own hosts under githubusercontent.com are not repository hosts.
        self.assertEqual(
            egress_policy.blocked_repository_hosts(
                "copilot-proxy.githubusercontent.com"
            ),
            (),
        )

    def test_host_lists_match_firewall_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = firewall.render(
                Path(tmp), extra_domains=[], allow_github=True
            ).read_text()
        domains = set(re.findall(r'^  "([^"]+)"$', script, re.MULTILINE))
        for host in egress_policy.PROVIDER_HOSTS:
            self.assertIn(host, domains)
        # The sidecar profile is the captured CLI set (task 1.3), a subset of
        # firewall mode's broader list. Neither admits Copilot CLI telemetry.
        for host in egress_policy.COPILOT_HOSTS:
            self.assertIn(host, domains)
        self.assertFalse([d for d in domains if d.startswith("telemetry.")])

    def test_copilot_profile_blocks_telemetry(self) -> None:
        effective = egress_policy.effective(egress_policy.PolicyFile(copilot=True))
        self.assertFalse([h for h in effective.allow if "telemetry" in h])


class PolicyLocationTests(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name).resolve()
        self.root = root
        self.project = root / "project"
        self.worktree = root / "worktrees" / "feature"
        self.git_dir = root / "main" / ".git"
        self.mount = root / "mounted-cache"
        self.config = root / "config"
        for directory in (
            self.project,
            self.worktree,
            self.git_dir,
            self.mount,
            self.config,
        ):
            directory.mkdir(parents=True)
        self.protected = [self.project, self.worktree, self.git_dir, self.mount]

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _resolve(self, path):
        egress_policy.check_policy_location(Path(path), self.protected)
        return Path(path)

    def test_accepts_policy_outside_protected_paths(self) -> None:
        path = _write(self.config, "")
        self.assertEqual(self._resolve(path), path)

    def test_rejects_mount_containing_the_policy(self) -> None:
        # Task 6.5: mounting ~/.config exposes egress.toml to the agent.
        policy_dir = self.config / "project-sandbox"
        policy_dir.mkdir()
        path = _write(policy_dir, "")
        with self.assertRaisesRegex(EgressPolicyError, "mounted into the sandbox"):
            egress_policy.check_policy_location(path, [self.config])

    def test_rejects_each_protected_location(self) -> None:
        for directory in self.protected:
            with self.subTest(directory=directory):
                path = _write(directory, "")
                with self.assertRaises(EgressPolicyError) as ctx:
                    self._resolve(path)
                self.assertIn("could modify", str(ctx.exception))

    def test_rejects_nested_and_relative_paths_inside_project(self) -> None:
        nested = self.project / "sub" / "dir"
        nested.mkdir(parents=True)
        _write(nested, "")
        cwd = os.getcwd()
        try:
            os.chdir(self.project)
            with self.assertRaises(EgressPolicyError):
                self._resolve("./sub/dir/egress.toml")
            with self.assertRaises(EgressPolicyError):
                self._resolve(Path("sub") / ".." / "sub" / "dir" / "egress.toml")
        finally:
            os.chdir(cwd)

    def test_rejects_symlink_into_project(self) -> None:
        target = _write(self.project, "")
        link = self.config / "egress.toml"
        link.symlink_to(target)
        with self.assertRaises(EgressPolicyError):
            self._resolve(link)

    def test_rejects_symlinked_directory_into_project(self) -> None:
        _write(self.project, "")
        (self.config / "ps").symlink_to(self.project, target_is_directory=True)
        with self.assertRaises(EgressPolicyError):
            self._resolve(self.config / "ps" / "egress.toml")

    def test_rejects_symlink_inside_project_pointing_outside(self) -> None:
        # The link itself is agent-writable and could be re-pointed later.
        target = _write(self.config, "")
        (self.project / "egress.toml").symlink_to(target)
        with self.assertRaises(EgressPolicyError):
            self._resolve(self.project / "egress.toml")

    def test_rejects_chain_through_project(self) -> None:
        target = _write(self.config, "", name="real.toml")
        hop = self.project / "hop.toml"
        hop.symlink_to(target)
        link = self.config / "egress.toml"
        link.symlink_to(hop)
        with self.assertRaises(EgressPolicyError):
            self._resolve(link)

    def test_rejects_protected_path_given_via_symlinked_root(self) -> None:
        # Protected roots are compared after resolve(), too.
        alias = self.root / "alias"
        alias.symlink_to(self.project, target_is_directory=True)
        path = _write(self.project, "")
        with self.assertRaises(EgressPolicyError):
            egress_policy.check_policy_location(path, [alias])

    def test_default_path_uses_xdg_config_home(self) -> None:
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config)}):
            self.assertEqual(
                egress_policy.default_policy_path(),
                self.config / "project-sandbox" / "egress.toml",
            )
            self.assertIsNone(egress_policy.load_global().path)
            policy_dir = self.config / "project-sandbox"
            policy_dir.mkdir()
            path = _write(policy_dir, "[egress]\nallow = ['pypi.org']\n")
            self.assertEqual(egress_policy.load_global().path, path)
            self.assertEqual(egress_policy.load_global().allow, ("pypi.org",))

    def test_relative_xdg_config_home_is_ignored(self) -> None:
        with (
            patch.dict(os.environ, {"XDG_CONFIG_HOME": "relative"}),
            patch.object(Path, "home", return_value=self.root),
        ):
            self.assertEqual(
                egress_policy.default_policy_path(),
                self.root / ".config" / "project-sandbox" / "egress.toml",
            )

    def test_default_path_inside_project_is_rejected(self) -> None:
        policy_dir = self.project / "project-sandbox"
        policy_dir.mkdir()
        _write(policy_dir, "")
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.project)}):
            with self.assertRaises(EgressPolicyError):
                self._resolve(egress_policy.default_policy_path())


class NewTablesTests(TestCase):
    """Task 1.1: [egress.upstream], [egress.ca] and [egress.sidecar]."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_valid_tables(self) -> None:
        path = _write(
            self.tmp,
            """
[egress]
allow = ["pypi.org"]
[egress.upstream]
url = "https://proxy.corp.example:8443"
ca_cert = ["corp.pem", "/abs/other.pem"]
[egress.ca]
lifetime_days = 30
[egress.sidecar]
memory = "512m"
cpus = 2
port = 3140
""",
        )
        policy = egress_policy.load(path)
        self.assertEqual(policy.upstream_url, "https://proxy.corp.example:8443")
        upstream = policy.upstream
        assert upstream is not None
        self.assertEqual(upstream.host, "proxy.corp.example")
        self.assertEqual(upstream.port, 8443)
        self.assertEqual(
            policy.upstream_ca_cert, (self.tmp / "corp.pem", Path("/abs/other.pem"))
        )
        self.assertEqual(policy.ca_lifetime_days, 30)
        self.assertEqual(policy.memory, "512M")
        self.assertEqual(policy.cpus, 2)
        self.assertEqual(policy.port, 3140)

    def test_defaults(self) -> None:
        policy = egress_policy.load(_write(self.tmp, ""))
        self.assertIsNone(policy.upstream)
        self.assertEqual(policy.ca_lifetime_days, 7)
        self.assertEqual(policy.memory, "256M")
        self.assertEqual(policy.cpus, 1)
        self.assertEqual(policy.port, 3129)

    def test_out_of_range_values(self) -> None:
        cases = (
            ("[egress.ca]\nlifetime_days = 90\n", "maximum 30"),
            ("[egress.ca]\nlifetime_days = 0\n", "out of range"),
            ("[egress.ca]\nlifetime_days = true\n", "integer"),
            ("[egress.sidecar]\nport = 80\n", "1024 to 65535"),
            ("[egress.sidecar]\nport = 1023\n", "1024 to 65535"),
            ("[egress.sidecar]\nport = 65536\n", "1024 to 65535"),
            ('[egress.sidecar]\nport = "3129"\n', "integer"),
            ("[egress.sidecar]\nport = true\n", "integer"),
            ('[egress.sidecar]\nmemory = "lots"\n', "size"),
            ("[egress.sidecar]\ncpus = 0\n", "1 to 64"),
            ('[egress.upstream]\nurl = "ftp://proxy"\n', "http"),
            ('[egress.upstream]\nurl = "http://u:p@proxy:3128"\n', "credentials"),
            ('[egress.upstream]\nca_cert = ["a.pem"]\n', "requires"),
        )
        for text, message in cases:
            with self.subTest(text=text):
                with self.assertRaisesRegex(EgressPolicyError, message):
                    egress_policy.load(_write(self.tmp, text))

    def test_port_bounds_are_accepted(self) -> None:
        for port in (1024, 65535):
            with self.subTest(port=port):
                path = _write(self.tmp, f"[egress.sidecar]\nport = {port}\n")
                self.assertEqual(egress_policy.load(path).port, port)

    def test_unknown_keys_in_new_tables(self) -> None:
        for table, key in (
            ("upstream", "username"),
            ("ca", "lifetime"),
            ("sidecar", "network"),
            # The internal-network design's forwarders are gone (Decision 1).
            ("sidecar", "forward_ports"),
        ):
            with self.subTest(table=table):
                path = _write(self.tmp, f"[egress.{table}]\n{key} = 1\n")
                with self.assertRaisesRegex(EgressPolicyError, re.escape(repr(key))):
                    egress_policy.load(path)

    def test_upstream_ca_is_read_at_render_time(self) -> None:
        path = _write(
            self.tmp,
            '[egress.upstream]\nurl = "http://proxy.example:3128"\nca_cert = ["c.pem"]\n',
        )
        policy = egress_policy.load(path)  # the file need not exist yet
        with self.assertRaisesRegex(EgressPolicyError, "Cannot read"):
            policy.upstream_ca_bytes()
        (self.tmp / "c.pem").write_bytes(b"PEM")
        self.assertEqual(policy.upstream_ca_bytes(), b"PEM")


class EditTests(TestCase):
    """Task 1.2: tomlkit edits keep comments and are validated atomically."""

    ORIGINAL = """# My egress policy
[egress]
# hosts I need
allow = [
  "pypi.org", # python
]

[egress.copilot]
enabled = false # not yet

[egress.sidecar]
memory = "512M"
"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = _write(Path(self._tmp.name), self.ORIGINAL)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_add_and_remove_hosts_keep_comments(self) -> None:
        policy, added = egress_policy.add_allowed_hosts(
            self.path, ["Registry.NPMJS.org", "pypi.org"]
        )
        self.assertEqual(added, ["registry.npmjs.org"])
        self.assertEqual(policy.allow, ("pypi.org", "registry.npmjs.org"))
        text = self.path.read_text()
        for needle in ("# My egress policy", "# hosts I need", "# python", "# not yet"):
            self.assertIn(needle, text)
        self.assertIn('memory = "512M"', text)
        policy, removed = egress_policy.remove_allowed_hosts(self.path, ["pypi.org"])
        self.assertEqual(removed, ["pypi.org"])
        self.assertEqual(policy.allow, ("registry.npmjs.org",))
        self.assertIn("# not yet", self.path.read_text())

    def test_invalid_edit_leaves_file_byte_identical(self) -> None:
        before = self.path.read_bytes()
        cases: tuple[tuple[Callable[..., object], list], ...] = (
            (egress_policy.add_allowed_hosts, ["github.com"]),
            (egress_policy.add_allowed_hosts, ["*.githubusercontent.com"]),
            (egress_policy.add_allowed_hosts, ["10.0.0.1"]),
            (egress_policy.remove_allowed_hosts, ["example.org"]),
        )
        for call, args in cases:
            with self.subTest(call=call.__name__, args=args):
                with self.assertRaises(EgressPolicyError):
                    call(self.path, args)
                self.assertEqual(self.path.read_bytes(), before)

    def test_edit_revalidates_the_whole_file(self) -> None:
        # A hand-made invalid key elsewhere blocks the edit; nothing is written.
        self.path.write_text(self.ORIGINAL + "bogus = 1\n")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(EgressPolicyError, "bogus"):
            egress_policy.add_allowed_hosts(self.path, ["example.org"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_file_is_created(self) -> None:
        path = Path(self._tmp.name) / "new" / "egress.toml"
        policy, _ = egress_policy.add_allowed_hosts(path, ["pypi.org"])
        self.assertEqual(policy.allow, ("pypi.org",))
        self.assertEqual(egress_policy.load(path).allow, ("pypi.org",))

    def test_file_mode_is_kept(self) -> None:
        self.path.chmod(0o600)
        egress_policy.add_allowed_hosts(self.path, ["example.org"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
