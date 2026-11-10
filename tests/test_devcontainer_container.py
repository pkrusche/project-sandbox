"""Build and run the generated devcontainer (minimal-devcontainer task 6.1).

Opt-in with PROJECT_SANDBOX_CONTAINER_TESTS=1. For each available Docker or
Podman runtime, this renders ``--devcontainer --python-uv`` for a small uv
project, builds ``Dockerfile.devcontainer`` the way an IDE does (the rendered
``build`` settings), and runs it with the rendered ``runArgs``, mounts and
``containerEnv``. Inside, as the ``agent`` user, it checks that:

- ``uv run pytest`` passes offline (the fixture's tests include a loopback
  server);
- a TCP connection to a public address fails;
- ``sudo -n true`` fails;
- ``/workspace/.devcontainer`` is read-only, and the host file is unchanged;
- no agent CLI or firewall script is installed.

The image build needs network access; the container run has none.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from project_sandbox import cli

ROOT = Path(__file__).resolve().parents[1]

_FIXTURE_TEST = """\
import socket
import threading


def test_loopback_server():
    server = socket.create_server(("127.0.0.1", 0))
    port = server.getsockname()[1]

    def serve():
        conn, _ = server.accept()
        with conn:
            conn.sendall(conn.recv(16).upper())

    thread = threading.Thread(target=serve)
    thread.start()
    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
        client.sendall(b"ping")
        assert client.recv(16) == b"PING"
    thread.join(timeout=5)
    server.close()
"""

# Each check prints "<name>=ok" or "<name>=FAIL", so one container run reports
# every result. The host compares the set.
_CHECKS = r"""
set -u
check() { name="$1"; shift; if "$@" >/tmp/out 2>&1; then echo "$name=ok"; else echo "$name=FAIL"; sed 's/^/    /' /tmp/out; fi; }
check_fails() { name="$1"; shift; if "$@" >/tmp/out 2>&1; then echo "$name=FAIL"; sed 's/^/    /' /tmp/out; else echo "$name=ok"; fi; }
check init /usr/local/bin/project-sandbox-devcontainer-init
check is_agent test "$(id -un)" = agent
check uv_offline test "${UV_OFFLINE:-}" = 1
check pytest uv run pytest -q -p no:cacheprovider
check_fails public_tcp python3 -c 'import socket; socket.create_connection(("1.1.1.1", 443), timeout=5)'
check_fails sudo sudo -n true
check_fails devcontainer_write sh -c 'echo x >> /workspace/.devcontainer/devcontainer.json'
check_fails devcontainer_create touch /workspace/.devcontainer/planted.json
check_fails sandbox_write touch /workspace/.project-sandbox/planted
check no_agents sh -c '! command -v claude && ! command -v codex && ! command -v opencode && ! command -v pi && ! command -v copilot'
check no_firewall test ! -e /usr/local/bin/project-sandbox-init-firewall
"""

_EXPECTED = {
    "init",
    "is_agent",
    "uv_offline",
    "pytest",
    "public_tcp",
    "sudo",
    "devcontainer_write",
    "devcontainer_create",
    "sandbox_write",
    "no_agents",
    "no_firewall",
}


def _available_runtimes() -> list[str]:
    found = []
    for cmd in ("docker", "podman"):
        path = shutil.which(cmd)
        if path is None:
            continue
        try:
            info = subprocess.run(
                [path, "info"], capture_output=True, timeout=15, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if info.returncode == 0:
            found.append(path)
    return found


@unittest.skipUnless(
    os.environ.get("PROJECT_SANDBOX_CONTAINER_TESTS") == "1",
    "set PROJECT_SANDBOX_CONTAINER_TESTS=1 to run real-container tests",
)
class DevcontainerContainerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("uv") is None:
            raise unittest.SkipTest("uv is required to lock the fixture")
        cls.runtimes = _available_runtimes()
        if not cls.runtimes:
            raise unittest.SkipTest("docker/podman not available")

    def setUp(self) -> None:
        # Under the repo like the other container suites: the runtimes' VMs
        # share the home directory but not always the per-user temp directory.
        base = ROOT / ".project-sandbox" / "e2e"
        base.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base, prefix="devcontainer.")
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "project"
        self._make_project()

    def _make_project(self) -> None:
        project = self.project
        (project / "tests").mkdir(parents=True)
        (project / "pyproject.toml").write_text(
            '[project]\nname = "devcontainer-fixture"\nversion = "0.1.0"\n'
            'requires-python = ">=3.11"\ndependencies = []\n\n'
            '[dependency-groups]\ndev = ["pytest>=8"]\n',
            encoding="utf-8",
        )
        (project / "tests" / "test_loopback.py").write_text(
            _FIXTURE_TEST, encoding="utf-8"
        )
        subprocess.run(["uv", "lock"], cwd=project, check=True, capture_output=True)

    def _render(self) -> dict:
        with contextlib.redirect_stdout(io.StringIO()):
            rc = cli.main(
                [
                    "--devcontainer",
                    "--python-uv",
                    "--memory",
                    "2g",
                    "--cpus",
                    "2",
                    str(self.project),
                ]
            )
        self.assertEqual(rc, 0)
        return json.loads(
            (self.project / ".devcontainer" / "devcontainer.json").read_text()
        )

    def _resolve(self, value: str) -> str:
        return value.replace("${localWorkspaceFolder}", str(self.project))

    def test_devcontainer_runs_tests_offline_without_privileges(self) -> None:
        spec = self._render()
        dc_dir = self.project / ".devcontainer"
        config_before = (dc_dir / "devcontainer.json").read_bytes()
        subprocess.run(
            [self._resolve(arg) for arg in spec["initializeCommand"]], check=True
        )
        for runtime in self.runtimes:
            with self.subTest(runtime=Path(runtime).name):
                tag = f"project-sandbox-devcontainer-test:{os.getpid()}"
                build = subprocess.run(
                    [
                        runtime,
                        "build",
                        "-t",
                        tag,
                        "-f",
                        str(dc_dir / spec["build"]["dockerfile"]),
                        str(dc_dir / spec["build"]["context"]),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=1800,
                    check=False,
                )
                self.assertEqual(build.returncode, 0, build.stderr[-4000:])
                self.addCleanup(
                    subprocess.run,
                    [runtime, "rmi", "-f", tag],
                    capture_output=True,
                    check=False,
                )
                argv = [runtime, "run", "--rm", *spec["runArgs"]]
                # VS Code passes these the same way; consistency= is a Docker
                # Desktop hint that other runtimes need not accept.
                workspace_mount = ",".join(
                    part
                    for part in self._resolve(spec["workspaceMount"]).split(",")
                    if not part.startswith("consistency=")
                )
                argv += ["--mount", workspace_mount]
                for mount in spec["mounts"]:
                    argv += ["--mount", self._resolve(mount)]
                for name, value in spec["containerEnv"].items():
                    argv += ["-e", f"{name}={value}"]
                for name, value in spec["remoteEnv"].items():
                    argv += ["-e", f"{name}={value}"]
                argv += [
                    "--user",
                    spec["remoteUser"],
                    "-w",
                    spec["workspaceFolder"],
                    tag,
                    "bash",
                    "-c",
                    _CHECKS,
                ]
                run = subprocess.run(
                    argv, capture_output=True, text=True, timeout=600, check=False
                )
                output = run.stdout + run.stderr
                passed = {
                    line.split("=", 1)[0]
                    for line in run.stdout.splitlines()
                    if line.endswith("=ok")
                }
                self.assertEqual(passed, _EXPECTED, output)
                self.assertEqual(
                    (dc_dir / "devcontainer.json").read_bytes(), config_before
                )
                self.assertFalse((dc_dir / "planted.json").exists())


if __name__ == "__main__":
    unittest.main()
