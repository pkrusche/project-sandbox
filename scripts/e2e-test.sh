#!/usr/bin/env bash
# End-to-end smoke test for project-sandbox.
#
# Creates a throwaway Python hello-world project, runs the tool against it,
# and verifies what each run writes: nothing in the project for a plain run
# (generated state lives in private host state), and real, self-ignoring files
# in .devcontainer/ for --devcontainer. By default this passes
# --no-build so the test is portable to hosts without apple/container installed;
# pass --with-container to additionally exercise direct Apple container runs,
# including the timeout path (requires the `container` CLI on PATH and a running
# container system).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

WITH_CONTAINER=0
case "${1:-}" in
  --with-container) WITH_CONTAINER=1 ;;
  -h|--help)
    sed -n '2,9p' "$0"
    exit 0
    ;;
  "") ;;
  *)
    echo "Unknown option: $1" >&2
    exit 64
    ;;
esac

if [ "$WITH_CONTAINER" = 1 ] && ! command -v container >/dev/null 2>&1; then
  echo "ERROR: --with-container requires the apple/container CLI on PATH." >&2
  exit 64
fi

TMP_PROJECT="$(mktemp -d -t project-sandbox-e2e.XXXXXX)"
# Private host state and per-session staging go to throwaway roots, so the
# checks below can see exactly what a run leaves behind.
TMP_STATE="$(mktemp -d -t project-sandbox-e2e-state.XXXXXX)"
export XDG_STATE_HOME="$TMP_STATE/state"
export PROJECT_SANDBOX_RUNTIME_DIR="$TMP_STATE/runtime"
PROJECT_KEPT=0
cleanup() {
  if [ "$PROJECT_KEPT" = 0 ]; then
    rm -rf "$TMP_PROJECT" "$TMP_STATE"
  fi
}
trap cleanup EXIT

cat > "$TMP_PROJECT/hello.py" <<'PY'
def main() -> None:
    print("hello, sandbox!")


if __name__ == "__main__":
    main()
PY

echo "Test project: $TMP_PROJECT"
echo "State roots:  $TMP_STATE"
echo

cd "$ROOT"

fail=0
if [ "$WITH_CONTAINER" = 1 ]; then
  echo "Running: uv run project-sandbox $TMP_PROJECT python:3.12-slim"
  uv run project-sandbox "$TMP_PROJECT" python:3.12-slim
else
  echo "Running: uv run project-sandbox --no-build $TMP_PROJECT python:3.12-slim"
  uv run project-sandbox --no-build "$TMP_PROJECT" python:3.12-slim
fi
echo

echo "Checking that a plain run leaves the project alone:"
leftover="$(cd "$TMP_PROJECT" && ls -A | grep -v '^hello.py$' || true)"
if [ -z "$leftover" ]; then
  echo "  ok    the project holds only hello.py"
else
  echo "  BAD   a plain run wrote into the project: $leftover"
  fail=1
fi
staging_left="$(ls -A "$PROJECT_SANDBOX_RUNTIME_DIR/staging" 2>/dev/null || true)"
if [ -z "$staging_left" ]; then
  echo "  ok    no session staging is left behind"
else
  echo "  BAD   session staging left behind: $staging_left"
  fail=1
fi
STATE_DIR="$(find "$XDG_STATE_HOME/project-sandbox/projects" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
if [ -n "$STATE_DIR" ] && [ -f "$STATE_DIR/migrated" ]; then
  echo "  ok    project state: $STATE_DIR"
else
  echo "  MISS  no private project state under $XDG_STATE_HOME/project-sandbox/projects"
  fail=1
fi
mode="$(stat -f '%Lp' "$XDG_STATE_HOME/project-sandbox" 2>/dev/null || stat -c '%a' "$XDG_STATE_HOME/project-sandbox")"
if [ "$mode" = 700 ]; then
  echo "  ok    the state root is 0700"
else
  echo "  BAD   the state root is $mode, expected 700"
  fail=1
fi
echo

echo "Running: uv run project-sandbox --devcontainer $TMP_PROJECT python:3.12-slim"
uv run project-sandbox --devcontainer "$TMP_PROJECT" python:3.12-slim
echo

DC="$TMP_PROJECT/.devcontainer"

# The devcontainer's files are real files in .devcontainer/, nothing else.
REQUIRED=(
  "$DC/devcontainer.json"
  "$DC/Dockerfile"
  "$DC/project-sandbox-devcontainer-init"
  "$DC/.gitignore"
)

ABSENT=(
  "$TMP_PROJECT/.project-sandbox"
  "$TMP_PROJECT/.gitignore"
  "$DC/init-firewall.sh"
  "$DC/entrypoint.sh"
  "$DC/claude"
  "$DC/codex"
)

echo "Checking required files:"
for f in "${REQUIRED[@]}"; do
  if [ -f "$f" ] && [ ! -L "$f" ]; then
    echo "  ok    $f"
  else
    echo "  MISS  $f (missing, or not a regular file)"
    fail=1
  fi
done

for f in "${ABSENT[@]}"; do
  if [ -e "$f" ] || [ -L "$f" ]; then
    echo "  BAD   $f (should not exist)"
    fail=1
  else
    echo "  ok    absent: $f"
  fi
done

echo
echo "Checking content invariants:"

check_contains() {
  local file="$1" needle="$2"
  if grep -qF -- "$needle" "$file"; then
    echo "  ok    $file contains: $needle"
  else
    echo "  MISS  $file missing: $needle"
    fail=1
  fi
}

check_contains "$DC/Dockerfile" "FROM python:3.12-slim"
check_contains "$DC/Dockerfile" "COPY project-sandbox-devcontainer-init"
check_contains "$DC/.gitignore" "*"
check_contains "$DC/devcontainer.json" '"none"'
check_contains "$DC/devcontainer.json" '"UV_OFFLINE": "1"'
check_contains "$DC/devcontainer.json" "$STATE_DIR/history/shell"
check_contains "$DC/devcontainer.json" "$STATE_DIR/devcontainer-mask"

if command -v python3 >/dev/null 2>&1; then
  if python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$DC/devcontainer.json" 2>/dev/null; then
    echo "  ok    $DC/devcontainer.json parses as JSON"
  else
    echo "  BAD   $DC/devcontainer.json is not valid JSON"
    fail=1
  fi
fi

if [ "$WITH_CONTAINER" = 1 ]; then
  echo
  echo "Checking direct Apple container runtime:"
  if uv run project-sandbox \
    --runtime apple-container \
    --agent bash \
    --prompt-text "printf direct-runtime-ok" \
    --timeout 30 \
    "$TMP_PROJECT" \
    python:3.12-slim; then
    echo "  ok    direct bash agent run completed"
  else
    echo "  BAD   direct bash agent run failed"
    fail=1
  fi

  set +e
  uv run project-sandbox \
    --runtime apple-container \
    --no-build \
    --agent bash \
    --prompt-text "sleep 10" \
    --timeout 1 \
    "$TMP_PROJECT" \
    python:3.12-slim
  timeout_rc=$?
  set -e
  if [ "$timeout_rc" = 124 ]; then
    echo "  ok    direct runtime timeout returned 124"
  else
    echo "  BAD   direct runtime timeout returned $timeout_rc (expected 124)"
    fail=1
  fi
fi

echo
if [ "$fail" = 0 ]; then
  PROJECT_KEPT=1
  cat <<EOF
PASS

Test project kept for inspection:
  $TMP_PROJECT

Useful next commands:
  ls -la $TMP_PROJECT $DC
  cat $DC/Dockerfile
  cat $DC/devcontainer.json
  ls -la $STATE_DIR

Remove when done:
  rm -rf $TMP_PROJECT $TMP_STATE
EOF
  exit 0
else
  echo "FAIL — see missing/invalid artefacts above."
  echo "Test project (kept for debugging): $TMP_PROJECT (state: $TMP_STATE)"
  PROJECT_KEPT=1
  exit 1
fi
