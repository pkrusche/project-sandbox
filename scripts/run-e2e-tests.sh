#!/usr/bin/env bash
# Run all project-sandbox end-to-end tests available on this host.
#
# Sequentially executes:
#   1. smoke                    — artifact-generation smoke test (no container needed)
#   2. env-injection            — bash-agent env/API-key forwarding
#   3. git-workflow             — git rebase/merge/nothing workflows
#   4. jj-workflow              — jj rebase/merge/nothing workflows (skipped if jj not on PATH)
#   5. dockerfile-tamper        — Dockerfile integrity and override behavior
#   6. timeout-teardown         — container/VM cleanup after timeout
#   7. pi-ollama                — Pi + Ollama networking/config
#   8. internet-proxy-smoke     — availability-gated routing/bypass smoke test
#   9. internet-proxy-isolation — routing/bypass/failure audit (explicit opt-in)
#  10. agent-proxy-isolation    — gateway-only network/credential audit (explicit opt-in)
#  11. agent-proxy              — real Pi/OpenCode gateway calls (explicit opt-in)
#
# The console shows one progress line per suite. Each suite's output is captured
# in its own temp log file; a failing suite's log is kept and its path reported,
# a passing suite's log is deleted unless --keep is given.
#
# Usage:
#   scripts/run-e2e-tests.sh [--runtime chroot|auto|apple-container|docker|podman]
#                            [--base-image IMAGE] [--no-build] [--keep]
#                            [--only SUITE] [--list]
#                            [--with-agent-proxy] [--with-internet-proxy]
#
#   --runtime NAME   runtime forwarded to workflow scripts (default: chroot on Linux, auto otherwise)
#   --base-image IMG base image forwarded to workflow scripts (default: python:3.12-slim)
#   --no-build       forward --no-build to workflow scripts
#   --keep           keep temporary directories on failure for debugging, and
#                    keep the captured log of every suite, not just failures
#   --only SUITE     run just one suite by name (default: run every available suite).
#                    Naming an opt-in suite is itself the opt-in, so --only agent-proxy,
#                    --only agent-proxy-isolation, and --only internet-proxy-isolation
#                    do not additionally need --with-agent-proxy/--with-internet-proxy.
#   --list           print the suite names accepted by --only and exit
#   --with-agent-proxy
#                    run proxy isolation plus the billable agent checker
#   --with-internet-proxy
#                    run the destructive two-service Internet isolation audit;
#                    also requires --blocked-url, --internet-proxy-dir, and
#                    --agentgateway-dir
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

SUITES=(
  smoke
  env-injection
  git-workflow
  jj-workflow
  dockerfile-tamper
  timeout-teardown
  pi-ollama
  internet-proxy-smoke
  internet-proxy-isolation
  agent-proxy-isolation
  agent-proxy
)

if [ "$(uname -s)" = Linux ]; then
  RUNTIME="chroot"
else
  RUNTIME="auto"
fi
BASE_IMAGE="python:3.12-slim"
NO_BUILD=0
KEEP=0
ONLY=""
WITH_AGENT_PROXY=0
WITH_INTERNET_PROXY=0
BLOCKED_URL=""
INTERNET_PROXY_DIR=""
AGENTGATEWAY_DIR=""

# Print the leading comment block (everything after the shebang up to the first
# non-comment line) so the header stays the single source of usage truth.
usage() { sed -n '2,${/^#/!q;p;}' "$0"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --runtime)    RUNTIME="${2:?--runtime needs a value}"; shift 2 ;;
    --base-image) BASE_IMAGE="${2:?--base-image needs a value}"; shift 2 ;;
    --no-build)   NO_BUILD=1; shift ;;
    --keep)       KEEP=1; shift ;;
    --only)       ONLY="${2:?--only needs a value}"; shift 2 ;;
    --list)       printf '%s\n' "${SUITES[@]}"; exit 0 ;;
    --with-agent-proxy) WITH_AGENT_PROXY=1; shift ;;
    --with-internet-proxy) WITH_INTERNET_PROXY=1; shift ;;
    --blocked-url) BLOCKED_URL="${2:?--blocked-url needs a value}"; shift 2 ;;
    --internet-proxy-dir) INTERNET_PROXY_DIR="${2:?--internet-proxy-dir needs a value}"; shift 2 ;;
    --agentgateway-dir) AGENTGATEWAY_DIR="${2:?--agentgateway-dir needs a value}"; shift 2 ;;
    -h|--help)    usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 64 ;;
  esac
done

if [ -n "$ONLY" ]; then
  known=0
  for suite in "${SUITES[@]}"; do
    [ "$suite" = "$ONLY" ] && known=1
  done
  if [ "$known" = 0 ]; then
    echo "Unknown suite: $ONLY" >&2
    echo "Available suites: ${SUITES[*]}" >&2
    exit 64
  fi
  # Selecting an opt-in suite by name is an explicit request to run it.
  case "$ONLY" in
    agent-proxy|agent-proxy-isolation) WITH_AGENT_PROXY=1 ;;
    internet-proxy-isolation) WITH_INTERNET_PROXY=1 ;;
  esac
fi

# True when the suite should run in this invocation: either no --only filter was
# given, or this is the selected suite.
selected() { [ -z "$ONLY" ] || [ "$ONLY" = "$1" ]; }

overall_fail=0
# Parallel arrays holding one entry per suite considered by this invocation, in
# execution order, for the closing summary. Bash 3.2 (stock macOS) has no
# associative arrays, so keep status/name/detail aligned by index.
RESULT_STATUS=()
RESULT_NAME=()
RESULT_DETAIL=()

record_result() {
  RESULT_STATUS+=("$1")
  RESULT_NAME+=("$2")
  RESULT_DETAIL+=("${3:-}")
}

# Suite output is captured per suite; the console only carries progress lines.
# A passing suite's log is deleted unless --keep was given; a failing suite's
# log is always retained so the failure can be inspected afterwards.
LOG_TMP_BASE="${TMPDIR:-/tmp}"
LOG_DIR="$(mktemp -d "${LOG_TMP_BASE%/}/project-sandbox-e2e-logs.XXXXXX")"
LOGS_KEPT=0

if [ -n "$ONLY" ]; then
  SUITE_TOTAL=1
else
  SUITE_TOTAL=${#SUITES[@]}
fi
SUITE_INDEX=0

# Print the leading half of a progress line; the outcome completes it.
suite_progress() {
  SUITE_INDEX=$((SUITE_INDEX + 1))
  printf '[%d/%d] %-26s ' "$SUITE_INDEX" "$SUITE_TOTAL" "$1"
}

run_suite() {
  local name="$1"; shift
  local log="$LOG_DIR/$name.log"
  local start=$SECONDS
  suite_progress "$name"
  if "$@" >"$log" 2>&1; then
    printf 'PASS (%ds)\n' "$((SECONDS - start))"
    if [ "$KEEP" = 1 ]; then
      record_result PASS "$name" "log: $log"
      LOGS_KEPT=1
    else
      record_result PASS "$name"
      rm -f "$log"
    fi
  else
    printf 'FAIL (%ds)  log: %s\n' "$((SECONDS - start))" "$log"
    tail -n 20 "$log" | sed 's/^/       | /'
    record_result FAIL "$name" "log: $log"
    LOGS_KEPT=1
    overall_fail=1
  fi
}

skip_suite() {
  suite_progress "$1"
  printf 'SKIP (%s)\n' "$2"
  record_result SKIP "$1" "$2"
}

# Report a suite that cannot even start because this invocation is missing a
# prerequisite it explicitly asked for. That is a failure, not a skip.
fail_suite() {
  suite_progress "$1"
  printf 'FAIL (%s)\n' "$2"
  record_result FAIL "$1" "$2"
  overall_fail=1
}

# Build common flags for the workflow scripts
WORKFLOW_ARGS=(--runtime "$RUNTIME" --base-image "$BASE_IMAGE")
[ "$NO_BUILD" = 1 ] && WORKFLOW_ARGS+=(--no-build)
[ "$KEEP"     = 1 ] && WORKFLOW_ARGS+=(--keep)

# 1. Basic artifact-generation smoke test (no container required)
if selected smoke; then
  run_suite "smoke" "$ROOT/scripts/e2e-test.sh"
fi

# 2. Env/API-key forwarding via headless bash-agent
if selected env-injection; then
  run_suite "env-injection" "$ROOT/scripts/e2e-env-injection.sh" "${WORKFLOW_ARGS[@]}"
fi

# 3. Git workflow: rebase / merge / nothing
if selected git-workflow; then
  run_suite "git-workflow" "$ROOT/scripts/e2e-git-workflow.sh" "${WORKFLOW_ARGS[@]}"
fi

# 4. Jj workflow: rebase / merge / nothing (only if jj is on PATH)
if selected jj-workflow; then
  if command -v jj >/dev/null 2>&1; then
    run_suite "jj-workflow" "$ROOT/scripts/e2e-jj-workflow.sh" "${WORKFLOW_ARGS[@]}"
  else
    skip_suite "jj-workflow" "jj not found on PATH"
  fi
fi

# Resolve a concrete runtime for checks that require a real container and do
# not accept chroot/auto. Selecting chroot deliberately keeps the aggregate
# suite host-only even if Docker or Podman happens to be installed.
CONTAINER_RUNTIME=""
case "$RUNTIME" in
  apple-container|docker|podman) CONTAINER_RUNTIME="$RUNTIME" ;;
  auto)
    if [ "$(uname -s)" = Darwin ] && command -v container >/dev/null 2>&1; then
      CONTAINER_RUNTIME="apple-container"
    elif command -v docker >/dev/null 2>&1; then
      CONTAINER_RUNTIME="docker"
    elif command -v podman >/dev/null 2>&1; then
      CONTAINER_RUNTIME="podman"
    fi
    ;;
esac

# 5. Dockerfile tamper detection always runs its portable host checks. Add its
# real-container override check when this invocation permits a build.
if selected dockerfile-tamper; then
  TAMPER_ARGS=()
  [ "$KEEP" = 1 ] && TAMPER_ARGS+=(--keep)
  if [ -n "$CONTAINER_RUNTIME" ] && [ "$NO_BUILD" = 0 ]; then
    TAMPER_ARGS+=(--with-container --runtime "$CONTAINER_RUNTIME" --base-image "$BASE_IMAGE")
  fi
  run_suite "dockerfile-tamper" "$ROOT/scripts/e2e-dockerfile-tamper.sh" "${TAMPER_ARGS[@]}"
fi

# 6. Timeout teardown necessarily builds and kills a real container. Skip it
# for chroot, unavailable auto runtimes, and explicit --no-build runs.
if selected timeout-teardown; then
  if [ -n "$CONTAINER_RUNTIME" ] && [ "$NO_BUILD" = 0 ]; then
    run_suite "timeout-teardown" \
      "$ROOT/scripts/verify-timeout-teardown.sh" --runtime "$CONTAINER_RUNTIME"
  else
    skip_suite "timeout-teardown" "select a container runtime without --no-build"
  fi
fi

# 7. Pi + Ollama: networking and baked config (only if Ollama is reachable;
# chroot cannot run --agent pi, so fall back to auto runtime detection)
if selected pi-ollama; then
  if command -v curl >/dev/null 2>&1 && curl -sf --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    PI_OLLAMA_RUNTIME="$RUNTIME"
    [ "$PI_OLLAMA_RUNTIME" = chroot ] && PI_OLLAMA_RUNTIME=auto
    PI_OLLAMA_ARGS=(--runtime "$PI_OLLAMA_RUNTIME" --base-image "$BASE_IMAGE")
    [ "$NO_BUILD" = 1 ] && PI_OLLAMA_ARGS+=(--no-build)
    [ "$KEEP"     = 1 ] && PI_OLLAMA_ARGS+=(--keep)
    run_suite "pi-ollama" "$ROOT/scripts/e2e-pi-ollama.sh" "${PI_OLLAMA_ARGS[@]}"
  else
    skip_suite "pi-ollama" "Ollama not reachable on 127.0.0.1:11434"
  fi
fi

# 8. Non-destructive Internet-proxy smoke test. The script itself detects an
# absent listener and reports a successful skip before creating anything.
if selected internet-proxy-smoke; then
  if [ -n "$CONTAINER_RUNTIME" ]; then
    INTERNET_SMOKE_ARGS=(--runtime "$CONTAINER_RUNTIME" --base-image "$BASE_IMAGE")
    [ "$NO_BUILD" = 1 ] && INTERNET_SMOKE_ARGS+=(--no-build)
    [ "$KEEP" = 1 ] && INTERNET_SMOKE_ARGS+=(--keep)
    run_suite "internet-proxy-smoke" \
      uv run python "$ROOT/scripts/e2e-internet-proxy-smoke.py" "${INTERNET_SMOKE_ARGS[@]}"
  else
    skip_suite "internet-proxy-smoke" "select a real container runtime"
  fi
fi

# 9. The combined Internet-proxy audit deliberately controls both external
# services and makes real AI requests. Never availability-gate this destructive test.
if selected internet-proxy-isolation; then
  if [ "$WITH_INTERNET_PROXY" = 1 ]; then
    if [ -z "$CONTAINER_RUNTIME" ] || [ -z "$BLOCKED_URL" ] || [ -z "$INTERNET_PROXY_DIR" ] || [ -z "$AGENTGATEWAY_DIR" ]; then
      fail_suite "internet-proxy-isolation" \
        "requires a real runtime, --blocked-url, --internet-proxy-dir, and --agentgateway-dir"
    else
      INTERNET_AUDIT_ARGS=(
        --runtime "$CONTAINER_RUNTIME" --base-image "$BASE_IMAGE"
        --blocked-url "$BLOCKED_URL" --internet-proxy-dir "$INTERNET_PROXY_DIR"
        --agentgateway-dir "$AGENTGATEWAY_DIR"
      )
      [ "$NO_BUILD" = 1 ] && INTERNET_AUDIT_ARGS+=(--no-build)
      [ "$KEEP" = 1 ] && INTERNET_AUDIT_ARGS+=(--keep)
      run_suite "internet-proxy-isolation" \
        uv run python "$ROOT/scripts/e2e-internet-proxy-isolation.py" "${INTERNET_AUDIT_ARGS[@]}"
    fi
  else
    skip_suite "internet-proxy-isolation" "pass --with-internet-proxy; destructive and billable"
  fi
fi

# 10–11. Gateway-only isolation is non-billable, followed by two real requests.
# Never trigger either merely because a listener happens to be present.
if [ "$WITH_AGENT_PROXY" = 1 ]; then
  if selected agent-proxy-isolation; then
    if [ -n "$CONTAINER_RUNTIME" ]; then
      PROXY_ISOLATION_ARGS=(--runtime "$CONTAINER_RUNTIME" --base-image "$BASE_IMAGE")
      [ "$NO_BUILD" = 1 ] && PROXY_ISOLATION_ARGS+=(--no-build)
      [ "$KEEP" = 1 ] && PROXY_ISOLATION_ARGS+=(--keep)
      run_suite "agent-proxy-isolation" \
        uv run python "$ROOT/scripts/e2e-agent-proxy-isolation.py" "${PROXY_ISOLATION_ARGS[@]}"
    else
      fail_suite "agent-proxy-isolation" \
        "select apple-container, docker, podman, or an available auto runtime"
    fi
  fi
  if selected agent-proxy; then
    run_suite "agent-proxy" \
      uv run python "$ROOT/scripts/check-agent-proxy.py" --base-image "$BASE_IMAGE"
  fi
else
  if selected agent-proxy-isolation; then
    skip_suite "agent-proxy-isolation" "pass --with-agent-proxy"
  fi
  if selected agent-proxy; then
    skip_suite "agent-proxy" "pass --with-agent-proxy; makes two billable requests"
  fi
fi

# Closing summary: every suite this invocation considered, with its outcome.
passed=0
failed=0
skipped=0
echo "========================================"
echo "Summary"
echo "========================================"
for i in "${!RESULT_NAME[@]}"; do
  status="${RESULT_STATUS[$i]}"
  detail="${RESULT_DETAIL[$i]}"
  case "$status" in
    PASS) passed=$((passed + 1)) ;;
    FAIL) failed=$((failed + 1)) ;;
    SKIP) skipped=$((skipped + 1)) ;;
  esac
  if [ -n "$detail" ]; then
    printf '  %-4s %s  (%s)\n' "$status" "${RESULT_NAME[$i]}" "$detail"
  else
    printf '  %-4s %s\n' "$status" "${RESULT_NAME[$i]}"
  fi
done
echo
echo "Ran $((passed + failed)) suite(s): $passed passed, $failed failed, $skipped skipped."
if [ "$LOGS_KEPT" = 1 ]; then
  echo "Suite logs kept in: $LOG_DIR"
else
  rmdir "$LOG_DIR" 2>/dev/null || true
fi

if [ "$overall_fail" = 0 ]; then
  echo "All e2e suites PASSED."
  exit 0
else
  echo "One or more e2e suites FAILED."
  exit 1
fi
