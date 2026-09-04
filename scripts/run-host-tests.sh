#!/usr/bin/env bash
# Run the complete project-sandbox test suite from the host.
#
# This is the single entry point for local verification. It installs the locked
# development environment, checks Python syntax/style/types, runs all unit
# tests, and dispatches every end-to-end suite supported by the selected
# runtime.
#
# Usage:
#   scripts/run-host-tests.sh [run-e2e-tests options]
#
# Common examples:
#   scripts/run-host-tests.sh
#   scripts/run-host-tests.sh --runtime docker
#   scripts/run-host-tests.sh --runtime apple-container --with-agent-proxy
#
# `--with-agent-proxy` makes two real, billable LLM requests. Ollama tests run
# automatically only when Ollama is reachable on host loopback.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  sed -n '2,17p' "$0"
  echo
  "$ROOT/scripts/run-e2e-tests.sh" --help
  exit 0
fi

command -v uv >/dev/null 2>&1 || {
  echo "ERROR: uv not found on PATH." >&2
  exit 64
}

echo "==> Syncing locked development environment"
uv sync --locked

echo "==> Compiling Python sources and tests"
uv run python -m compileall -q src tests scripts

echo "==> Checking formatting and lint"
"$ROOT/scripts/check-ruff.sh"

echo "==> Type checking"
uv run ty check

echo "==> Running unit tests"
uv run pytest -q

echo "==> Running end-to-end suites"
"$ROOT/scripts/run-e2e-tests.sh" "$@"
