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
                ("podman", "info"): ok("5.4.0 true\n"),
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

    def test_rootful_podman_warns_and_auto_fails(self) -> None:
        host = FakeHost(
            {"podman", "git", "jj"},
            {("podman", "info"): ok("5.4.2 false\n"), **HEALTHY_VCS},
        )
        with host.active():
            checks = by_name(doctor.run_checks())
        self.assertEqual(checks["podman"].status, WARN)
        self.assertIn("rootful", checks["podman"].detail)
        self.assertIn("5.4.2", checks["podman"].detail)
        self.assertEqual(checks["auto"].status, FAIL)

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
            {"docker", "unshare", "git", "jj"},
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
        # Bridge forwarding needs no external tool (share-local-service-forwarders).
        self.assertNotIn("socat", checks)


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


class EgressSidecarTests(TestCase):
    """egress-sidecar-macos task 7.4: a read-only sidecar status line."""

    def _host(self, *, inspect):
        return FakeHost(
            {"docker", "git", "jj"},
            {
                ("docker", "version"): ok("29.0.0\n"),
                ("docker", "inspect", "project-sandbox-egress"): inspect,
                **HEALTHY_VCS,
            },
        )

    def test_reports_the_published_endpoint_read_only(self) -> None:
        host = self._host(
            inspect=ok(
                '[{"State": {"Running": true}, "Config": {"Labels": '
                '{"project-sandbox.egress.port": "3129"}}}]'
            )
        )
        with host.active():
            checks = by_name(doctor.run_checks())
        check = checks["docker sidecar"]
        self.assertEqual(check.status, INFO)
        self.assertEqual(check.section, "egress")
        self.assertIn("project-sandbox-egress running on 127.0.0.1:3129", check.detail)
        egress_calls = [argv for argv in host.calls if "project-sandbox-egress" in argv]
        self.assertEqual(
            egress_calls, [["docker", "inspect", "project-sandbox-egress"]]
        )
        forbidden = {"rm", "run", "exec", "create", "start", "stop", "network"}
        for argv in host.calls:
            self.assertFalse(forbidden & set(argv[1:]), argv)

    def test_reports_the_docker_engine_gateway_endpoint(self) -> None:
        # egress-sidecar-linux: the address label names the bridge gateway.
        host = self._host(
            inspect=ok(
                '[{"State": {"Running": true}, "Config": {"Labels": '
                '{"project-sandbox.egress.port": "3129", '
                '"project-sandbox.egress.address": "172.17.0.1"}}}]'
            )
        )
        with host.active():
            check = by_name(doctor.run_checks())["docker sidecar"]
        self.assertIn("running on 172.17.0.1:3129", check.detail)

    def test_leftover_container_and_absent_sidecar(self) -> None:
        host = self._host(
            inspect=ok('[{"State": {"Running": true}, "Config": {"Labels": {}}}]')
        )
        with host.active():
            checks = by_name(doctor.run_checks())
        self.assertIn("next launch replaces it", checks["docker sidecar"].detail)
        host = self._host(inspect=Probe(1, "", "No such object"))
        with host.active():
            checks = by_name(doctor.run_checks())
        self.assertIn("not running", checks["docker sidecar"].detail)
        self.assertNotIn("podman sidecar", checks)  # not on PATH


SOCKET = "unix:///Users/x/.colima/project-sandbox/docker.sock"
CTX = ("docker", "--context", "colima-project-sandbox")


def _network(
    role: str, internal: bool, *, owner: str | None = "project-sandbox"
) -> Probe:
    labels = {
        "project-sandbox.profile": "project-sandbox",
        "project-sandbox.network.role": role,
    }
    if owner is not None:
        labels["project-sandbox.owner"] = owner
    return ok(json.dumps({"Driver": "bridge", "Internal": internal, "Labels": labels}))


def _sidecar(*, attached: bool) -> Probe:
    networks = {"psbx-egress-uplink": {"IPAddress": "172.18.0.2", "Aliases": []}}
    if attached:
        networks["psbx-sandbox"] = {"IPAddress": "172.19.0.2", "Aliases": ["egress"]}
    return ok(
        json.dumps(
            [
                {
                    "State": {"Running": True},
                    "Config": {"Labels": {}},
                    "NetworkSettings": {"Networks": networks},
                }
            ]
        )
    )


HEALTHY_COLIMA = {
    ("docker", "context", "inspect", "colima-project-sandbox"): ok(SOCKET + "\n"),
    (*CTX, "info", "--format", "{{.ID}}"): ok("ID\n"),
    (*CTX, "version"): ok("29.5.2\n"),
    (*CTX, "info", "--format", "{{.NCPU}} {{.MemTotal}}"): ok("8 17179869184\n"),
    ("colima", "ssh"): ok(""),
    (*CTX, "network", "inspect", "psbx-egress-uplink"): _network(
        "egress-uplink", False
    ),
    (*CTX, "network", "inspect", "psbx-sandbox"): _network("sandbox", True),
    (*CTX, "inspect", "project-sandbox-egress"): Probe(1, "", "No such object"),
    **HEALTHY_VCS,
}


def colima_host(overrides=None, binaries=None, first=None) -> FakeHost:
    """``first`` probes are consulted before the healthy defaults (prefix match)."""
    return FakeHost(
        binaries or {"docker", "colima", "git", "jj"},
        {**(first or {}), **HEALTHY_COLIMA, **(overrides or {})},
    )


class ColimaTests(TestCase):
    name = "colima (project-sandbox)"

    def checks(self, host: FakeHost, profile: str | None = None):
        with host.active("darwin"):
            return by_name(doctor.run_checks(profile))

    def test_hidden_without_colima_on_path_or_a_requested_profile(self) -> None:
        host = colima_host(binaries={"docker", "git", "jj"})
        self.assertNotIn(self.name, self.checks(host))
        self.assertIn(self.name, self.checks(host, "project-sandbox"))

    def test_shown_when_colima_is_installed(self) -> None:
        self.assertIn(self.name, self.checks(colima_host()))

    def test_a_running_profile_is_ok_with_visible_mounts(self) -> None:
        checks = self.checks(colima_host())
        self.assertEqual(checks[self.name].status, doctor.OK)
        self.assertIn("29.5.2", checks[self.name].detail)
        self.assertEqual(checks["mounts"].status, doctor.OK)

    def test_no_network_or_sidecar_yet_is_normal_not_a_problem(self) -> None:
        absent = Probe(1, "", "No such network")
        host = colima_host(
            {
                (*CTX, "network", "inspect", "psbx-egress-uplink"): absent,
                (*CTX, "network", "inspect", "psbx-sandbox"): absent,
            }
        )
        checks = self.checks(host)
        self.assertEqual(checks["psbx-sandbox"].status, doctor.INFO)
        self.assertIn("not created yet", checks["psbx-sandbox"].detail)
        self.assertEqual(checks["colima-project-sandbox sidecar"].status, doctor.INFO)

    def test_a_vm_with_enough_cpus_is_ok(self) -> None:
        check = self.checks(colima_host())["resources"]
        self.assertEqual(check.status, doctor.OK)
        self.assertEqual(check.detail, "8 CPUs, 16.0 GiB")

    def test_a_vm_with_fewer_cpus_than_the_default_reports_the_cap(self) -> None:
        host = colima_host(
            {
                (*CTX, "info", "--format", "{{.NCPU}} {{.MemTotal}}"): ok(
                    "2 2147483648\n"
                )
            }
        )
        check = self.checks(host)["resources"]
        self.assertEqual(check.status, doctor.INFO)
        self.assertIn("2 CPUs, 2.0 GiB", check.detail)
        self.assertIn("capped to 2 CPUs instead of the default 4", check.detail)
        self.assertIn("--cpu 4", check.hint or "")

    def test_an_unreadable_vm_size_is_info(self) -> None:
        host = colima_host(
            {(*CTX, "info", "--format", "{{.NCPU}} {{.MemTotal}}"): ok("garbage\n")}
        )
        self.assertEqual(self.checks(host)["resources"].status, doctor.INFO)

    def test_a_profile_that_was_never_started_is_info_with_the_start_command(
        self,
    ) -> None:
        host = colima_host(
            {
                ("docker", "context", "inspect", "colima-project-sandbox"): Probe(
                    1, "", "x"
                )
            }
        )
        check = self.checks(host)[self.name]
        self.assertEqual(check.status, doctor.INFO)
        self.assertIn("colima start --profile project-sandbox", check.hint or "")

    def test_a_stopped_profile_warns_with_the_start_command(self) -> None:
        host = colima_host(
            {(*CTX, "info", "--format", "{{.ID}}"): Probe(1, "", "cannot connect")}
        )
        check = self.checks(host)[self.name]
        self.assertEqual(check.status, doctor.WARN)
        self.assertIn("not running", check.detail)
        self.assertIn("colima start --profile project-sandbox", check.hint or "")

    def test_a_context_that_is_not_colimas_warns(self) -> None:
        host = colima_host(
            {
                ("docker", "context", "inspect", "colima-project-sandbox"): ok(
                    "unix:///var/run/docker.sock\n"
                )
            }
        )
        check = self.checks(host)[self.name]
        self.assertEqual(check.status, doctor.WARN)
        self.assertIn("refusing to use it", check.detail)

    def test_unexported_directories_warn_naming_them(self) -> None:
        host = colima_host({("colima", "ssh"): ok("/somewhere/else\n")})
        with patch.object(
            doctor.colima_mounts,
            "probe_path",
            side_effect=lambda p: Path("/somewhere/else"),
        ):
            check = self.checks(host)["mounts"]
        self.assertEqual(check.status, doctor.WARN)
        self.assertIn("/somewhere/else", check.detail)
        self.assertIn("--mount", check.hint or "")

    def test_the_directories_checked_are_the_ones_the_sandbox_mounts(self) -> None:
        seen: list[Path] = []

        def capture(runtime, paths, **_kwargs):
            seen.extend(paths)
            return []

        with patch.object(doctor.colima_mounts, "missing_in_vm", side_effect=capture):
            self.checks(colima_host())
        names = {p.name for p in seen}
        self.assertIn(Path.cwd().name, names)
        # The session records are host-only: a hardened profile must not be
        # asked about, or told to export, them.
        self.assertNotIn("sessions", names)
        self.assertNotIn("locks", names)

    def test_the_mount_hint_keeps_the_profiles_current_exports(self) -> None:
        host = colima_host(
            {("colima", "ssh"): ok("/somewhere/else\n")},
            first={
                ("colima", "ssh", "--profile", "project-sandbox", "--", "findmnt"): ok(
                    "/Users/x/src rw,relatime\n/Users/x/ro ro,relatime\n"
                )
            },
        )
        with patch.object(
            doctor.colima_mounts,
            "probe_path",
            side_effect=lambda p: Path("/somewhere/else"),
        ):
            check = self.checks(host)["mounts"]
        self.assertEqual(check.status, doctor.WARN)
        hint = check.hint or ""
        self.assertIn("replaces the profile's mounts", hint)
        for flag in (
            "--mount /Users/x/src:w",
            "--mount /Users/x/ro ",
            "--mount /somewhere/else:w",
        ):
            self.assertIn(flag, hint + " ")
        self.assertNotIn("exports only the home directory", hint)

    def test_without_the_colima_binary_mounts_are_not_checked(self) -> None:
        host = colima_host(binaries={"docker", "git", "jj"})
        check = self.checks(host, "project-sandbox")["mounts"]
        self.assertEqual(check.status, doctor.INFO)
        self.assertIn("not checked", check.detail)

    def test_owned_networks_are_ok_and_foreign_ones_warn(self) -> None:
        host = colima_host()
        self.assertEqual(self.checks(host)["psbx-egress-uplink"].status, doctor.OK)
        foreign = colima_host(
            {
                (*CTX, "network", "inspect", "psbx-sandbox"): _network(
                    "sandbox", True, owner=None
                )
            }
        )
        check = self.checks(foreign)["psbx-sandbox"]
        self.assertEqual(check.status, doctor.WARN)
        self.assertIn("not owned by project-sandbox", check.detail)

    def test_an_attached_sidecar_is_info_and_a_detached_one_warns(self) -> None:
        key = (*CTX, "inspect", "project-sandbox-egress")
        attached = self.checks(colima_host({key: _sidecar(attached=True)}))
        self.assertEqual(attached["colima-project-sandbox sidecar"].status, doctor.INFO)
        self.assertIn("egress:3128", attached["colima-project-sandbox sidecar"].detail)
        detached = self.checks(colima_host({key: _sidecar(attached=False)}))
        check = detached["colima-project-sandbox sidecar"]
        self.assertEqual(check.status, doctor.WARN)
        self.assertIn("not attached", check.detail)

    def test_every_probe_is_read_only(self) -> None:
        host = colima_host(
            {(*CTX, "inspect", "project-sandbox-egress"): _sidecar(attached=True)}
        )
        self.checks(host)
        forbidden = {
            "start",
            "run",
            "build",
            "create",
            "init",
            "new",
            "stop",
            "rm",
            "connect",
            "disconnect",
        }
        for argv in host.calls:
            self.assertFalse(forbidden & set(argv[1:]), argv)
        # colima itself is only asked one read-only question.
        for argv in host.calls:
            if argv[0] == "colima":
                self.assertEqual(argv[1], "ssh")
                self.assertNotIn("start", argv[2:6])

    def test_the_profile_option_selects_the_checked_profile(self) -> None:
        import contextlib
        import io

        host = colima_host(
            {
                ("docker", "context", "inspect", "colima-work"): ok(
                    "unix:///Users/x/.colima/work/docker.sock\n"
                ),
                (
                    "docker",
                    "--context",
                    "colima-work",
                    "info",
                    "--format",
                    "{{.ID}}",
                ): ok("ID\n"),
                (
                    "docker",
                    "--context",
                    "colima-work",
                    "info",
                    "--format",
                    "{{.NCPU}} {{.MemTotal}}",
                ): ok("4 4294967296\n"),
                ("docker", "--context", "colima-work", "version"): ok("29.5.2\n"),
            }
        )
        out = io.StringIO()
        with host.active("darwin"), contextlib.redirect_stdout(out):
            cli.main(["doctor", "--colima-profile", "work", "--json"])
        names = [c["name"] for c in json.loads(out.getvalue())["checks"]]
        self.assertIn("colima (work)", names)

    def test_a_bad_profile_name_and_extra_arguments_are_rejected(self) -> None:
        with self.assertRaisesRegex(SystemExit, "Invalid Colima profile"):
            cli.main(["doctor", "--colima-profile", "a/b"])
        for bad in (["--colima-profile"], ["--colima-profile", "a", "b"], ["x"]):
            with self.subTest(bad=bad), self.assertRaisesRegex(SystemExit, "usage"):
                cli.main(["doctor", *bad])


class ColimaExportsTests(TestCase):
    """Home-directory exposure and drift of a profile's exports (read-only)."""

    FINDMNT = ("colima", "ssh", "--profile", "project-sandbox", "--", "findmnt")

    def setUp(self) -> None:
        import os
        import tempfile

        from project_sandbox import colima_mounts, colima_profile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        (self.base / "home").mkdir()
        patcher = patch.dict(
            os.environ,
            {"HOME": str(self.base / "home"), "XDG_STATE_HOME": str(self.base / "st")},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.profile = colima_profile
        self.dirs = [str(d) for d in colima_mounts.required_state_dirs()]
        self.home = str(self.base / "home")

    def checks(self, exports: list[tuple[str, str]]):
        out = "".join(f"{path} {mode},relatime\n" for path, mode in exports)
        host = colima_host(first={self.FINDMNT: ok(out)})
        with host.active("darwin"):
            return by_name(doctor.run_checks()), host

    def test_a_home_export_warns_and_names_init(self) -> None:
        checks, _ = self.checks([(self.home, "rw")])
        check = checks["exports"]
        self.assertEqual(check.status, WARN)
        self.assertIn("home directory", check.detail)
        self.assertIn("project-sandbox colima init", check.hint or "")

    def test_an_ancestor_export_warns(self) -> None:
        checks, _ = self.checks([(str(self.base), "rw")])
        self.assertEqual(checks["exports"].status, WARN)

    def test_a_read_only_home_export_is_not_a_warning(self) -> None:
        checks, _ = self.checks([(self.home, "ro")])
        self.assertNotIn("exports", checks)

    def test_a_dedicated_profile_without_a_record_has_no_finding(self) -> None:
        checks, _ = self.checks([(str(self.base / "work"), "rw")])
        self.assertNotIn("exports", checks)

    def test_exact_exports_of_a_managed_profile_are_ok(self) -> None:
        work = str(self.base / "work")
        self.profile.write_record("project-sandbox", [work])
        checks, _ = self.checks([(work, "rw"), *((d, "rw") for d in self.dirs)])
        self.assertEqual(checks["exports"].status, OK)

    def test_an_extra_export_is_named(self) -> None:
        work = str(self.base / "work")
        self.profile.write_record("project-sandbox", [work])
        checks, _ = self.checks(
            [
                (work, "rw"),
                ("/Users/me/Documents", "rw"),
                *((d, "rw") for d in self.dirs),
            ]
        )
        check = checks["exports"]
        self.assertEqual(check.status, WARN)
        self.assertIn("/Users/me/Documents", check.detail)

    def test_a_missing_root_is_named(self) -> None:
        work = str(self.base / "work")
        self.profile.write_record("project-sandbox", [work])
        checks, _ = self.checks([(d, "rw") for d in self.dirs])
        check = checks["exports"]
        self.assertEqual(check.status, WARN)
        self.assertIn(work, check.detail)

    def test_the_new_probe_is_read_only(self) -> None:
        _, host = self.checks([(self.home, "rw")])
        for argv in host.calls:
            self.assertNotIn(argv[:2], (["colima", "start"], ["colima", "stop"]))
            self.assertNotIn(argv[:2], (["colima", "delete"], ["colima", "init"]))

    def test_the_mount_hint_names_add_root_for_a_managed_profile(self) -> None:
        self.profile.write_record("project-sandbox", [str(self.base / "work")])
        host = colima_host({("colima", "ssh"): ok("/somewhere/else\n")})
        with (
            host.active("darwin"),
            patch.object(
                doctor.colima_mounts,
                "probe_path",
                side_effect=lambda p: Path("/somewhere/else"),
            ),
        ):
            check = by_name(doctor.run_checks())["mounts"]
        self.assertIn(
            "project-sandbox colima add-root /somewhere/else", check.hint or ""
        )
        self.assertNotIn("colima start", check.hint or "")
