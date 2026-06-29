#!/usr/bin/env bash
# Run all project-sandbox end-to-end tests.
#
# Sequentially executes:
#   1. e2e-test.sh          — artifact-generation smoke test (no container needed)
#   2. e2e-git-workflow.sh  — git rebase/merge/nothing workflows
#   3. e2e-jj-workflow.sh   — jj rebase/merge/nothing workflows (skipped if jj not on PATH)
#
# Usage:
#   scripts/run-e2e-tests.sh [--runtime auto|apple-container|docker|podman]
#                            [--base-image IMAGE] [--no-build] [--keep]
#
#   --runtime NAME   container runtime forwarded to workflow scripts (default: auto)
#   --base-image IMG base image forwarded to workflow scripts (default: python:3.12-slim)
#   --no-build       forward --no-build to workflow scripts
#   --keep           keep temporary directories on failure for debugging
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

RUNTIME="auto"
BASE_IMAGE="python:3.12-slim"
NO_BUILD=0
KEEP=0

usage() { sed -n '2,16p' "$0"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --runtime)    RUNTIME="${2:?--runtime needs a value}"; shift 2 ;;
    --base-image) BASE_IMAGE="${2:?--base-image needs a value}"; shift 2 ;;
    --no-build)   NO_BUILD=1; shift ;;
    --keep)       KEEP=1; shift ;;
    -h|--help)    usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 64 ;;
  esac
done

overall_fail=0
run_suite() {
  local name="$1"; shift
  echo "========================================"
  echo "Suite: $name"
  echo "========================================"
  if "$@"; then
    echo "Suite PASS: $name"
  else
    echo "Suite FAIL: $name"
    overall_fail=1
  fi
  echo
}

# Build common flags for the workflow scripts
WORKFLOW_ARGS=(--runtime "$RUNTIME" --base-image "$BASE_IMAGE")
[ "$NO_BUILD" = 1 ] && WORKFLOW_ARGS+=(--no-build)
[ "$KEEP"     = 1 ] && WORKFLOW_ARGS+=(--keep)

# 1. Basic artifact-generation smoke test (no container required)
run_suite "smoke" "$ROOT/scripts/e2e-test.sh"

# 2. Git workflow: rebase / merge / nothing
run_suite "git-workflow" "$ROOT/scripts/e2e-git-workflow.sh" "${WORKFLOW_ARGS[@]}"

# 3. Jj workflow: rebase / merge / nothing (only if jj is on PATH)
if command -v jj >/dev/null 2>&1; then
  run_suite "jj-workflow" "$ROOT/scripts/e2e-jj-workflow.sh" "${WORKFLOW_ARGS[@]}"
else
  echo "========================================"
  echo "Suite: jj-workflow  (SKIPPED — jj not found on PATH)"
  echo "========================================"
  echo
fi

if [ "$overall_fail" = 0 ]; then
  echo "All e2e suites PASSED."
  exit 0
else
  echo "One or more e2e suites FAILED."
  exit 1
fi
