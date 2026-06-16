#!/usr/bin/env bash
# End-to-end verification that --timeout tears down the container *and* its
# backing VM, not just the project-sandbox process.
#
# Why this exists: on timeout, session.py SIGTERM->SIGKILLs the whole
# `container run` process group so that `--rm` can clean the container up.
# Returning exit code 124 only proves the *host* process was killed; it does
# NOT prove the apple/container guest VM was actually reclaimed. This script
# closes that gap by diffing the runtime's running-container set before and
# after a deliberately-timed-out run and asserting nothing lingers.
#
# Requirements: a working container runtime on PATH with a running system.
# Defaults to apple/container (the runtime the TODO is about); pass
# --runtime docker|podman to exercise the same teardown contract elsewhere.
# Because the container must stay alive long enough to be killed mid-flight,
# this builds the project-sandbox image (no --no-build) and runs a sleeping
# bash agent.
#
# Usage:
#   scripts/verify-timeout-teardown.sh [--runtime NAME] [--timeout SECS]
#                                      [--sleep SECS] [--settle SECS]
#
#   --runtime NAME   apple-container (default) | docker | podman
#   --timeout SECS   --timeout passed to project-sandbox (default 5)
#   --sleep SECS     how long the in-container agent tries to sleep; must be
#                    comfortably larger than --timeout (default 120)
#   --settle SECS    how long to poll for the VM to disappear after the run
#                    returns, before declaring it lingering (default 30)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

RUNTIME="apple-container"
TIMEOUT=5
SLEEP=120
SETTLE=30

usage() { sed -n '2,28p' "$0"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --runtime) RUNTIME="${2:?--runtime needs a value}"; shift 2 ;;
    --timeout) TIMEOUT="${2:?--timeout needs a value}"; shift 2 ;;
    --sleep)   SLEEP="${2:?--sleep needs a value}"; shift 2 ;;
    --settle)  SETTLE="${2:?--settle needs a value}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 64 ;;
  esac
done

case "$RUNTIME" in
  apple-container) BIN="container" ;;
  docker)          BIN="docker" ;;
  podman)          BIN="podman" ;;
  *) echo "ERROR: unsupported --runtime '$RUNTIME'" >&2; exit 64 ;;
esac

if ! command -v "$BIN" >/dev/null 2>&1; then
  echo "ERROR: '$BIN' CLI not found on PATH (needed for --runtime $RUNTIME)." >&2
  exit 64
fi

if [ "$SLEEP" -le "$TIMEOUT" ]; then
  echo "ERROR: --sleep ($SLEEP) must be greater than --timeout ($TIMEOUT)," \
       "otherwise the container exits on its own before the timeout fires." >&2
  exit 64
fi

# List the IDs of currently-running containers, one per line, sorted.
# `-q`/`--quiet` is supported by docker, podman and apple/container and prints
# bare IDs with no header, which keeps this parser runtime-agnostic.
running_ids() {
  "$BIN" ls -q 2>/dev/null | sed '/^$/d' | sort -u
}

TMP_PROJECT="$(mktemp -d -t project-sandbox-timeout.XXXXXX)"
BEFORE_FILE="$(mktemp -t ps-timeout-before.XXXXXX)"
AFTER_FILE="$(mktemp -t ps-timeout-after.XXXXXX)"
cleanup() { rm -rf "$TMP_PROJECT" "$BEFORE_FILE" "$AFTER_FILE"; }
trap cleanup EXIT

cat > "$TMP_PROJECT/hello.py" <<'PY'
print("hello, sandbox!")
PY

echo "Runtime:      $RUNTIME ($BIN)"
echo "Test project: $TMP_PROJECT"
echo "Plan:         run a bash agent sleeping ${SLEEP}s with --timeout ${TIMEOUT}s,"
echo "              then confirm no container/VM survives within ${SETTLE}s."
echo

fail=0

cd "$ROOT"

echo "Snapshot of running containers BEFORE run:"
running_ids | tee "$BEFORE_FILE" | sed 's/^/  /' || true
[ -s "$BEFORE_FILE" ] || echo "  (none)"
echo

echo "Running timed-out sandbox..."
start=$(date +%s)
set +e
uv run project-sandbox \
  --runtime "$RUNTIME" \
  --agent bash \
  --prompt-text "sleep $SLEEP" \
  --timeout "$TIMEOUT" \
  --verbose \
  "$TMP_PROJECT" \
  python:3.12-slim
timeout_rc=$?
set -e
elapsed=$(( $(date +%s) - start ))
echo

if [ "$timeout_rc" = 124 ]; then
  echo "  ok    run returned 124 (timeout) after ${elapsed}s"
else
  echo "  BAD   run returned $timeout_rc (expected 124) after ${elapsed}s"
  ls -la "${TMP_PROJECT}"
  fail=1
fi

# session.py allows up to 30s between SIGTERM and SIGKILL, so the host process
# can legitimately take a moment to exit; the elapsed time should still be in
# the neighbourhood of --timeout plus that grace, not the full sleep.
if [ "$elapsed" -ge "$SLEEP" ]; then
  echo "  BAD   run took ${elapsed}s (>= sleep ${SLEEP}s): timeout did not interrupt it"
  fail=1
else
  echo "  ok    run was interrupted well before the ${SLEEP}s sleep elapsed"
fi
echo

# Poll for teardown: any container ID present now but not before is one this
# run created. --rm + the process-group kill should make it disappear; if it
# is still there after $SETTLE seconds, the VM is lingering.
echo "Polling up to ${SETTLE}s for the container/VM to be reclaimed..."
lingering=""
for _ in $(seq 1 "$SETTLE"); do
  running_ids > "$AFTER_FILE"
  lingering="$(comm -13 "$BEFORE_FILE" "$AFTER_FILE")"
  [ -z "$lingering" ] && break
  sleep 1
done

if [ -z "$lingering" ]; then
  echo "  ok    no new container/VM remains after the timeout"
else
  echo "  BAD   container(s)/VM(s) still running after ${SETTLE}s:"
  echo "$lingering" | sed 's/^/          /'
  echo "        Teardown is NOT clean — the run leaked a guest VM."
  echo "        Fix path (per TODO): give the run a known --name/id and"
  echo "        \`$BIN stop\`/\`$BIN kill\` it explicitly in the timeout path, e.g.:"
  echo "$lingering" | sed "s|^|          $BIN stop |"
  fail=1
fi
echo

if [ "$fail" = 0 ]; then
  echo "PASS — --timeout interrupts the run AND tears down the backing VM."
  exit 0
else
  echo "FAIL — see the BAD lines above."
  echo
  echo "Current running containers (for debugging):"
  "$BIN" ls 2>/dev/null | sed 's/^/  /' || true
  exit 1
fi
