"""Shared pytest fixtures.

Hermeticity guard: the host token refresh shells out to the real ``claude`` /
``codex`` CLI, which makes network calls and can rotate the developer's real
credentials. No test may trigger that, so it is neutralised suite-wide. The
dedicated ``test_oauth_refresh`` module exercises the real implementation (with
``subprocess`` mocked) and is excluded.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import oauth_refresh


@pytest.fixture(autouse=True)
def _no_live_host_token_refresh(request):
    if request.node.module.__name__ == "test_oauth_refresh":
        yield
        return
    with patch.object(oauth_refresh, "refresh_host_token"):
        yield
