"""Check the e2e orphan reaper without a container runtime."""

import importlib.util
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from project_sandbox import container_cli, observability

ROOT = Path(__file__).parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "e2e_reap_orphans", ROOT / "scripts/e2e-reap-orphans.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reaper = _load()


def _record(session_id: str, *, pid: int, runtime: str = "docker") -> dict:
    return {
        "session_id": session_id,
        "container_name": observability.container_name(session_id),
        "runtime": runtime,
        "status": "running",
        "pid": pid,
    }


class ReapOrphansTests(TestCase):
    def setUp(self) -> None:
        self.state = tempfile.TemporaryDirectory()
        self.addCleanup(self.state.cleanup)
        patcher = patch.dict(os.environ, {"XDG_STATE_HOME": self.state.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        observability.state_dir().mkdir(parents=True)

    def _write(self, record: dict) -> Path:
        path = observability.state_dir() / f"{record['session_id']}.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def test_stops_only_running_containers_of_dead_clis(self) -> None:
        # Regression: a SIGKILLed CLI left its container running into the next
        # e2e run. Containers of a live CLI, of another runtime, or that are no
        # longer running must be left alone.
        dead = 2**22 + 12345  # above the default pid_max, so never alive
        orphan = self._write(_record("20261027T000001-aaaaaaaaaaaa", pid=dead))
        self._write(_record("20261027T000002-bbbbbbbbbbbb", pid=os.getpid()))
        self._write(_record("20261027T000003-cccccccccccc", pid=dead, runtime="podman"))
        self._write(_record("20261027T000004-dddddddddddd", pid=dead))
        running = {
            observability.container_name(f"20261027T00000{i}-{c * 12}")
            for i, c in ((1, "a"), (2, "b"), (3, "c"))
        }
        calls = []

        def fake_run(argv, **_kwargs):
            calls.append(argv)
            if argv[1] == "ps":
                return SimpleNamespace(
                    returncode=0, stdout="\n".join(running) + "\n", stderr=""
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch.object(reaper.subprocess, "run", side_effect=fake_run),
            patch.object(
                reaper.container_cli,
                "select_runtime",
                return_value=container_cli.DOCKER,
            ),
        ):
            self.assertEqual(reaper.main(["--runtime", "docker"]), 0)

        stops = [argv for argv in calls if argv[1] == "stop"]
        self.assertEqual(
            stops,
            [
                container_cli.build_stop_argv(
                    container_cli.DOCKER,
                    "project-sandbox-20261027t000001-aaaaaaaaaaaa",
                )
            ],
        )
        self.assertEqual(json.loads(orphan.read_text())["status"], "orphaned")

    def test_runtime_listing_failure_stops_nothing(self) -> None:
        self._write(_record("20261027T000001-aaaaaaaaaaaa", pid=2**22 + 12345))
        with (
            patch.object(
                reaper.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=1, stdout="", stderr="down"),
            ) as run,
            patch.object(
                reaper.container_cli,
                "select_runtime",
                return_value=container_cli.DOCKER,
            ),
        ):
            self.assertEqual(reaper.main(["--runtime", "docker"]), 0)
        run.assert_called_once()
