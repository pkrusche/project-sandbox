#!/usr/bin/env bash
# End-to-end verification that --python-uv gives the agent a usable interpreter.
#
# Builds two throwaway uv projects and checks, inside the sandbox and as the
# unprivileged agent user, that 'uv run' works and resolves to the baked
# /opt/venv. One project pins a requires-python the base image cannot satisfy,
# which makes uv fetch its own interpreter during the build: left in root's
# home that interpreter is unreadable to the agent and every 'uv run' fails
# with "failed to canonicalize path /opt/venv/bin/python3: Permission denied".
#
# Usage:
#   scripts/e2e-python-uv.sh [--runtime auto|apple-container|docker|podman]
#                            [--keep]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

RUNTIME="auto"
KEEP=0

# Pinned rather than derived so both scenarios stay deterministic: the base
# image ships BASE_PYTHON, and MANAGED_PYTHON is newer than it, so uv must
# download an interpreter for the second project. Each scenario also asserts
# where its interpreter came from, so bumping these into agreement fails the
# run instead of quietly making it vacuous.
BASE_PYTHON="3.11"
MANAGED_PYTHON="3.13"

usage() { sed -n '2,13p' "$0"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --runtime) RUNTIME="${2:?--runtime needs a value}"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 64 ;;
  esac
done

# The image build is the subject of this test, so unlike the workflow scripts
# there is no --no-build mode and chroot (which builds no image) cannot run it.
case "$RUNTIME" in
  auto) ;;
  apple-container) command -v container >/dev/null 2>&1 || { echo "ERROR: container CLI not found." >&2; exit 64; } ;;
  docker|podman) command -v "$RUNTIME" >/dev/null 2>&1 || { echo "ERROR: $RUNTIME CLI not found." >&2; exit 64; } ;;
  chroot) echo "ERROR: --python-uv builds an image; the chroot runtime cannot run this suite." >&2; exit 64 ;;
  *) echo "ERROR: unsupported --runtime '$RUNTIME'" >&2; exit 64 ;;
esac

command -v uv >/dev/null 2>&1 || { echo "ERROR: uv not found on PATH." >&2; exit 64; }

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

# Apple's build VM cannot read the macOS per-user temp directory, so throwaway
# projects live under the repo like the other container-backed suites.
TMP_BASE="$ROOT/.project-sandbox/e2e"
mkdir -p "$TMP_BASE"
TMP_ROOT="$(mktemp -d "$TMP_BASE/python-uv-e2e.XXXXXX")"
cleanup() {
  if [ "$KEEP" = 0 ]; then
    rm -rf "$TMP_ROOT"
  fi
}
trap cleanup EXIT

fail=0
ok()  { echo "  ok    $*"; }
bad() { echo "  BAD   $*"; fail=1; }

# Compares a captured stdout file, reporting the matching stderr capture on a
# mismatch so a failure shows uv's own diagnostic rather than just the diff.
assert_file_equals() {
  local what="$1" file="$2" expected="$3" error_file="${4:-}"
  if [ ! -f "$file" ]; then
    bad "$what — $(basename "$file") was not written"
    return
  fi
  local actual
  actual="$(cat "$file")"
  if [ "$actual" = "$expected" ]; then
    ok "$what"
    return
  fi
  bad "$what — got '$actual' (expected '$expected')"
  if [ -n "$error_file" ] && [ -s "$error_file" ]; then
    sed 's/^/          /' "$error_file"
  fi
}

# Write a minimal, buildable uv project. Hatchling needs an explicit package
# path here: the src layout alone is not enough for its default file selection.
make_project() {
  local dir="$1" name="$2" requires_python="$3"
  mkdir -p "$dir/src/$name"
  : > "$dir/src/$name/__init__.py"
  printf 'e2e fixture\n' > "$dir/README.md"
  cat > "$dir/pyproject.toml" <<EOF
[project]
name = "$name"
version = "0.1.0"
requires-python = ">=$requires_python"
dependencies = []

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/$name"]
EOF
  (cd "$dir" && uv lock >/dev/null 2>&1) || {
    echo "ERROR: uv lock failed for $dir" >&2
    exit 1
  }
}

# Run one sandbox session and assert the agent can actually use the venv.
# The checks capture stderr into the workspace so a failure reports uv's own
# message rather than just a missing file.
check_scenario() {
  local label="$1" dir="$2" interpreter_prefix="$3"

  echo
  echo "Scenario: $label"

  # uv reports progress on stderr, so each check keeps the two streams in
  # separate files: stdout carries the value to assert on, stderr the reason a
  # failing check failed.
  local prompt
  prompt=$(cat <<'PROMPT'
whoami > agent-user.txt 2>/dev/null || true
uv run python -c 'import sys; print(sys.prefix)' > uv-prefix.txt 2> uv-prefix-error.txt || true
uv run python -c 'print("uv-run-ok")' > uv-run.txt 2> uv-run-error.txt || true
readlink -f /opt/venv/bin/python3 > interpreter.txt 2> interpreter-error.txt || true
"$(readlink -f /opt/venv/bin/python3)" --version > interpreter-version.txt 2>&1 || true
PROMPT
  )

  if (cd "$ROOT" && uv run project-sandbox \
    --runtime "$RUNTIME" \
    --python-uv \
    --python "$BASE_PYTHON" \
    --agent bash \
    --prompt-text "$prompt" \
    --no-forward-credentials \
    --timeout 300 \
    "$dir"); then
    ok "$label: sandbox session completed"
  else
    bad "$label: sandbox session failed"
  fi

  # A root session would mask the bug entirely: /root is only unreadable to the
  # unprivileged agent, so assert who ran before trusting the other checks.
  assert_file_equals "$label: runs as the agent user" "$dir/agent-user.txt" "agent"
  assert_file_equals "$label: uv run uses the baked venv" \
    "$dir/uv-prefix.txt" "/opt/venv" "$dir/uv-prefix-error.txt"
  assert_file_equals "$label: uv run executes" \
    "$dir/uv-run.txt" "uv-run-ok" "$dir/uv-run-error.txt"

  # Pinning the expected location keeps each scenario honest: the managed case
  # must really have downloaded an interpreter, and neither may land in /root.
  local interpreter
  interpreter="$(cat "$dir/interpreter.txt" 2>/dev/null || true)"
  case "$interpreter" in
    "$interpreter_prefix"/*)
      ok "$label: interpreter resolves under $interpreter_prefix" ;;
    "")
      bad "$label: interpreter did not resolve (unreadable to the agent?)"
      [ -s "$dir/interpreter-error.txt" ] && sed 's/^/          /' "$dir/interpreter-error.txt" ;;
    *)
      bad "$label: interpreter is $interpreter (expected it under $interpreter_prefix)" ;;
  esac

  if grep -q "^Python " "$dir/interpreter-version.txt" 2>/dev/null; then
    ok "$label: the agent can execute the interpreter"
  else
    bad "$label: the agent cannot execute the interpreter: $(cat "$dir/interpreter-version.txt" 2>/dev/null || echo '<missing>')"
  fi
}

echo "Test root: $TMP_ROOT"
echo "Configuration: runtime=$RUNTIME base_python=$BASE_PYTHON managed_python=$MANAGED_PYTHON"

# The interpreter uv has to download is the regression case; the interpreter
# that ships in the base image is the common path and must keep working.
make_project "$TMP_ROOT/managed-python" managedpython "$MANAGED_PYTHON"
check_scenario "uv-managed interpreter" "$TMP_ROOT/managed-python" "/opt/uv-python"

make_project "$TMP_ROOT/base-python" basepython "$BASE_PYTHON"
check_scenario "base image interpreter" "$TMP_ROOT/base-python" "/usr/local"

echo
if [ "$fail" = 0 ]; then
  echo "PASS"
  if [ "$KEEP" = 1 ]; then
    echo
    echo "Test projects kept for inspection: $TMP_ROOT"
  fi
  exit 0
fi

if [ "$KEEP" = 1 ]; then
  echo "FAIL - test projects kept for debugging: $TMP_ROOT"
else
  echo "FAIL - test projects will be removed (use --keep to retain them): $TMP_ROOT"
fi
exit 1
