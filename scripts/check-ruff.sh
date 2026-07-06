#!/usr/bin/env bash
# Verify that Python sources are formatted and pass Ruff linting.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

uv run ruff format --check .
uv run ruff check .
