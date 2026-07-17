#!/usr/bin/env bash
# End-to-end Pi + Ollama verification (OpenSpec add-local-ollama-support, task 11.7).
#
# Requires a real container runtime (Apple `container`, Docker, or Podman) and a
# host Ollama server listening on 127.0.0.1:11434 with the target model pulled.
# `--runtime chroot` is not supported: `--agent pi` needs a real container.
#
# The script:
#   1. Detects whether Ollama is reachable on host loopback (and, if the `ollama`
#      CLI is present, whether the target model has been pulled).
#   2. Creates a throwaway test repo.
#   3. Runs `project-sandbox --agent pi --pi-ollama --prompt-text ...` unsupervised,
#      asking Pi to prove both network reachability (curl the fixed hostname) and
#      that the local model can complete a trivial file-writing task.
#   4. Asserts the rendered Pi config and both output files landed on the host.
#   5. Prints the selected Ollama forwarding strategy so the runtime mode under
#      test is recorded explicitly (native mapping, bridge-socat, or probe-gated).
#
# Usage:
#   scripts/e2e-pi-ollama.sh [--runtime auto|apple-container|docker|podman]
#                            [--base-image IMAGE] [--ollama-model MODEL_ID]
#                            [--timeout SECONDS] [--no-build] [--keep]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

RUNTIME="auto"
BASE_IMAGE="python:3.12-slim"
OLLAMA_MODEL="qwen2.5-coder"
TIMEOUT=300
NO_BUILD=0
KEEP=0

usage() { sed -n '2,20p' "$0"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --runtime) RUNTIME="${2:?--runtime needs a value}"; shift 2 ;;
    --base-image) BASE_IMAGE="${2:?--base-image needs a value}"; shift 2 ;;
    --ollama-model) OLLAMA_MODEL="${2:?--ollama-model needs a value}"; shift 2 ;;
    --timeout) TIMEOUT="${2:?--timeout needs a value}"; shift 2 ;;
    --no-build) NO_BUILD=1; shift ;;
    --keep) KEEP=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 64 ;;
  esac
done

case "$RUNTIME" in
  auto) ;;
  apple-container) command -v container >/dev/null 2>&1 || { echo "ERROR: container CLI not found." >&2; exit 64; } ;;
  docker|podman) command -v "$RUNTIME" >/dev/null 2>&1 || { echo "ERROR: $RUNTIME CLI not found." >&2; exit 64; } ;;
  chroot)
    echo "ERROR: --runtime chroot does not support --agent pi (Ollama forwarding requires a real container)." >&2
    echo "Pass --runtime apple-container|docker|podman|auto instead." >&2
    exit 64
    ;;
  *) echo "ERROR: unsupported --runtime '$RUNTIME'" >&2; exit 64 ;;
esac

command -v uv >/dev/null 2>&1 || { echo "ERROR: uv not found on PATH." >&2; exit 64; }
command -v curl >/dev/null 2>&1 || { echo "ERROR: curl not found on PATH." >&2; exit 64; }

REQUESTED_RUNTIME="$RUNTIME"
if [ "$RUNTIME" = auto ]; then
  if [ "$(uname -s)" = Darwin ] && command -v container >/dev/null 2>&1; then
    RUNTIME="apple-container"
  elif command -v docker >/dev/null 2>&1; then
    RUNTIME="docker"
  elif command -v podman >/dev/null 2>&1; then
    RUNTIME="podman"
  else
    echo "ERROR: no supported container runtime found on PATH." >&2
    exit 64
  fi
fi

# --- Detect Ollama ---------------------------------------------------------
echo "Checking for a host Ollama server on 127.0.0.1:11434 ..."
if ! curl -sf --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  echo "ERROR: Ollama is not reachable at http://127.0.0.1:11434/api/tags." >&2
  echo "Install Ollama and start it (e.g. 'ollama serve' or the desktop app)," >&2
  echo "bound to loopback only, then retry." >&2
  exit 64
fi
echo "  ok    Ollama server is reachable on host loopback"

if command -v ollama >/dev/null 2>&1; then
  if ollama list 2>/dev/null | tail -n +2 | awk '{print $1}' | grep -qE "^${OLLAMA_MODEL}(:|$)"; then
    echo "  ok    model '$OLLAMA_MODEL' is present ('ollama list')"
  else
    echo "ERROR: model '$OLLAMA_MODEL' was not found in 'ollama list'." >&2
    echo "Pull it first: ollama pull $OLLAMA_MODEL" >&2
    echo "Or pick an already-pulled model with --ollama-model MODEL_ID." >&2
    exit 64
  fi
else
  echo "  ..   'ollama' CLI not found on PATH; skipping pulled-model check for '$OLLAMA_MODEL'"
fi

# Chroot can use the system temp directory directly, but pi-ollama never runs
# under chroot; container runtimes keep the throwaway repo under the project
# because Apple's build VM cannot read the macOS per-user temp directory.
TMP_BASE="$ROOT/.project-sandbox/e2e"
mkdir -p "$TMP_BASE"
TMP_PROJECT="$(mktemp -d "$TMP_BASE/pi-ollama-e2e.XXXXXX")"
cleanup() {
  if [ "$KEEP" = 0 ]; then
    rm -rf "$TMP_PROJECT"
  fi
}
trap cleanup EXIT

fail=0
ok()  { echo "  ok    $*"; }
bad() { echo "  BAD   $*"; fail=1; }

assert_file_contains() {
  local file="$1"
  local needle="$2"
  if [ ! -f "$file" ]; then
    bad "$file was not written"
    return
  fi
  if grep -qF -- "$needle" "$file"; then
    ok "$file contains: $needle"
  else
    bad "$file missing: $needle"
  fi
}

NETWORK_CHECK_OUT="$TMP_PROJECT/network-check.json"
AGENT_CHECK_OUT="$TMP_PROJECT/agent-check.txt"
AGENT_CHECK_MARKER="PROJECT_SANDBOX_OLLAMA_E2E_OK"

printf "# pi-ollama e2e\n" > "$TMP_PROJECT/README.md"

prompt=$(
  cat <<EOF
Run these two shell commands exactly, in order, and do nothing else:
1. curl -sf http://ollama.project-sandbox.internal:11434/api/tags -o network-check.json
2. printf '${AGENT_CHECK_MARKER}\n' > agent-check.txt
Stop after both commands succeed. Do not explain what you did.
EOF
)

RUN_LOG="$TMP_PROJECT.run.log"
cmd=(
  uv run project-sandbox
  --runtime "$RUNTIME"
  --agent pi
  --pi-ollama
  --ollama-model "$OLLAMA_MODEL"
  --prompt-text "$prompt"
  --no-forward-credentials
  --timeout "$TIMEOUT"
  --verbose
)
if [ "$NO_BUILD" = 1 ]; then
  cmd+=(--no-build)
fi
cmd+=("$TMP_PROJECT" "$BASE_IMAGE")

echo
echo "Test project: $TMP_PROJECT"
echo "Configuration: runtime=$RUNTIME requested_runtime=$REQUESTED_RUNTIME base_image=$BASE_IMAGE ollama_model=$OLLAMA_MODEL timeout=${TIMEOUT}s no_build=$NO_BUILD"
echo
echo "Running pi + Ollama unsupervised check"
if (cd "$ROOT" && "${cmd[@]}") 2>&1 | tee "$RUN_LOG"; then
  ok "sandbox session completed"
else
  bad "sandbox session failed (see $RUN_LOG)"
fi

strategy_line="$(grep -m1 '^Ollama forwarding strategy:' "$RUN_LOG" || true)"
if [ -n "$strategy_line" ]; then
  echo
  echo "Recorded forwarding mode: $strategy_line"
else
  echo
  echo "  ..   no 'Ollama forwarding strategy:' line found in output (unexpected with --verbose)"
fi

echo
echo "Verifying rendered Pi config"
assert_file_contains "$TMP_PROJECT/.project-sandbox/pi/models.json" "\"$OLLAMA_MODEL\""
assert_file_contains "$TMP_PROJECT/.project-sandbox/pi/settings.json" "\"defaultProvider\": \"ollama\""

echo
echo "Verifying agent output"
assert_file_contains "$NETWORK_CHECK_OUT" '"models"'
assert_file_contains "$AGENT_CHECK_OUT" "$AGENT_CHECK_MARKER"

rm -f "$RUN_LOG"

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
