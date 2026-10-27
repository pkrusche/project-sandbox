import contextlib
import io
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unittest.mock import patch

from project_sandbox import cli, doctor
from project_sandbox.doctor import FAIL, INFO, OK, WARN, Probe


class FakeHost:
    """Stub PATH lookups and probe results keyed by argv prefix."""

    def __init__(
        self, binaries: set[str], probes: Mapping[tuple[str, ...], Probe | None]
    ):
        self.binaries = binaries
        self.probes = probes
        self.calls: list[list[str]] = []

    def which(self, name: str) -> str | None:
        return f"/usr/bin/{name}" if name in self.binaries else None

    def run(self, argv: list[str]) -> Probe | None:
        self.calls.append(argv)
        for prefix, probe in self.probes.items():
            if tuple(argv[: len(prefix)]) == prefix:
                return probe
        return Probe(1, "", "unexpected probe")

    @contextlib.contextmanager
    def active(self, platform: str = "linux"):
        with (
            patch.object(doctor.shutil, "which", side_effect=self.which),
            patch.object(doctor, "_run", side_effect=self.run),
            patch.object(doctor.sys, "platform", platform),
        ):
            yield


def ok(stdout: str = "") -> Probe:
    return Probe(0, stdout, "")


HEALTHY_VCS = {
    ("git", "--version"): ok("git version 2.47.3\n"),
    ("git", "config"): ok("Someone\n"),
    ("jj", "--version"): ok("jj 0.46.0\n"),
    ("jj", "workspace", "add", "--help"): ok("  --no-colocate  ...\n"),
}


def by_name(checks: list[doctor.Check]) -> dict[str, doctor.Check]:
    return {check.name: check for check in checks}


class DockerTests(TestCase):
    def test_reachable_daemon_is_ok_and_auto_selects_it(self) -> None:
        host = FakeHost(
            {"docker", "git", "jj"},
            {("docker", "version"): ok("28.1.1\n"), **HEALTHY_VCS},
        )
        with host.active():
            checks = by_name(doctor.run_checks())
        self.assertEqual(checks["docker"].status, OK)
        self.assertIn("28.1.1", checks["docker"].detail)
        self.assertEqual(checks["auto"].status, OK)
        self.assertIn("docker", checks["auto"].detail)

    def test_stopped_daemon_fails_auto_and_suggests_working_podman(self) -> None:
        host = FakeHost(
            {"docker", "podman", "git", "jj"},
            {
                ("docker", "version"): Probe(
                    1, "", "Cannot connect to the Docker daemon\n"
                ),
                ("podman", "info"): ok("5.4.0\n"),
                **HEALTHY_VCS,
            },
        )
        with host.active():
            checks = by_name(doctor.run_checks())
        self.assertEqual(checks["docker"].status, WARN)
        self.assertIn("Cannot connect", checks["docker"].detail)
        self.assertEqual(checks["podman"].status, OK)
        self.assertEqual(checks["auto"].status, FAIL)
        self.assertIn("--runtime podman", checks["auto"].hint or "")

    def test_hung_daemon_is_reported_not_raised(self) -> None:
        host = FakeHost({"docker", "git"}, {("docker", "version"): None, **HEALTHY_VCS})
        with host.active():
            checks = by_name(doctor.run_checks())
        self.assertIn("did not answer", checks["docker"].detail)
        self.assertEqual(checks["auto"].status, FAIL)

    def test_broken_engine_auto_does_not_pick_is_only_a_warning(self) -> None:
        host = FakeHost(
            {"container", "podman", "git", "jj"},
            {
                ("container", "--version"): ok("container CLI version 0.12.3\n"),
                ("container", "system", "status"): ok(),
                ("container", "system", "dns"): ok("host.docker.internal\n"),
                ("podman", "info"): Probe(125, "", "machine not running\n"),
                **HEALTHY_VCS,
            },
        )
        with host.active("darwin"):
            checks = doctor.run_checks()
        named = by_name(checks)
        self.assertEqual(named["podman"].status, WARN)
        self.assertEqual(named["auto"].status, OK)
        self.assertIn("apple-container", named["auto"].detail)
        self.assertFalse(any(check.status == FAIL for check in checks))


class NoRuntimeTests(TestCase):
    def test_no_runtime_fails_with_install_hint(self) -> None:
        host = FakeHost({"git", "jj"}, HEALTHY_VCS)
        with host.active():
            checks = by_name(doctor.run_checks())
        self.assertEqual(checks["auto"].status, FAIL)
        self.assertIn("docker, podman", checks["auto"].hint or "")
        self.assertEqual(checks["docker"].status, INFO)


class AppleContainerTests(TestCase):
    def test_stopped_service_is_ok_and_missing_dns_alias_is_info(self) -> None:
        host = FakeHost(
            {"container", "git", "jj"},
            {
                ("container", "--version"): ok("container CLI version 0.12.3\n"),
                ("container", "system", "status"): Probe(1, "", "not running\n"),
                ("container", "system", "dns"): ok("example.test\n"),
                **HEALTHY_VCS,
            },
        )
        with host.active("darwin"):
            checks = by_name(doctor.run_checks())
        self.assertEqual(checks["apple-container"].status, OK)
        self.assertIn("started on demand", checks["apple-container"].detail)
        dns = checks["dns host.docker.internal"]
        self.assertEqual(dns.status, INFO)
        self.assertIn("container system dns create", dns.hint or "")

    def test_linux_skips_apple_and_probes_chroot(self) -> None:
        host = FakeHost(
            {"docker", "unshare", "socat", "git", "jj"},
            {
                ("docker", "version"): ok("28.1.1\n"),
                ("unshare",): Probe(1, "", "unshare: Operation not permitted\n"),
                **HEALTHY_VCS,
            },
        )
        with host.active():
            checks = by_name(doctor.run_checks())
        self.assertEqual(checks["apple-container"].status, INFO)
        self.assertNotIn(["container", "--version"], host.calls)
        self.assertEqual(checks["chroot"].status, WARN)
        self.assertEqual(checks["socat"].status, OK)


class VcsTests(TestCase):
    def test_missing_git_fails(self) -> None:
        host = FakeHost({"docker"}, {("docker", "version"): ok("28.1.1\n")})
        with host.active():
            checks = by_name(doctor.run_checks())
        self.assertEqual(checks["git"].status, FAIL)
        self.assertEqual(checks["jj"].status, INFO)

    def test_missing_identity_warns(self) -> None:
        host = FakeHost(
            {"docker", "git"},
            {
                ("docker", "version"): ok("28.1.1\n"),
                ("git", "--version"): ok("git version 2.47.3\n"),
                ("git", "config"): Probe(1, "", ""),
            },
        )
        with host.active():
            checks = by_name(doctor.run_checks())
        self.assertEqual(checks["git identity"].status, WARN)
        self.assertIn("user.name and user.email", checks["git identity"].detail)

    def test_jj_without_no_colocate_warns(self) -> None:
        host = FakeHost(
            {"docker", "git", "jj"},
            {
                ("docker", "version"): ok("28.1.1\n"),
                ("git", "--version"): ok("git version 2.47.3\n"),
                ("git", "config"): ok("x\n"),
                ("jj", "--version"): ok("jj 0.40.0\n"),
                ("jj", "workspace", "add", "--help"): ok("Usage: jj workspace add\n"),
            },
        )
        with host.active():
            checks = by_name(doctor.run_checks())
        self.assertEqual(checks["jj"].status, WARN)
        self.assertIn("0.40.0", checks["jj"].detail)
        self.assertIn("0.46", checks["jj"].hint or "")


class CommandTests(TestCase):
    def test_cli_dispatches_and_exits_nonzero_on_failure(self) -> None:
        host = FakeHost({"git", "jj"}, HEALTHY_VCS)
        out = io.StringIO()
        with host.active(), contextlib.redirect_stdout(out):
            rc = cli.main(["doctor"])
        self.assertEqual(rc, 1)
        self.assertIn("[fail] auto", out.getvalue())
        self.assertIn("1 failure(s)", out.getvalue())

    def test_json_output_is_machine_readable(self) -> None:
        host = FakeHost(
            {"docker", "git", "jj"},
            {("docker", "version"): ok("28.1.1\n"), **HEALTHY_VCS},
        )
        out = io.StringIO()
        with host.active(), contextlib.redirect_stdout(out):
            rc = cli.main(["doctor", "--json"])
        self.assertEqual(rc, 0)
        report = json.loads(out.getvalue())
        self.assertTrue(report["ok"])
        names = {check["name"] for check in report["checks"]}
        self.assertTrue({"docker", "podman", "git", "jj", "auto"} <= names)

    def test_rejects_unknown_arguments(self) -> None:
        with self.assertRaisesRegex(SystemExit, "doctor"):
            cli.main(["doctor", "--verbose"])

    def test_probes_are_read_only(self) -> None:
        host = FakeHost(
            {"docker", "podman", "container", "unshare", "git", "jj"}, HEALTHY_VCS
        )
        with host.active("darwin"):
            doctor.run_checks()
        with host.active("linux"):
            doctor.run_checks()
        forbidden = {"start", "run", "build", "create", "init", "new", "stop"}
        for argv in host.calls:
            self.assertFalse(forbidden & set(argv[1:]), argv)
