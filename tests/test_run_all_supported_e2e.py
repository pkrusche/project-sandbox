"""scripts/run_all_supported_e2e_tests.sh: runtime and proxy detection (dry-run)."""

from __future__ import annotations

import json
import os
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


class RunAllSupportedE2ETests(unittest.TestCase):
    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bindir = tmp_path / "bin"
            bindir.mkdir()
            uv = bindir / "uv"
            uv.write_text(FAKE_UV)
            uv.chmod(0o755)
            doctor = tmp_path / "doctor.json"
            doctor.write_text(json.dumps(DOCTOR))
            env = {
                **os.environ,
                "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
                "FAKE_DOCTOR": str(doctor),
            }
            return subprocess.run(
                ["bash", str(SCRIPT), "--dry-run", *args],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

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
        self.assertIn("--with-copilot", docker)
        self.assertIn("--with-copilot-egress", docker)
        for flag in (
            "--with-build-proxy",
            "--with-agent-proxy",
            "--with-internet-proxy",
        ):
            self.assertNotIn(flag, docker)
        self.assertTrue(chroot.endswith("run-e2e-tests.sh --runtime chroot"), chroot)
        # Cleanup brackets the runs and covers container runtimes only.
        for cleanup in (before, after):
            self.assertTrue(
                cleanup.endswith("e2e-cleanup.py --runtime docker"), cleanup
            )

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


# Stub runner: --plan prints RUN rows; a real run passes on docker but fails
# git-workflow on chroot, mimicking run-e2e-tests.sh's Summary format.
FAKE_RUNNER = """#!/usr/bin/env bash
rt=""; plan=0
while [ $# -gt 0 ]; do
  case "$1" in --runtime) rt="$2"; shift 2 ;; --plan) plan=1; shift ;; *) shift ;; esac
done
echo "[1/2] smoke ..."
echo "========================================"
echo "Summary"
echo "========================================"
if [ "$plan" = 1 ]; then
  echo "  RUN  smoke"
  if [ "$rt" = chroot ]; then echo "  SKIP egress-sidecar  (pass --with-egress-sidecar; needs network and builds)"
  else echo "  RUN  egress-sidecar"; fi
  exit 0
fi
if [ "$rt" = chroot ]; then
  echo "  FAIL smoke  (log: /tmp/x/smoke.log)"
  echo "  SKIP egress-sidecar  (pass --with-egress-sidecar; needs network and builds)"
  exit 1
fi
echo "  PASS smoke"
echo "  PASS egress-sidecar"
"""


class RunAllSupportedE2ETablesTests(unittest.TestCase):
    def test_plan_and_outcome_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            scripts = tmp_path / "repo" / "scripts"
            scripts.mkdir(parents=True)
            runner = scripts / "run_all_supported_e2e_tests.sh"
            runner.write_text(SCRIPT.read_text())
            fake = scripts / "run-e2e-tests.sh"
            fake.write_text(FAKE_RUNNER)
            fake.chmod(0o755)
            (scripts / "e2e-cleanup.py").write_text("print('cleaned')\n")
            bindir = tmp_path / "bin"
            bindir.mkdir()
            uv = bindir / "uv"
            uv.write_text(FAKE_UV)
            uv.chmod(0o755)
            doctor = tmp_path / "doctor.json"
            doctor.write_text(json.dumps(DOCTOR))
            env = {
                **os.environ,
                "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
                "FAKE_DOCTOR": str(doctor),
                "TMPDIR": str(tmp_path),
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
        out = result.stdout
        self.assertEqual(result.returncode, 1, out + result.stderr)
        plan = out[out.index("Test plan") : out.index("Cleanup (before)")]
        self.assertRegex(plan, r"Suite\s+docker\s+chroot")
        self.assertRegex(plan, r"smoke\s+RUN\s+RUN")
        self.assertRegex(plan, r"egress-sidecar\s+RUN\s+SKIP")
        self.assertIn("egress-sidecar [chroot]: needs a container runtime", plan)
        outcome = out[out.index("Outcome") :]
        self.assertRegex(outcome, r"smoke\s+PASS\s+FAIL")
        self.assertRegex(outcome, r"egress-sidecar\s+PASS\s+SKIP")
        self.assertIn("Totals: 2 PASS, 1 FAIL, 1 SKIP", outcome)
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
