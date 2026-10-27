"""Check the e2e cleanup script without a container runtime."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

ROOT = Path(__file__).parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "e2e_cleanup", ROOT / "scripts/e2e-cleanup.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cleanup = _load()

SESSION = "project-sandbox-20261027t000001-aaaaaaaaaaaa"
E2E_IMAGE = "project-sandbox-git-e2e.o6thym-5b571f8e:latest"
REAL_IMAGE = "project-sandbox-project-sandbox-tui-1f647a08:latest"


def _docker_entry(name, image, *, labels=None, sources=()):
    return {
        "Name": "/" + name,
        "Config": {"Image": image, "Labels": labels or {}},
        "Mounts": [{"Source": source} for source in sources],
    }


def _squid(source):
    return _docker_entry(
        "internet-proxy-squid-127-0-0-1-3128",
        "ubuntu/squid:6.10",
        labels={"io.internet-proxy-locally.managed": "true"},
        sources=[source],
    )


class ImageMatchTests(TestCase):
    def test_matches_images_the_suites_create(self) -> None:
        for reference in (
            E2E_IMAGE,
            "project-sandbox-env-e2e.gl7bgf-a5bd6795:latest",
            "project-sandbox-dockerfile-tamper-e2e.ooubsu-b5423c23:latest",
            "project-sandbox-node-npm.iyadrz-00144b4e:latest",
            "project-sandbox-project.wv1jts-039bb50b:latest",
            "project-sandbox-managed-python-77d0b0bd:latest",
            "project-sandbox-base-python-90f044c5:latest",
            "project-sandbox-agent-proxy-isolation.zjlx_f_i-62355878:latest",
            "project-sandbox-agent-proxy-isolation-2-x6-ttl-352db7dd:latest",
            "project-sandbox-project-sandbox-agent-proxy-k_pe0wet-d299a8bf:latest",
            "project-sandbox-internet-proxy-e2e.0jr2jo3-bad83733:latest",
            "project-sandbox-internet-proxy-smoke:latest",
            "project-sandbox-proxy-endpoints-cppn_ok7:latest",
            "project-sandbox-build-proxy:3f9a1c",
            "localhost/project-sandbox-jj-e2e.lv6bl1-3ee8ea75:latest",
        ):
            with self.subTest(reference=reference):
                self.assertTrue(cleanup.is_e2e_image(reference))

    def test_leaves_real_project_images_alone(self) -> None:
        for reference in (
            REAL_IMAGE,
            "project-sandbox-project-sandbox-f1af2178:latest",
            "project-sandbox-myapp-0123abcd:latest",
            "project-sandbox-project-website-0123abcd:latest",
            "python:3.12-slim",
            "<none>:<none>",
        ):
            with self.subTest(reference=reference):
                self.assertFalse(cleanup.is_e2e_image(reference))


class ContainerMatchTests(TestCase):
    def test_session_container_needs_an_e2e_image(self) -> None:
        self.assertTrue(cleanup.is_e2e_container(_docker_entry(SESSION, E2E_IMAGE)))
        self.assertFalse(cleanup.is_e2e_container(_docker_entry(SESSION, REAL_IMAGE)))
        self.assertFalse(
            cleanup.is_e2e_container(_docker_entry("agentgateway", E2E_IMAGE))
        )

    def test_squid_fixture_needs_a_build_proxy_temp_mount(self) -> None:
        # A real IPL Squid must survive; only e2e-build-proxy.py's fixture goes.
        fixture = "/tmp/project-sandbox-build-proxy.ab12cd/squid/config/squid.conf"
        self.assertTrue(cleanup.is_e2e_container(_squid(fixture)))
        self.assertFalse(
            cleanup.is_e2e_container(
                _squid("/home/u/internet-proxy-locally/config/squid.conf")
            )
        )

    def test_reads_apple_container_entries(self) -> None:
        entry = {
            "configuration": {
                "id": SESSION,
                "image": {"reference": "docker.io/library/" + E2E_IMAGE},
                "labels": {},
                "mounts": [],
            }
        }
        self.assertTrue(cleanup.is_e2e_container(entry))


class ProcessTests(TestCase):
    def test_only_orphaned_socat_forwarders_are_selected(self) -> None:
        socat = "/usr/bin/socat TCP-LISTEN:11434,bind=172.17.0.1,reuseaddr,fork TCP:127.0.0.1:11434"
        table = {
            1: (0, "/sbin/init"),
            100: (1, socat),
            200: (
                1,
                "/home/u/project-sandbox/.venv/bin/python /home/u/.venv/bin/project-sandbox run",
            ),
            201: (200, socat),
            300: (1, "socat - TCP:example.com:80"),
        }
        self.assertEqual(cleanup.orphaned_forwarders(table), [100])

    def test_detects_running_e2e_scripts_but_not_itself(self) -> None:
        table = {
            10: (1, "bash scripts/run-e2e-tests.sh --runtime docker"),
            11: (1, "python /repo/scripts/e2e-build-proxy.py --runtime docker"),
            12: (1, "python scripts/e2e-cleanup.py --dry-run"),
            13: (1, "python scripts/e2e-reap-orphans.py --runtime docker"),
            14: (1, "vim scripts/e2e-cleanup.py"),
        }
        self.assertEqual(
            cleanup.active_e2e(table),
            [
                "bash scripts/run-e2e-tests.sh --runtime docker",
                "python /repo/scripts/e2e-build-proxy.py --runtime docker",
            ],
        )


class MainTests(TestCase):
    def _fake_run(self, calls, *, ps="", docker_inspect=None):
        def run(argv, **_kwargs):
            calls.append(argv)
            if argv[0] == "ps":
                out = ps
            elif argv[1] == "ps":
                out = f"{SESSION}\nagentgateway\n"
            elif argv[1] == "inspect":
                out = json.dumps(docker_inspect or [])
            elif argv[1] == "images":
                out = f"{E2E_IMAGE}\n{REAL_IMAGE}\n"
            else:
                out = ""
            return SimpleNamespace(returncode=0, stdout=out, stderr="")

        return run

    def _main(self, calls, argv, **kwargs):
        with (
            patch.object(
                cleanup.subprocess, "run", side_effect=self._fake_run(calls, **kwargs)
            ),
            patch.object(cleanup.shutil, "which", return_value="/usr/bin/x"),
        ):
            return cleanup.main(argv)

    def test_removes_containers_before_images(self) -> None:
        calls = []
        code = self._main(
            calls,
            ["--runtime", "docker"],
            docker_inspect=[_docker_entry(SESSION, E2E_IMAGE)],
        )
        self.assertEqual(code, 0)
        # Only e2e candidates are inspected.
        self.assertIn(["docker", "inspect", SESSION], calls)
        removals = [argv for argv in calls if "rm" in argv]
        self.assertEqual(
            removals,
            [
                ["docker", "rm", "--force", SESSION],
                ["docker", "image", "rm", E2E_IMAGE],
            ],
        )

    def test_dry_run_removes_nothing(self) -> None:
        calls = []
        with patch.object(cleanup.os, "kill") as kill:
            code = self._main(
                calls,
                ["--runtime", "docker", "--dry-run"],
                ps="100 1 socat TCP-LISTEN:1,bind=10.0.0.1,reuseaddr,fork TCP:127.0.0.1:1\n",
                docker_inspect=[_docker_entry(SESSION, E2E_IMAGE)],
            )
        self.assertEqual(code, 0)
        kill.assert_not_called()
        self.assertFalse([argv for argv in calls if "rm" in argv])

    def test_refuses_while_an_e2e_run_is_active(self) -> None:
        calls = []
        code = self._main(
            calls,
            ["--runtime", "docker"],
            ps="10 1 bash scripts/run-e2e-tests.sh\n",
        )
        self.assertEqual(code, 1)
        self.assertEqual(calls, [["ps", "-eo", "pid=,ppid=,args="]])
