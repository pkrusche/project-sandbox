#!/usr/bin/env bash
# End-to-end Dockerfile tamper-detection verification for project-sandbox.
#
# Records a Dockerfile checksum baseline, then simulates an agent tamper by
# modifying the Dockerfile, and verifies:
#   1. Unsupervised runs (--prompt-text) abort with rc=1 before launching a
#      container, leaving the baseline unchanged so the tamper remains detectable.
#   2. A --no-build run with --no-verify-dockerfile does not advance the baseline.
#   3. --dry-run prints the warning but never blocks, aborts, or writes the baseline.
#   4. --no-verify-dockerfile suppresses the abort and the session runs to completion.
#      (This check requires a container runtime; use --with-container to enable it.)
#
# Checks 1–3 exercise the CLI exit path that fires before any container is launched
# and so run without a container runtime on any host that has uv.
#
# Usage:
#   scripts/e2e-dockerfile-tamper.sh [--with-container]
#                                    [--runtime auto|apple-container|docker|podman]
#                                    [--base-image IMAGE] [--keep]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

RUNTIME="auto"
BASE_IMAGE="python:3.12-slim"
WITH_CONTAINER=0
KEEP=0

usage() { sed -n '2,19p' "$0"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --with-container) WITH_CONTAINER=1; shift ;;
    --runtime) RUNTIME="${2:?--runtime needs a value}"; shift 2 ;;
    --base-image) BASE_IMAGE="${2:?--base-image needs a value}"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 64 ;;
  esac
done

command -v uv >/dev/null 2>&1 || { echo "ERROR: uv not found on PATH." >&2; exit 64; }

if [ "$WITH_CONTAINER" = 1 ]; then
  case "$RUNTIME" in
    auto) ;;
    apple-container) command -v container >/dev/null 2>&1 || { echo "ERROR: container CLI not found." >&2; exit 64; } ;;
    docker|podman) command -v "$RUNTIME" >/dev/null 2>&1 || { echo "ERROR: $RUNTIME CLI not found." >&2; exit 64; } ;;
    *) echo "ERROR: unsupported --runtime '$RUNTIME'" >&2; exit 64 ;;
  esac

  REQUESTED_RUNTIME="$RUNTIME"
  if [ "$RUNTIME" = auto ]; then
    if [ "$(uname -s)" = Darwin ] && command -v container >/dev/null 2>&1; then
      RUNTIME="apple-container"
    elif command -v docker >/dev/null 2>&1; then
      RUNTIME="docker"
    elif command -v podman >/dev/null 2>&1; then
      RUNTIME="podman"
    else
      echo "ERROR: --with-container requires a supported container runtime on PATH." >&2
      exit 64
    fi
  fi
fi

# Apple's `container` build VM cannot read the macOS per-user temp dir
# ($TMPDIR, /var/folders/...), so keep the throwaway project under the repo's
# gitignored .project-sandbox/ tree (same reason as the other e2e scripts).
TMP_BASE="$ROOT/.project-sandbox/e2e"
mkdir -p "$TMP_BASE"
TMP_PROJECT="$(mktemp -d "$TMP_BASE/dockerfile-tamper-e2e.XXXXXX")"
cleanup() {
  if [ "$KEEP" = 0 ]; then
    rm -rf "$TMP_PROJECT"
  fi
}
trap cleanup EXIT

fail=0

ok()  { echo "  ok    $*"; }
bad() { echo "  BAD   $*"; fail=1; }

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Record the current checksum baseline via the project's own module so the
# state file format stays in sync with the CLI.
record_baseline() {
  uv run python -c "
from pathlib import Path
from project_sandbox import dockerfile_checksum
dockerfile_checksum.record(Path('$1'), [Path('$2')])
"
}

# Returns 0 when the given Dockerfile is flagged as changed.
tamper_detectable() {
  uv run python -c "
import sys
from pathlib import Path
from project_sandbox import dockerfile_checksum
warnings = dockerfile_checksum.changed_warnings(Path('$1'), [Path('$2')])
sys.exit(0 if warnings else 1)
"
}

# ---------------------------------------------------------------------------
# Project setup
# ---------------------------------------------------------------------------

CONTEXT_DIR="$TMP_PROJECT/.project-sandbox"
DOCKERFILE="$TMP_PROJECT/Dockerfile"

echo "Test project: $TMP_PROJECT"
echo "Configuration: with_container=$WITH_CONTAINER base_image=$BASE_IMAGE"
echo

printf "FROM debian:bookworm-slim\nRUN echo original\n" > "$DOCKERFILE"

mkdir -p "$CONTEXT_DIR"
record_baseline "$CONTEXT_DIR" "$DOCKERFILE"
ok "baseline recorded for: $DOCKERFILE"

# Simulate an agent modifying the Dockerfile during a session.
printf "FROM debian:bookworm-slim\nRUN echo pwned\n" > "$DOCKERFILE"
ok "Dockerfile mutated (tamper simulated)"

echo

# ---------------------------------------------------------------------------
# Check 1: unsupervised run aborts on tamper (rc=1, no container launched)
# ---------------------------------------------------------------------------

echo "Check 1: unsupervised run aborts when Dockerfile is changed"

set +e
(cd "$ROOT" && uv run project-sandbox \
  --no-build \
  --agent bash \
  --prompt-text "echo should-not-reach" \
  --no-firewall \
  --dockerfile "$DOCKERFILE" \
  "$TMP_PROJECT") 2>/dev/null
abort_rc=$?
set -e

if [ "$abort_rc" = 1 ]; then
  ok "unsupervised run exited with rc=1 on tampered Dockerfile"
else
  bad "expected rc=1, got rc=$abort_rc"
fi

# ---------------------------------------------------------------------------
# Check 2: baseline unchanged after aborted run — tamper still detectable
# ---------------------------------------------------------------------------

echo
echo "Check 2: baseline unchanged after aborted run"

if tamper_detectable "$CONTEXT_DIR" "$DOCKERFILE"; then
  ok "tamper is still detectable after aborted run"
else
  bad "tamper cleared after abort — baseline must not be advanced on abort"
fi

# ---------------------------------------------------------------------------
# Check 3: --dry-run prints warning but exits 0 and does not write baseline
# ---------------------------------------------------------------------------

echo
echo "Check 3: --dry-run prints warning, exits 0, does not update baseline"

set +e
dry_out=$(cd "$ROOT" && uv run project-sandbox \
  --dry-run \
  --no-build \
  --agent bash \
  --prompt-text "echo ok" \
  --no-firewall \
  --dockerfile "$DOCKERFILE" \
  "$TMP_PROJECT" 2>/dev/null)
dry_rc=$?
set -e

if [ "$dry_rc" = 0 ]; then
  ok "--dry-run exited with rc=0"
else
  bad "--dry-run exited with rc=$dry_rc (expected 0)"
fi

if echo "$dry_out" | grep -qF "changed since it was last built"; then
  ok "--dry-run printed tamper warning"
else
  bad "--dry-run did not print tamper warning"
fi

if tamper_detectable "$CONTEXT_DIR" "$DOCKERFILE"; then
  ok "tamper still detectable after --dry-run (baseline not mutated)"
else
  bad "--dry-run advanced the baseline — dry-run must never write state"
fi

# ---------------------------------------------------------------------------
# Check 4: --no-build with --no-verify-dockerfile does not advance baseline
# ---------------------------------------------------------------------------

echo
echo "Check 4: --no-build run with --no-verify-dockerfile does not advance baseline"

# The session will fail trying to launch a container (no runtime available in
# the default no-container path), but that is irrelevant: the baseline must not
# be updated regardless of whether the container step succeeds.
set +e
(cd "$ROOT" && uv run project-sandbox \
  --no-build \
  --agent bash \
  --prompt-text "echo ok" \
  --no-firewall \
  --no-verify-dockerfile \
  --dockerfile "$DOCKERFILE" \
  "$TMP_PROJECT") 2>/dev/null
# Any rc is acceptable here; we only care about the baseline.
set -e

if tamper_detectable "$CONTEXT_DIR" "$DOCKERFILE"; then
  ok "tamper still detectable after --no-build --no-verify-dockerfile run"
else
  bad "--no-build run advanced the baseline — record must only fire after a real build"
fi

# ---------------------------------------------------------------------------
# Check 5 (--with-container): session proceeds with --no-verify-dockerfile
# ---------------------------------------------------------------------------

if [ "$WITH_CONTAINER" = 1 ]; then
  echo
  echo "Check 5: --no-verify-dockerfile allows session to run to completion"

  set +e
  (cd "$ROOT" && uv run project-sandbox \
    --runtime "$RUNTIME" \
    --agent bash \
    --prompt-text "printf tamper-check-skipped-ok" \
    --no-firewall \
    --no-verify-dockerfile \
    --timeout 30 \
    --dockerfile "$DOCKERFILE" \
    "$TMP_PROJECT")
  session_rc=$?
  set -e

  if [ "$session_rc" = 0 ]; then
    ok "--no-verify-dockerfile session completed (rc=0)"
  else
    bad "--no-verify-dockerfile session failed with rc=$session_rc"
  fi
fi

# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

echo
if [ "$fail" = 0 ]; then
  echo "PASS"
  if [ "$KEEP" = 1 ]; then
    cat <<EOF

Test project kept for inspection:
  $TMP_PROJECT

Remove when done:
  rm -rf $TMP_PROJECT
EOF
  fi
  exit 0
fi

if [ "$KEEP" = 1 ]; then
  echo "FAIL - test project kept for debugging: $TMP_PROJECT"
else
  echo "FAIL - test project will be removed (use --keep to retain it): $TMP_PROJECT"
fi
exit 1
