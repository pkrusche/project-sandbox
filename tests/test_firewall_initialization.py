"""Exercise generated startup without changing host firewall or sudo policy."""

import os
import re
import select
import shlex
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from project_sandbox import firewall


class FirewallHelperIsolationTests(unittest.TestCase):
    def test_both_helpers_ignore_workspace_modules_and_pythonpath(self):
        with tempfile.TemporaryDirectory() as tmp, socket.socket() as listener:
            root = Path(tmp)
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            hosts = root / "hosts"
            hosts.write_text("127.0.0.1 localhost\n")
            for name in ("ipaddress", "socket", "ssl", "pathlib", "sitecustomize"):
                (root / f"{name}.py").write_text(
                    'raise SystemExit("WORKSPACE MODULE EXECUTED")\n'
                )
            script = firewall.render(
                root,
                extra_domains=[],
                policy=firewall.INTERNET_PROXY,
                local_destinations=[
                    firewall.TcpDestination(
                        "Internet proxy", "127.0.0.1", listener.getsockname()[1]
                    )
                ],
            ).read_text()
            helpers = re.findall(
                r"python3 (.*?) <<'(PY_\w+)'\n(.*?)\n\2",
                script,
                flags=re.DOTALL,
            )
            self.assertEqual(len(helpers), 2)
            for arguments, marker, source in helpers:
                with self.subTest(helper=marker):
                    arguments = arguments.replace('"$IPV6_FW"', "1")
                    source = source.replace(
                        'Path("/etc/hosts")', f"Path({str(hosts)!r})"
                    )
                    result = subprocess.run(
                        [sys.executable, *shlex.split(arguments)],
                        input=source,
                        cwd=root,
                        env={**os.environ, "PYTHONPATH": str(root)},
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=10,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertNotIn("WORKSPACE MODULE EXECUTED", result.stderr)


@unittest.skipUnless(sys.platform == "linux", "Generated firewall uses Linux Bash")
class FirewallInitializationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.guard = self.root / "attempted"
        self.sudoers = self.root / "agent-firewall"
        self.sudoers.write_text("fixture sudo permission\n")
        self.calls = self.root / "calls"
        self.cwd = self.root / "cwd"

    def script(
        self, *, devcontainer=False, fail_rules=False, fail_revoke=False, pause=False
    ):
        firewall.render(self.root, extra_domains=[], policy=firewall.INTERNET_PROXY)
        name = "init-firewall-devcontainer.sh" if devcontainer else "init-firewall.sh"
        script = (self.root / name).read_text()
        # Only these paths are writable in this fixture. Network commands below
        # are shell functions, so no host firewall tools are ever invoked.
        script = script.replace(
            "/run/project-sandbox-firewall-init", shlex.quote(str(self.guard))
        )
        script = script.replace(
            "/etc/sudoers.d/agent-firewall", shlex.quote(str(self.sudoers))
        )
        stubs = (
            "iptables() {\n"
            f"  pwd > {shlex.quote(str(self.cwd))}\n"
            f"  printf '%s\\n' \"$*\" >> {shlex.quote(str(self.calls))}\n"
            + ('  [ "$1" != "-F" ]\n' if fail_rules else "  return 0\n")
            + "}\n"
            "ip6tables() {\n"
            + (
                '  if [ "$1" = "-L" ]; then echo READY >&3; read -r release; fi\n'
                if pause
                else ""
            )
            + '  iptables "$@"\n'
            "}\n"
            "iptables-save() { :; }\n"
            "ip6tables-save() { :; }\n"
            "ipset() { :; }\n" + ("rm() { return 1; }\n" if fail_revoke else "")
        )
        return ("exec 3>&1\n" if pause else "") + stubs + script

    def run_script(self, script):
        return subprocess.run(
            ["bash", "-c", script],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

    def test_success_revokes_permission_and_rejects_rerun_for_both_variants(self):
        for devcontainer in (False, True):
            with self.subTest(devcontainer=devcontainer):
                script = self.script(devcontainer=devcontainer)
                result = self.run_script(script)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Firewall initialized.", result.stdout)
                self.assertFalse(self.sudoers.exists())
                self.assertEqual(self.cwd.read_text().strip(), "/")
                self.assertEqual(self.guard.stat().st_mode & 0o777, 0o700)
                calls = self.calls.read_text()
                retry = self.run_script(script)
                self.assertNotEqual(retry.returncode, 0)
                self.assertIn("fresh container", retry.stderr)
                self.assertEqual(self.calls.read_text(), calls)
                self.guard.rmdir()
                self.sudoers.write_text("fixture sudo permission\n")

    def test_failed_rules_require_fresh_container_without_reporting_success(self):
        result = self.run_script(self.script(fail_rules=True))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Firewall initialized.", result.stdout)
        self.assertTrue(self.sudoers.exists())
        calls = self.calls.read_text()
        retry = self.run_script(self.script())
        self.assertNotEqual(retry.returncode, 0)
        self.assertIn("fresh container", retry.stderr)
        self.assertEqual(self.calls.read_text(), calls)

    def test_failed_sudo_revocation_aborts_startup(self):
        result = self.run_script(self.script(fail_revoke=True))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Firewall initialized.", result.stdout)
        self.assertTrue(self.sudoers.exists())

    def test_concurrent_authorized_invocation_cannot_enter_initialization(self):
        with subprocess.Popen(
            ["bash", "-c", self.script(pause=True)],
            cwd=self.root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as first:
            try:
                ready, _, _ = select.select([first.stdout], [], [], 5)
                self.assertTrue(ready, "first initializer did not reach the barrier")
                self.assertEqual(first.stdout.readline().strip(), "READY")
                second = self.run_script(self.script())
                self.assertNotEqual(second.returncode, 0)
                self.assertIn("fresh container", second.stderr)
                self.assertFalse(self.calls.exists())
                self.assertTrue(self.sudoers.exists())
                stdout, stderr = first.communicate("continue\n", timeout=5)
                self.assertEqual(first.returncode, 0, stderr)
                self.assertIn("Firewall initialized.", stdout)
                self.assertFalse(self.sudoers.exists())
            finally:
                if first.poll() is None:
                    first.kill()
                    first.communicate()
