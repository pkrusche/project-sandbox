#!/usr/bin/env bash
# Run all project-sandbox end-to-end tests available on this host.
#
# Sequentially executes:
#   1. e2e-test.sh           — artifact-generation smoke test (no container needed)
#   2. e2e-env-injection.sh  — bash-agent env/API-key forwarding
#   3. e2e-git-workflow.sh   — git rebase/merge/nothing workflows
#   4. e2e-jj-workflow.sh    — jj rebase/merge/nothing workflows (skipped if jj not on PATH)
#   5. e2e-dockerfile-tamper.sh — Dockerfile integrity and override behavior
#   6. verify-timeout-teardown.sh — container/VM cleanup after timeout
#   7. e2e-pi-ollama.sh      — Pi + Ollama networking/config
#   8. e2e-internet-proxy-smoke.py — availability-gated routing/bypass smoke test
#   9. e2e-agent-proxy-isolation.py — gateway-only network/credential audit
#  10. e2e-internet-proxy-isolation.py — routing/bypass/failure audit (explicit opt-in)
#  11. check-agent-proxy.py  — real Pi/OpenCode gateway calls (explicit opt-in)
#
# Usage:
#   scripts/run-e2e-tests.sh [--runtime chroot|auto|apple-container|docker|podman]
#                            [--base-image IMAGE] [--no-build] [--keep]
#                            [--with-agent-proxy] [--with-internet-proxy]
#
#   --runtime NAME   runtime forwarded to workflow scripts (default: chroot on Linux, auto otherwise)
#   --base-image IMG base image forwarded to workflow scripts (default: python:3.12-slim)
#   --no-build       forward --no-build to workflow scripts
#   --keep           keep temporary directories on failure for debugging
#   --with-agent-proxy
#                    run proxy isolation plus the billable agent checker
#   --with-internet-proxy
#                    run the destructive two-service Internet isolation audit;
#                    also requires --blocked-url, --internet-proxy-dir, and
#                    --agentgateway-dir
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [ "$(uname -s)" = Linux ]; then
  RUNTIME="chroot"
else
  RUNTIME="auto"
fi
BASE_IMAGE="python:3.12-slim"
NO_BUILD=0
KEEP=0
WITH_AGENT_PROXY=0
WITH_INTERNET_PROXY=0
BLOCKED_URL=""
INTERNET_PROXY_DIR=""
AGENTGATEWAY_DIR=""

usage() { sed -n '2,30p' "$0"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --runtime)    RUNTIME="${2:?--runtime needs a value}"; shift 2 ;;
    --base-image) BASE_IMAGE="${2:?--base-image needs a value}"; shift 2 ;;
    --no-build)   NO_BUILD=1; shift ;;
    --keep)       KEEP=1; shift ;;
    --with-agent-proxy) WITH_AGENT_PROXY=1; shift ;;
    --with-internet-proxy) WITH_INTERNET_PROXY=1; shift ;;
    --blocked-url) BLOCKED_URL="${2:?--blocked-url needs a value}"; shift 2 ;;
    --internet-proxy-dir) INTERNET_PROXY_DIR="${2:?--internet-proxy-dir needs a value}"; shift 2 ;;
    --agentgateway-dir) AGENTGATEWAY_DIR="${2:?--agentgateway-dir needs a value}"; shift 2 ;;
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

# 2. Env/API-key forwarding via headless bash-agent
run_suite "env-injection" "$ROOT/scripts/e2e-env-injection.sh" "${WORKFLOW_ARGS[@]}"

# 3. Git workflow: rebase / merge / nothing
run_suite "git-workflow" "$ROOT/scripts/e2e-git-workflow.sh" "${WORKFLOW_ARGS[@]}"

# 4. Jj workflow: rebase / merge / nothing (only if jj is on PATH)
if command -v jj >/dev/null 2>&1; then
  run_suite "jj-workflow" "$ROOT/scripts/e2e-jj-workflow.sh" "${WORKFLOW_ARGS[@]}"
else
  echo "========================================"
  echo "Suite: jj-workflow  (SKIPPED — jj not found on PATH)"
  echo "========================================"
  echo
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
TAMPER_ARGS=()
[ "$KEEP" = 1 ] && TAMPER_ARGS+=(--keep)
if [ -n "$CONTAINER_RUNTIME" ] && [ "$NO_BUILD" = 0 ]; then
  TAMPER_ARGS+=(--with-container --runtime "$CONTAINER_RUNTIME" --base-image "$BASE_IMAGE")
fi
run_suite "dockerfile-tamper" "$ROOT/scripts/e2e-dockerfile-tamper.sh" "${TAMPER_ARGS[@]}"

# 6. Timeout teardown necessarily builds and kills a real container. Skip it
# for chroot, unavailable auto runtimes, and explicit --no-build runs.
if [ -n "$CONTAINER_RUNTIME" ] && [ "$NO_BUILD" = 0 ]; then
  run_suite "timeout-teardown" \
    "$ROOT/scripts/verify-timeout-teardown.sh" --runtime "$CONTAINER_RUNTIME"
else
  echo "========================================"
  echo "Suite: timeout-teardown  (SKIPPED — select a container runtime without --no-build)"
  echo "========================================"
  echo
fi

# 7. Pi + Ollama: networking and baked config (only if Ollama is reachable;
# chroot cannot run --agent pi, so fall back to auto runtime detection)
if command -v curl >/dev/null 2>&1 && curl -sf --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  PI_OLLAMA_RUNTIME="$RUNTIME"
  [ "$PI_OLLAMA_RUNTIME" = chroot ] && PI_OLLAMA_RUNTIME=auto
  PI_OLLAMA_ARGS=(--runtime "$PI_OLLAMA_RUNTIME" --base-image "$BASE_IMAGE")
  [ "$NO_BUILD" = 1 ] && PI_OLLAMA_ARGS+=(--no-build)
  [ "$KEEP"     = 1 ] && PI_OLLAMA_ARGS+=(--keep)
  run_suite "pi-ollama" "$ROOT/scripts/e2e-pi-ollama.sh" "${PI_OLLAMA_ARGS[@]}"
else
  echo "========================================"
  echo "Suite: pi-ollama  (SKIPPED — Ollama not reachable on 127.0.0.1:11434)"
  echo "========================================"
  echo
fi

# 8. Non-destructive Internet-proxy smoke test. The script itself detects an
# absent listener and reports a successful skip before creating anything.
if [ -n "$CONTAINER_RUNTIME" ]; then
  INTERNET_SMOKE_ARGS=(--runtime "$CONTAINER_RUNTIME" --base-image "$BASE_IMAGE")
  [ "$NO_BUILD" = 1 ] && INTERNET_SMOKE_ARGS+=(--no-build)
  [ "$KEEP" = 1 ] && INTERNET_SMOKE_ARGS+=(--keep)
  run_suite "internet-proxy-smoke" \
    uv run python "$ROOT/scripts/e2e-internet-proxy-smoke.py" "${INTERNET_SMOKE_ARGS[@]}"
else
  echo "Suite: internet-proxy-smoke  (SKIPPED — select a real container runtime)"
  echo
fi

# 9. The combined Internet-proxy audit deliberately controls both external
# services and makes real AI requests. Never availability-gate this destructive test.
if [ "$WITH_INTERNET_PROXY" = 1 ]; then
  if [ -z "$CONTAINER_RUNTIME" ] || [ -z "$BLOCKED_URL" ] || [ -z "$INTERNET_PROXY_DIR" ] || [ -z "$AGENTGATEWAY_DIR" ]; then
    echo "Suite FAIL: Internet-proxy audit requires a real runtime, --blocked-url, --internet-proxy-dir, and --agentgateway-dir"
    overall_fail=1
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
  echo "Suite: internet-proxy-isolation  (SKIPPED — pass --with-internet-proxy; destructive and billable)"
  echo
fi

# 10–11. Gateway-only isolation is non-billable, followed by two real requests.
# Never trigger either merely because a listener happens to be present.
if [ "$WITH_AGENT_PROXY" = 1 ]; then
  if [ -n "$CONTAINER_RUNTIME" ]; then
    PROXY_ISOLATION_ARGS=(--runtime "$CONTAINER_RUNTIME" --base-image "$BASE_IMAGE")
    [ "$NO_BUILD" = 1 ] && PROXY_ISOLATION_ARGS+=(--no-build)
    [ "$KEEP" = 1 ] && PROXY_ISOLATION_ARGS+=(--keep)
    run_suite "agent-proxy-isolation" \
      uv run python "$ROOT/scripts/e2e-agent-proxy-isolation.py" "${PROXY_ISOLATION_ARGS[@]}"
  else
    echo "========================================"
    echo "Suite: agent-proxy-isolation"
    echo "========================================"
    echo "Suite FAIL: select apple-container, docker, podman, or an available auto runtime"
    echo
    overall_fail=1
  fi
  run_suite "agent-proxy" \
    uv run python "$ROOT/scripts/check-agent-proxy.py" --base-image "$BASE_IMAGE"
else
  echo "========================================"
  echo "Suite: agent-proxy  (SKIPPED — pass --with-agent-proxy; makes two billable requests)"
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
