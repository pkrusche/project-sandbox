"""Check interpreter selection in the E2E fixtures without a container runtime."""

import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]


class PythonUvFixtureTests(unittest.TestCase):
    def test_scenarios_explicitly_select_interpreter_source(self):
        script = (ROOT / "scripts/e2e-python-uv.sh").read_text()
        function = script.split("make_project() {", 1)[1].split("\n}\n", 1)[0]
        # Exercise the actual scenario calls and fixture writer. Stub only the
        # network-dependent lock operation; no runtime or Python download runs.
        calls = "\n".join(
            line for line in script.splitlines() if line.startswith("make_project ")
        )
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(
                [
                    "bash", "-euc",
                    'TMP_ROOT="$1"\nBASE_PYTHON=3.11\nMANAGED_PYTHON=3.13\n'
                    'uv() { test "$1" = lock; }\n'
                    "make_project() {" + function + "\n}\n" + calls,
                    "fixture-test", tmp,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            for scenario, expected in (
                ("managed-python", "only-managed"),
                ("base-python", "only-system"),
            ):
                with self.subTest(scenario=scenario):
                    config = tomllib.loads(
                        (Path(tmp) / scenario / "pyproject.toml").read_text()
                    )
                    self.assertEqual(
                        config["tool"]["uv"]["python-preference"], expected
                    )
