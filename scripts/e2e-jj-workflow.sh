#!/usr/bin/env bash
# End-to-end jj workflow verification for headless bash-agent sessions.
#
# Creates a throwaway jj repository (matching tests/test_jj_workspace.py's
# _make_jj_repo), asks the sandboxed bash agent to modify the jj workspace, and
# verifies --after-session=rebase, merge, and nothing. The agent runs jj inside
# the container, which works because project-sandbox mounts both the shared
# .jj/repo store and the git backend it points at for an additional workspace.
#
# In jj, project-sandbox currently handles merge the same way as rebase: it
# rebases the bookmarked agent change onto the default workspace revision.
#
# Requirements: uv, jj, and a supported container runtime on PATH.
#
# Usage:
#   scripts/e2e-jj-workflow.sh [--runtime auto|apple-container|docker|podman]
#                              [--base-image IMAGE] [--no-build] [--keep]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

RUNTIME="auto"
BASE_IMAGE="python:3.12-slim"
NO_BUILD=0
KEEP=0

usage() { sed -n '2,17p' "$0"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --runtime) RUNTIME="${2:?--runtime needs a value}"; shift 2 ;;
    --base-image) BASE_IMAGE="${2:?--base-image needs a value}"; shift 2 ;;
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
  *) echo "ERROR: unsupported --runtime '$RUNTIME'" >&2; exit 64 ;;
esac

command -v uv >/dev/null 2>&1 || { echo "ERROR: uv not found on PATH." >&2; exit 64; }
command -v jj >/dev/null 2>&1 || { echo "ERROR: jj not found on PATH." >&2; exit 64; }

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

# Apple's `container` build VM cannot read the macOS per-user temp dir
# ($TMPDIR, /var/folders/...), so a build context created there arrives empty
# and the image's COPY steps fail. Keep the throwaway repo under the repo's
# gitignored .project-sandbox/ tree, which the runtime can access (the same
# reason scripts/verify-timeout-teardown.sh does this).
TMP_BASE="$ROOT/.project-sandbox/e2e"
mkdir -p "$TMP_BASE"
TMP_PROJECT="$(mktemp -d "$TMP_BASE/jj-e2e.XXXXXX")"
cleanup() {
  if [ "$KEEP" = 0 ]; then
    rm -rf "$TMP_PROJECT" "${TMP_PROJECT}-workspaces"
  fi
}
trap cleanup EXIT

fail=0

run_ps() {
  local bookmark="$1"
  local after="$2"
  local file="$3"
  local text="$4"
  local message="$5"
  local prompt

  prompt=$(
    printf "set -euo pipefail\n"
    printf "printf '%%s\\\\n' %q > %q\n" "$text" "$file"
    printf "jj describe -m %q\n" "$message"
    printf "jj status\n"
  )

  local cmd=(
    uv run project-sandbox
    --runtime "$RUNTIME"
    --agent bash
    --prompt-text "$prompt"
    --branch "$bookmark"
    --after-session "$after"
    --no-forward-credentials
    --no-firewall
    --verbose
  )
  if [ "$NO_BUILD" = 1 ]; then
    cmd+=(--no-build)
  fi
  cmd+=("$TMP_PROJECT" "$BASE_IMAGE")

  echo "Running jj $after workflow on bookmark $bookmark"
  (cd "$ROOT" && "${cmd[@]}")
}

assert_jj_file_contains() {
  local repo="$1"
  local rev="$2"
  local file="$3"
  local needle="$4"
  # jj resolves file-show paths relative to the cwd, not -R; anchor to the repo
  # root with a root: fileset so it works regardless of where the script runs.
  if jj -R "$repo" file show -r "$rev" "root:$file" | grep -qF -- "$needle"; then
    echo "  ok    $rev:$file contains: $needle"
  else
    echo "  BAD   $rev:$file missing: $needle"
    fail=1
  fi
}

assert_jj_log_contains() {
  local rev="$1"
  local needle="$2"
  if jj -R "$TMP_PROJECT" log -r "$rev" --no-graph --template 'description ++ "\n"' | grep -qF -- "$needle"; then
    echo "  ok    jj revision $rev has description: $needle"
  else
    echo "  BAD   jj revision $rev missing description: $needle"
    fail=1
  fi
}

echo "Test jj repo: $TMP_PROJECT"
echo "Configuration: runtime=$RUNTIME requested_runtime=$REQUESTED_RUNTIME base_image=$BASE_IMAGE no_build=$NO_BUILD"
jj git init "$TMP_PROJECT" >/dev/null
jj -R "$TMP_PROJECT" config set --repo user.name "Project Sandbox E2E"
jj -R "$TMP_PROJECT" config set --repo user.email "project-sandbox-e2e@example.invalid"
printf "base\n" > "$TMP_PROJECT/README.md"
jj -R "$TMP_PROJECT" describe -m "initial commit"
jj -R "$TMP_PROJECT" new

# Let the sandbox's agent user write in this disposable repo on Docker/Podman
# hosts where the container UID may not match the host user.
chmod -R a+rwX "$TMP_PROJECT"

echo
run_ps "e2e-jj-rebase" "rebase" "jj-rebase.txt" "jj rebase" "agent: jj rebase"
assert_jj_file_contains "$TMP_PROJECT" "e2e-jj-rebase" "jj-rebase.txt" "jj rebase"
assert_jj_log_contains "e2e-jj-rebase" "agent: jj rebase"
if [ -d "${TMP_PROJECT}-workspaces/e2e-jj-rebase" ]; then
  echo "  BAD   rebase workspace was not removed"
  fail=1
else
  echo "  ok    rebase workspace was removed"
fi

echo
run_ps "e2e-jj-merge" "merge" "jj-merge.txt" "jj merge" "agent: jj merge"
assert_jj_file_contains "$TMP_PROJECT" "e2e-jj-merge" "jj-merge.txt" "jj merge"
assert_jj_log_contains "e2e-jj-merge" "agent: jj merge"
if [ -d "${TMP_PROJECT}-workspaces/e2e-jj-merge" ]; then
  echo "  BAD   merge workspace was not removed"
  fail=1
else
  echo "  ok    merge workspace was removed"
fi

echo
run_ps "e2e-jj-nothing" "nothing" "jj-nothing.txt" "jj nothing" "agent: jj nothing"
if jj -R "$TMP_PROJECT" file show -r @ "root:jj-nothing.txt" >/dev/null 2>&1; then
  echo "  BAD   nothing action changed the default workspace revision"
  fail=1
else
  echo "  ok    nothing action left the default workspace revision unchanged"
fi
WS_NOTHING="${TMP_PROJECT}-workspaces/e2e-jj-nothing"
if [ -d "$WS_NOTHING" ]; then
  echo "  ok    nothing workspace remains"
else
  echo "  BAD   nothing workspace was removed"
  fail=1
fi
assert_jj_file_contains "$WS_NOTHING" "e2e-jj-nothing" "jj-nothing.txt" "jj nothing"
assert_jj_log_contains "e2e-jj-nothing" "agent: jj nothing"

echo
if [ "$fail" = 0 ]; then
  echo "PASS"
  if [ "$KEEP" = 1 ]; then
    cat <<EOF

Test repository kept for inspection:
  $TMP_PROJECT

Remove when done:
  rm -rf "$TMP_PROJECT" "${TMP_PROJECT}-workspaces"
EOF
  fi
  exit 0
fi

if [ "$KEEP" = 1 ]; then
  echo "FAIL - test repository kept for debugging: $TMP_PROJECT"
else
  echo "FAIL - test repository will be removed (use --keep to retain it): $TMP_PROJECT"
fi
exit 1
