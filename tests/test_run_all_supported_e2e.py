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

    def test_found_but_down_services_are_noted(self) -> None:
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
            # Skip if a real service happens to listen on the default port.
            if "(up)" in result.stdout:
                self.skipTest("a local proxy service is listening on a default port")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("internet proxy not up", result.stdout)
        self.assertIn("agent proxy not up", result.stdout)
        docker = next(
            line for line in result.stdout.splitlines() if "run-e2e-tests.sh" in line
        )
        self.assertIn("--with-build-proxy", docker)  # needs sources only
        self.assertNotIn("--with-agent-proxy", docker)
        self.assertNotIn("--with-internet-proxy", docker)

    def test_unavailable_runtime_is_rejected(self) -> None:
        result = self._run("--runtimes", "podman")
        self.assertEqual(result.returncode, 64)
        self.assertIn("runtime 'podman' is not available", result.stderr)


if __name__ == "__main__":
    unittest.main()
