"""scripts/run_all_supported_e2e_tests.sh: runtime and proxy detection (dry-run)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_all_supported_e2e_tests.sh"

DOCTOR = {
    "ok": True,
    "checks": [
        {
            "section": "runtimes",
            "name": "apple-container",
            "status": "info",
            "detail": "macOS only",
        },
        {
            "section": "runtimes",
            "name": "docker",
            "status": "ok",
            "detail": "daemon reachable",
        },
        {
            "section": "runtimes",
            "name": "podman",
            "status": "fail",
            "detail": "rootful",
        },
        {
            "section": "runtimes",
            "name": "chroot",
            "status": "ok",
            "detail": "userns ok",
        },
        {
            "section": "runtimes",
            "name": "auto",
            "status": "ok",
            "detail": "selects docker",
        },
    ],
}

# Fake uv: answers `doctor --json` from a file and runs `uv run ... python ARGS`
# with the real interpreter, so the script's JSON parsing is exercised as is.
FAKE_UV = f"""#!{sys.executable}
import os, sys
args = sys.argv[1:]
if "doctor" in args:
    sys.stdout.write(open(os.environ["FAKE_DOCTOR"]).read())
    sys.exit(0)
i = args.index("python")
os.execv({sys.executable!r}, [{sys.executable!r}, *args[i + 1:]])
"""

# Fake colima: logs every invocation; `list --json` prints $FAKE_COLIMA_LIST
# (one JSON object per line, as colima does); start/stop succeed.
FAKE_COLIMA = """#!/usr/bin/env bash
[ -n "${FAKE_COLIMA_LOG:-}" ] && echo "colima $*" >>"$FAKE_COLIMA_LOG"
if [ "$1" = list ]; then printf '%s' "${FAKE_COLIMA_LIST:-}"; fi
exit 0
"""

# Fake docker for colima tests: no context exists, so the real run-e2e-tests.sh
# plan decides the colima suite without reaching a real engine.
FAKE_DOCKER = "#!/usr/bin/env bash\nexit 1\n"


def _install(bindir: Path, name: str, text: str) -> None:
    path = bindir / name
    path.write_text(text)
    path.chmod(0o755)


def _colima_list(*profiles: tuple[str, str]) -> str:
    return "".join(
        json.dumps({"name": name, "status": status}) + "\n" for name, status in profiles
    )


class RunAllSupportedE2ETests(unittest.TestCase):
    def _run(
        self,
        *args: str,
        doctor: dict | None = None,
        colima_list: str = "",
        colima_log: list[str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bindir = tmp_path / "bin"
            bindir.mkdir()
            _install(bindir, "uv", FAKE_UV)
            _install(bindir, "colima", FAKE_COLIMA)
            _install(bindir, "docker", FAKE_DOCKER)
            doctor_path = tmp_path / "doctor.json"
            doctor_path.write_text(json.dumps(DOCTOR if doctor is None else doctor))
            log = tmp_path / "colima.log"
            env = {
                **os.environ,
                "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
                "FAKE_DOCTOR": str(doctor_path),
                "FAKE_COLIMA_LIST": colima_list,
                "FAKE_COLIMA_LOG": str(log),
            }
            result = subprocess.run(
                ["bash", str(SCRIPT), "--dry-run", *args],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            if colima_log is not None:
                colima_log.extend(log.read_text().splitlines() if log.exists() else [])
            return result

    @staticmethod
    def _runs(result: subprocess.CompletedProcess[str]) -> list[str]:
        return [
            line
            for line in result.stdout.splitlines()
            if line.startswith("+ ") and "run-e2e-tests.sh" in line
        ]

    def test_running_colima_profile_runs_colima_once(self) -> None:
        log: list[str] = []
        result = self._run(
            colima_list=_colima_list(
                ("default", "Stopped"), ("project-sandbox", "Running")
            ),
            colima_log=log,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Colima profile:   project-sandbox (running)", result.stdout)
        docker, chroot = self._runs(result)
        self.assertIn("--with-colima --colima-profile project-sandbox", docker)
        self.assertNotIn("--with-colima", chroot)
        self.assertNotIn("+ colima start", result.stdout)
        self.assertNotIn("+ colima stop", result.stdout)
        self.assertEqual(log, ["colima list --json"])

    def test_stopped_colima_profile_is_started_and_stopped(self) -> None:
        log: list[str] = []
        result = self._run(
            colima_list=_colima_list(("project-sandbox", "Stopped")), colima_log=log
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn("is not running; it is started before the plan", out)
        start = out.index("+ colima start --profile project-sandbox")
        self.assertLess(start, out.index("Test plan"))
        self.assertLess(
            out.rindex("e2e-cleanup.py"),
            out.index("+ colima stop --profile project-sandbox"),
        )
        self.assertIn("--with-colima", self._runs(result)[0])
        # A dry run only prints the start and stop.
        self.assertEqual(log, ["colima list --json"])

    def test_missing_colima_profile_is_never_created(self) -> None:
        log: list[str] = []
        result = self._run(
            colima_list=_colima_list(("default", "Running")), colima_log=log
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("colima profile 'project-sandbox' does not exist", result.stdout)
        self.assertIn(
            "project-sandbox colima init --colima-profile project-sandbox",
            result.stdout,
        )
        self.assertTrue(all("--with-colima" not in line for line in self._runs(result)))
        self.assertEqual(log, ["colima list --json"])

    def test_colima_profile_option_selects_the_profile(self) -> None:
        result = self._run(
            "--colima-profile",
            "work",
            colima_list=_colima_list(
                ("project-sandbox", "Running"), ("work", "Stopped")
            ),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("+ colima start --profile work", result.stdout)
        self.assertIn("--with-colima --colima-profile work", self._runs(result)[0])

    def test_colima_runs_with_the_first_container_runtime(self) -> None:
        doctor = json.loads(json.dumps(DOCTOR))
        for check in doctor["checks"]:
            if check["name"] == "podman":
                check["status"] = "ok"
        result = self._run(
            "--runtimes",
            "chroot,podman,docker",
            doctor=doctor,
            colima_list=_colima_list(("project-sandbox", "Running")),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        chroot, podman, docker = self._runs(result)
        self.assertIn("--with-colima", podman)
        self.assertNotIn("--with-colima", chroot)
        self.assertNotIn("--with-colima", docker)

    def test_no_colima_and_no_build_leave_colima_alone(self) -> None:
        stopped = _colima_list(("project-sandbox", "Stopped"))
        for flag, note in (
            ("--no-colima", "colima disabled by --no-colima"),
            ("--no-build", "copilot-egress and colima"),
        ):
            with self.subTest(flag=flag):
                log: list[str] = []
                result = self._run(flag, colima_list=stopped, colima_log=log)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(note, result.stdout)
                self.assertNotIn("+ colima start", result.stdout)
                self.assertTrue(
                    all("--with-colima" not in line for line in self._runs(result))
                )
                self.assertEqual(log, [])

    def test_colima_not_on_path_is_noted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bindir = tmp_path / "bin"
            bindir.mkdir()
            _install(bindir, "uv", FAKE_UV)
            # --incremental fingerprints the checkout with jj or git, which may
            # live outside the system directories (e.g. ~/.cargo/bin/jj).
            for vcs in ("jj", "git"):
                if found := shutil.which(vcs):
                    (bindir / vcs).symlink_to(found)
            doctor_path = tmp_path / "doctor.json"
            doctor_path.write_text(json.dumps(DOCTOR))
            # System directories only, so a Homebrew/user colima stays invisible.
            path = os.pathsep.join((str(bindir), "/usr/bin", "/bin"))
            if shutil.which("colima", path=path) or not shutil.which("bash", path=path):
                self.skipTest("needs a PATH with bash and without colima")
            result = subprocess.run(
                ["bash", str(SCRIPT), "--dry-run", "--runtimes", "chroot"],
                env={**os.environ, "PATH": path, "FAKE_DOCTOR": str(doctor_path)},
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("colima not available: colima not on PATH", result.stdout)
        self.assertIn("Colima profile:   project-sandbox (absent)", result.stdout)

    def test_incremental_is_forwarded_to_every_runtime(self) -> None:
        result = self._run(
            "--incremental",
            "--internet-proxy-dir",
            "/nonexistent/ipl",
            "--agentgateway-dir",
            "/nonexistent/agw",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        runs = [
            line
            for line in result.stdout.splitlines()
            if line.startswith("+ ") and "run-e2e-tests.sh" in line
        ]
        self.assertEqual(len(runs), 2)
        for line in runs:
            self.assertIn("--incremental", line)

    def test_strict_security_coverage_is_opt_in_and_forwarded(self) -> None:
        args = ("--internet-proxy-dir", "/nonexistent/ipl")
        args += ("--agentgateway-dir", "/nonexistent/agw")
        default = self._run(*args)
        strict = self._run("--strict-security-coverage", *args)
        self.assertEqual(strict.returncode, 0, strict.stderr)
        self.assertNotIn("--strict-security-coverage", default.stdout)
        runs = [
            line
            for line in strict.stdout.splitlines()
            if line.startswith("+ ") and "run-e2e-tests.sh" in line
        ]
        self.assertEqual(len(runs), 2)
        for line in runs:
            self.assertIn("--strict-security-coverage", line)

    def test_plans_only_ok_runtimes_and_notes_missing_proxies(self) -> None:
        result = self._run(
            "--internet-proxy-dir",
            "/nonexistent/ipl",
            "--agentgateway-dir",
            "/nonexistent/agw",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn("Runtimes to test: docker chroot", out)
        self.assertIn("runtime podman not available: rootful", out)
        self.assertIn("internet proxy not available", out)
        self.assertIn("agent proxy not available", out)
        commands = [line for line in out.splitlines() if line.startswith("+ ")]
        self.assertEqual(len(commands), 4)
        before, docker, chroot, after = commands
        self.assertIn("--runtime docker", docker)
        self.assertIn("--with-proxy-endpoints", docker)
        self.assertIn("--with-egress-sidecar", docker)
        self.assertIn("--with-security", docker)
        self.assertNotIn("--security-peer-runtime", docker)
        self.assertRegex(out, r"security\s+RUN\s+SKIP")
        self.assertIn("pass --security-peer-runtime NAME to run it", out)
        self.assertIn("--with-copilot", docker)
        self.assertIn("--with-copilot-egress", docker)
        for flag in (
            "--with-build-proxy",
            "--with-agent-proxy",
            "--with-internet-proxy",
        ):
            self.assertNotIn(flag, docker)
        self.assertTrue(
            chroot.endswith(
                "run-e2e-tests.sh --runtime chroot --replace-existing --incremental"
            ),
            chroot,
        )
        # Cleanup brackets the runs and covers container runtimes only.
        for cleanup in (before, after):
            self.assertTrue(
                cleanup.endswith("e2e-cleanup.py --runtime docker"), cleanup
            )

    def test_security_peer_is_opt_in(self) -> None:
        doctor = json.loads(json.dumps(DOCTOR))
        for check in doctor["checks"]:
            if check["name"] in ("apple-container", "podman"):
                check["status"] = "ok"

        def commands(result):
            self.assertEqual(result.returncode, 0, result.stderr)
            return {
                runtime: next(
                    line
                    for line in result.stdout.splitlines()
                    if line.startswith("+ ")
                    and "run-e2e-tests.sh" in line
                    and f"--runtime {runtime}" in line
                )
                for runtime in ("apple-container", "docker", "podman", "chroot")
            }

        # Never chosen automatically, even with several container runtimes.
        default = self._run(doctor=doctor)
        for runtime, command in commands(default).items():
            self.assertNotIn("--security-peer-runtime", command, runtime)
            if runtime != "chroot":
                self.assertIn("--with-security", command)
        self.assertIn("pass --security-peer-runtime NAME to run it", default.stdout)

        chosen = commands(self._run("--security-peer-runtime", "podman", doctor=doctor))
        for runtime in ("apple-container", "docker"):
            self.assertIn("--security-peer-runtime podman", chosen[runtime])
        self.assertNotIn("--security-peer-runtime", chosen["podman"])
        self.assertNotIn("--security-peer-runtime", chosen["chroot"])

    def test_security_peer_must_be_an_available_container_runtime(self) -> None:
        for peer in ("podman", "chroot", "colima"):
            with self.subTest(peer=peer):
                result = self._run("--security-peer-runtime", peer)
                self.assertEqual(result.returncode, 64)
                self.assertIn("not an available container runtime", result.stderr)

    def test_no_build_omits_security_and_peer_selection(self) -> None:
        result = self._run("--no-build")
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = [
            line for line in result.stdout.splitlines() if line.startswith("+ ")
        ]
        self.assertTrue(all("--with-security" not in line for line in commands))
        self.assertTrue(all("--security-peer-runtime" not in line for line in commands))
        self.assertRegex(result.stdout, r"security\s+SKIP\s+SKIP")
        self.assertIn("egress-sidecar, security, build-proxy", result.stdout)

    def test_security_fixture_options_are_forwarded(self) -> None:
        result = self._run(
            "--runtimes",
            "docker",
            "--",
            "--security-peer-runtime",
            "podman",
            "--security-fixture-host",
            "192.0.2.10",
            "--security-nat64-origin",
            "origin.example.test",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        command = next(
            line
            for line in result.stdout.splitlines()
            if line.startswith("+ ") and "run-e2e-tests.sh" in line
        )
        self.assertIn("--security-peer-runtime podman", command)
        self.assertIn("--security-fixture-host 192.0.2.10", command)
        self.assertIn("--security-nat64-origin origin.example.test", command)

    def test_no_copilot_drops_copilot_suites(self) -> None:
        result = self._run("--runtimes", "docker", "--no-copilot")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("disabled by --no-copilot", result.stdout)
        docker = next(
            line for line in result.stdout.splitlines() if "run-e2e-tests.sh" in line
        )
        self.assertNotIn("--with-copilot", docker)

    def test_chroot_only_skips_cleanup(self) -> None:
        result = self._run("--runtimes", "chroot")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("e2e-cleanup.py", result.stdout)

    def test_found_but_down_services_still_run_proxy_suites(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ipl = Path(tmp) / "ipl"
            agw = Path(tmp) / "agw"
            ipl.mkdir()
            agw.mkdir()
            (ipl / "pyproject.toml").write_text("")
            (agw / "run.py").write_text("")
            result = self._run(
                "--runtimes",
                "docker",
                "--internet-proxy-dir",
                str(ipl),
                "--agentgateway-dir",
                str(agw),
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        # A real service may listen on a default port; only expect the
        # "not up" note for a service that is actually down.
        if "(down)" in next(
            line
            for line in result.stdout.splitlines()
            if line.startswith("Internet proxy:")
        ):
            self.assertIn("internet proxy not up", result.stdout)
        if "(down)" in next(
            line
            for line in result.stdout.splitlines()
            if line.startswith("Agent proxy:")
        ):
            self.assertIn("agent proxy not up", result.stdout)
        docker = next(
            line for line in result.stdout.splitlines() if "run-e2e-tests.sh" in line
        )
        # Checkouts alone enable the proxy suites; down services are only noted.
        for flag in (
            "--with-build-proxy",
            "--with-agent-proxy",
            "--with-internet-proxy",
        ):
            self.assertIn(flag, docker)
        self.assertIn(f"--internet-proxy-dir {ipl}", docker)
        self.assertIn(f"--agentgateway-dir {agw}", docker)

    def test_no_internet_proxy_isolation_keeps_agent_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ipl = Path(tmp) / "ipl"
            agw = Path(tmp) / "agw"
            ipl.mkdir()
            agw.mkdir()
            (ipl / "pyproject.toml").write_text("")
            (agw / "run.py").write_text("")
            result = self._run(
                "--runtimes",
                "docker",
                "--internet-proxy-dir",
                str(ipl),
                "--agentgateway-dir",
                str(agw),
                "--no-internet-proxy-isolation",
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("disabled by --no-internet-proxy-isolation", result.stdout)
        docker = next(
            line for line in result.stdout.splitlines() if "run-e2e-tests.sh" in line
        )
        self.assertIn("--with-agent-proxy", docker)
        self.assertNotIn("--with-internet-proxy", docker)

    def test_unavailable_runtime_is_rejected(self) -> None:
        result = self._run("--runtimes", "podman")
        self.assertEqual(result.returncode, 64)
        self.assertIn("runtime 'podman' is not available", result.stderr)


# Stub runner: --plan prints RUN rows; execution fails security on Docker
# and smoke on chroot, mimicking run-e2e-tests.sh's Summary format.
FAKE_RUNNER = """#!/usr/bin/env bash
rt=""; plan=0; security=0; colima=0
while [ $# -gt 0 ]; do
  case "$1" in --runtime) rt="$2"; shift 2 ;; --plan) plan=1; shift ;; --with-security) security=1; shift ;; --with-colima) colima=1; shift ;; *) shift ;; esac
done
[ -n "${FAKE_COLIMA_LOG:-}" ] && echo "runner $rt plan=$plan" >>"$FAKE_COLIMA_LOG"
echo "[1/2] smoke ..."
echo "========================================"
echo "Summary"
echo "========================================"
if [ -n "${FAKE_COLIMA_ROWS:-}" ]; then
  if [ "$colima" = 1 ]; then echo "  $([ "$plan" = 1 ] && echo RUN || echo PASS)  colima"
  else echo "  SKIP colima  (pass --with-colima; needs a running Colima profile, network and builds)"; fi
fi
if [ "$plan" = 1 ]; then
  echo "  RUN  smoke"
  if [ "$rt" = chroot ]; then echo "  SKIP egress-sidecar  (pass --with-egress-sidecar; needs network and builds)"
  else echo "  RUN  egress-sidecar"; fi
  if [ "$security" = 1 ]; then echo "  RUN  security"
  else echo "  SKIP security  (pass --with-security)"; fi
  exit 0
fi
if [ "$rt" = chroot ]; then
  echo "  FAIL smoke  (log: /tmp/x/smoke.log)"
  echo "  SKIP egress-sidecar  (pass --with-egress-sidecar; needs network and builds)"
  echo "  SKIP security  (pass --with-security)"
  exit 1
fi
echo "  PASS smoke"
echo "  PASS egress-sidecar"
if [ "$security" = 1 ]; then
  echo "  FAIL security  (log: /tmp/x/security.log)"
  exit 1
fi
echo "  SKIP security  (pass --with-security)"
"""


class RunAllSupportedE2ETablesTests(unittest.TestCase):
    def _execute(
        self, colima_list: str = "", **extra_env: str
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        """Really run the script against FAKE_RUNNER, a fake uv and a fake colima."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            scripts = tmp_path / "repo" / "scripts"
            scripts.mkdir(parents=True)
            runner = scripts / "run_all_supported_e2e_tests.sh"
            runner.write_text(SCRIPT.read_text())
            _install(scripts, "run-e2e-tests.sh", FAKE_RUNNER)
            (scripts / "e2e-cleanup.py").write_text("print('cleaned')\n")
            bindir = tmp_path / "bin"
            bindir.mkdir()
            _install(bindir, "uv", FAKE_UV)
            _install(bindir, "colima", FAKE_COLIMA)
            doctor = tmp_path / "doctor.json"
            doctor.write_text(json.dumps(DOCTOR))
            log = tmp_path / "calls.log"
            env = {
                **os.environ,
                "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
                "FAKE_DOCTOR": str(doctor),
                "FAKE_COLIMA_LIST": colima_list,
                "FAKE_COLIMA_LOG": str(log),
                "TMPDIR": str(tmp_path),
                **extra_env,
            }
            result = subprocess.run(
                [
                    "bash",
                    str(runner),
                    "--internet-proxy-dir",
                    "/nonexistent",
                    "--agentgateway-dir",
                    "/nonexistent",
                ],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            calls = log.read_text().splitlines() if log.exists() else []
        return result, calls

    def test_stopped_colima_profile_is_started_for_the_run_and_stopped(self) -> None:
        result, calls = self._execute(
            _colima_list(("project-sandbox", "Stopped")), FAKE_COLIMA_ROWS="1"
        )
        out = result.stdout
        self.assertEqual(
            calls,
            [
                "colima list --json",
                "colima start --profile project-sandbox",
                "runner docker plan=1",
                "runner chroot plan=1",
                "runner docker plan=0",
                "runner chroot plan=0",
                "colima stop --profile project-sandbox",
            ],
        )
        plan = out[out.index("Test plan") : out.index("Cleanup (before)")]
        self.assertRegex(plan, r"colima\s+RUN\s+SKIP")
        self.assertIn("colima [chroot]: runs once, with docker", plan)
        outcome = out[out.index("Outcome") :]
        self.assertRegex(outcome, r"colima\s+PASS\s+SKIP")
        self.assertIn("PASS colima-start", outcome)
        self.assertIn("PASS colima-stop", outcome)

    def test_already_running_colima_profile_is_left_running(self) -> None:
        _, calls = self._execute(_colima_list(("project-sandbox", "Running")))
        self.assertNotIn("colima start --profile project-sandbox", calls)
        self.assertNotIn("colima stop --profile project-sandbox", calls)

    def test_plan_and_outcome_tables(self) -> None:
        result, _ = self._execute()
        out = result.stdout
        self.assertEqual(result.returncode, 1, out + result.stderr)
        plan = out[out.index("Test plan") : out.index("Cleanup (before)")]
        self.assertRegex(plan, r"Suite\s+docker\s+chroot")
        self.assertRegex(plan, r"smoke\s+RUN\s+RUN")
        self.assertRegex(plan, r"egress-sidecar\s+RUN\s+SKIP")
        self.assertRegex(plan, r"security\s+RUN\s+SKIP")
        self.assertIn("egress-sidecar [chroot]: needs a container runtime", plan)
        outcome = out[out.index("Outcome") :]
        self.assertRegex(outcome, r"smoke\s+PASS\s+FAIL")
        self.assertRegex(outcome, r"egress-sidecar\s+PASS\s+SKIP")
        self.assertRegex(outcome, r"security\s+FAIL\s+SKIP")
        self.assertIn("Totals: 2 PASS, 2 FAIL, 2 SKIP", outcome)
        self.assertIn("security [docker]: log: /tmp/x/security.log", outcome)
        self.assertIn("FAIL docker  (exit 1)", outcome)
        self.assertIn("smoke [chroot]: log: /tmp/x/smoke.log", outcome)
        self.assertIn("PASS cleanup-before", outcome)
        self.assertIn("FAIL chroot  (exit 1)", outcome)
        self.assertIn("PASS cleanup-after", outcome)


class RunE2EPlanTests(unittest.TestCase):
    def test_plan_executes_nothing(self) -> None:
        """--plan decides every suite but runs none (chroot keeps it host-only)."""
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [
                    "bash",
                    str(ROOT / "scripts" / "run-e2e-tests.sh"),
                    "--plan",
                    "--runtime",
                    "chroot",
                ],
                env={**os.environ, "TMPDIR": tmp},
                capture_output=True,
                text=True,
                check=False,
            )
            leftovers = list(Path(tmp).iterdir())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("  RUN  smoke", result.stdout)
        self.assertIn("  SKIP timeout-teardown", result.stdout)
        self.assertNotIn("PASS", result.stdout)
        self.assertRegex(result.stdout, r"Plan: \d+ suite\(s\) would run")
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()


class DefaultForwardingTests(unittest.TestCase):
    """--replace-existing and --incremental are on by default in run_all_supported."""

    RUNNER = SCRIPT.parent / "run-e2e-tests.sh"
    _run = RunAllSupportedE2ETests._run
    _runs = staticmethod(RunAllSupportedE2ETests._runs)
    PROXIES = (
        "--internet-proxy-dir",
        "/nonexistent/ipl",
        "--agentgateway-dir",
        "/nonexistent/agw",
    )

    def forwarded(self, *args: str) -> list[str]:
        result = self._run(*args, *self.PROXIES)
        self.assertEqual(result.returncode, 0, result.stderr)
        runs = self._runs(result)
        self.assertEqual(len(runs), 2)
        return runs

    def test_both_are_forwarded_by_default(self) -> None:
        for line in self.forwarded():
            self.assertIn(" --replace-existing", line)
            self.assertIn(" --incremental", line)

    def test_each_can_be_turned_off(self) -> None:
        for line in self.forwarded("--no-replace-existing"):
            self.assertNotIn("--replace-existing", line)
            self.assertIn(" --incremental", line)
        for line in self.forwarded("--no-incremental"):
            self.assertNotIn("--incremental", line)
            self.assertIn(" --replace-existing", line)

    def test_explicit_flags_are_not_forwarded_twice(self) -> None:
        for line in self.forwarded("--incremental"):
            self.assertEqual(line.count("--incremental"), 1)
        for line in self.forwarded("--no-incremental", "--incremental"):
            self.assertEqual(line.count("--incremental"), 1)

    def runner_args(self, suite: str, *extra: str) -> list[str]:
        """The arguments run-e2e-tests.sh passes to ``suite``'s script."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root / "arguments"
            fake_uv = root / "uv"
            fake_uv.write_text(
                '#!/bin/sh\nprintf "%s " "$@" >> "$E2E_ARGUMENT_LOG"\n'
                'echo >> "$E2E_ARGUMENT_LOG"\n'
            )
            fake_uv.chmod(0o755)
            result = subprocess.run(
                [
                    "bash",
                    str(self.RUNNER),
                    "--only",
                    suite,
                    "--runtime",
                    "docker",
                    *extra,
                ],
                env={
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "E2E_ARGUMENT_LOG": str(log),
                },
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            lines = log.read_text().splitlines() if log.exists() else []
        script = {
            "egress-sidecar": "e2e-egress-sidecar.py",
            "copilot-egress": "e2e-copilot-egress.py",
            "colima": "e2e-colima.py",
            "security": "security_e2e",
        }[suite]
        runs = [
            line.split()
            for line in lines
            if script in line and "--check-available" not in line
        ]
        self.assertEqual(len(runs), 1, result.stdout + result.stderr + str(lines))
        return runs[0]

    def test_the_runner_forwards_replace_existing_only_when_asked(self) -> None:
        for suite in ("egress-sidecar", "copilot-egress", "colima", "security"):
            with self.subTest(suite=suite):
                self.assertNotIn("--replace-existing", self.runner_args(suite))
                self.assertIn(
                    "--replace-existing",
                    self.runner_args(suite, "--replace-existing"),
                )

    def test_the_security_suite_refuses_an_existing_sidecar_unless_asked(self) -> None:
        source = (ROOT / "scripts/security_e2e/test_network.py").read_text()
        fixture = source[source.index("def sidecar(sandbox, request):") :]
        fixture = fixture[: fixture.index("def reached(")]
        self.assertIn('getoption("--replace-existing")', fixture)
        self.assertIn("assert existing is None or replace", fixture)
        # The replacement is an explicit `down` before the first launch.
        self.assertLess(fixture.index('cli("down"'), fixture.index("yield fixture"))
        conftest = (ROOT / "scripts/security_e2e/conftest.py").read_text()
        self.assertIn('"--replace-existing"', conftest)
