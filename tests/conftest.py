"""Shared pytest fixtures.

Hermeticity guard: the host token refresh shells out to the real ``claude`` /
``codex`` CLI, which makes network calls and can rotate the developer's real
credentials. No test may trigger that, so it is neutralised suite-wide. The
dedicated ``test_oauth_refresh`` module exercises the real implementation (with
``subprocess`` mocked) and is excluded.
"""

import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import config_agents, container_cli, mount_storage, oauth_refresh


@pytest.fixture(autouse=True)
def _mock_image_selection_for_cli_tests(request):
    # These integration modules already replace builds/runs. Do not inspect a
    # real host image store or create aliases for their simulated builds.
    # test_build_concurrency and test_image_identity exercise the real helpers.
    if request.node.module.__name__ not in {
        "test_cli",
        "test_internet_proxy_relay",
        "test_vcs_access",
        "test_internet_proxy",
        "test_ca_certificates",
        "test_build_network",
        "test_generated_files",
        "test_implicit_mounts",
        "test_remote_proxy_cli",
        "test_agent_proxy",
        "test_build_proxy_forwarder",
        "test_session_inputs",
    }:
        yield
        return

    @contextmanager
    def pin(_runtime, _reference, identity):
        yield identity

    with (
        patch.object(
            container_cli, "image_identity", return_value="sha256:" + "a" * 64
        ),
        patch.object(container_cli, "pinned_image", side_effect=pin),
    ):
        yield


@pytest.fixture(autouse=True)
def _private_mount_storage(tmp_path):
    # Keep persistent history and generated helper installations out of the
    # developer's real state directory, including in CLI/render integration tests.
    with (
        patch.object(mount_storage, "storage_root", return_value=tmp_path / "mounts"),
        patch.object(config_agents, "CREDENTIALS_ROOT", tmp_path / "credentials"),
    ):
        yield


@pytest.fixture(autouse=True)
def _no_live_host_token_refresh(request):
    if request.node.module.__name__ == "test_oauth_refresh":
        yield
        return
    with patch.object(oauth_refresh, "refresh_host_token"):
        yield
