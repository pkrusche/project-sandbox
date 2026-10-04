"""Container test discovery and execution must require an explicit opt-in."""

import importlib.util
import os
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.mark.parametrize(
    ("filename", "class_name"),
    [
        ("test_worktree.py", "GitWorktreeDockerEndToEndTests"),
        ("test_jj_workspace.py", "JjWorkspaceDockerEndToEndTests"),
    ],
)
def test_container_tests_are_opt_in(filename, class_name):
    real_which = shutil.which

    def guarded_which(command):
        if command in ("docker", "podman"):
            raise AssertionError("runtime discovery during collection")
        return real_which(command)

    def load_module():
        spec = importlib.util.spec_from_file_location(
            "container_test_opt_in_fixture", Path(__file__).with_name(filename)
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    with (
        patch.dict(os.environ, {"PROJECT_SANDBOX_CONTAINER_TESTS": ""}),
        patch("shutil.which", side_effect=guarded_which),
        patch(
            "subprocess.run", side_effect=AssertionError("subprocess during collection")
        ),
    ):
        module = load_module()
        case = getattr(module, class_name)
        with patch.object(module, "_find_docker") as discover:
            result = unittest.TestResult()
            unittest.defaultTestLoader.loadTestsFromTestCase(case).run(result)
        assert result.testsRun > 0
        assert len(result.skipped) == result.testsRun
        assert not result.errors
        assert not result.failures
        discover.assert_not_called()

    with (
        patch.dict(os.environ, {"PROJECT_SANDBOX_CONTAINER_TESTS": "1"}),
        patch("shutil.which", side_effect=guarded_which),
        patch(
            "subprocess.run", side_effect=AssertionError("subprocess during collection")
        ),
    ):
        module = load_module()
    case = getattr(module, class_name)
    assert not getattr(case, "__unittest_skip__", False)
    with patch.object(module, "_find_docker", return_value="/test/docker") as discover:
        # The jj prerequisite is independent of container runtime discovery.
        with patch.object(module, "JJ", "/test/jj", create=True):
            case.setUpClass()
    discover.assert_called_once_with()
    assert case.docker == "/test/docker"
