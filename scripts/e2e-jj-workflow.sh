#!/usr/bin/env bash
# End-to-end jj workflow verification for headless bash-agent sessions.
#
# Creates a throwaway jj repository (matching tests/test_jj_workspace.py's
# _make_jj_repo), asks the sandboxed bash agent to modify the jj workspace, and
# verifies the single after-session action: the bookmark is advanced to the
# session's revision (without rebasing onto the default workspace), the workspace
# is removed by default, and --keep-workspace leaves it in place. The agent runs
# jj inside the container, which works because project-sandbox mounts both the
# shared .jj/repo store and the git backend it points at for an additional
# workspace.
#
# Requirements: Linux with unshare, uv, and jj. Container runtimes are optional.
#
# Usage:
#   scripts/e2e-jj-workflow.sh [--runtime chroot|auto|apple-container|docker|podman]
#                              [--base-image IMAGE] [--no-build] [--keep]
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
  chroot)
    if [ "$(uname -s)" != Linux ] || ! command -v unshare >/dev/null 2>&1; then
      echo "ERROR: chroot requires Linux and unshare." >&2
      exit 64
    fi
    if ! unshare --map-root-user --mount -- true >/dev/null 2>&1; then
      echo "ERROR: chroot requires unprivileged user namespaces (unshare --map-root-user --mount failed)." >&2
      echo "On Ubuntu 24.04+ (including GitHub Actions ubuntu-latest runners), this is usually blocked by AppArmor; run:" >&2
      echo "  sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0" >&2
      exit 64
    fi
    ;;
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

# Chroot can use the system temp directory directly. Container runtimes keep the
# throwaway repo under the project because Apple's build VM cannot read the
# macOS per-user temp directory ($TMPDIR, /var/folders/...).
if [ "$RUNTIME" = chroot ]; then
  TMP_BASE="${TMPDIR:-/tmp}/project-sandbox-e2e"
else
  TMP_BASE="$ROOT/.project-sandbox/e2e"
fi
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
  local keep="$2"
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
    --no-forward-credentials
    --no-firewall
    --verbose
  )
  if [ "$keep" = 1 ]; then
    cmd+=(--keep-workspace)
  fi
  if [ "$NO_BUILD" = 1 ]; then
    cmd+=(--no-build)
  fi
  cmd+=("$TMP_PROJECT" "$BASE_IMAGE")

  echo "Running jj workflow on bookmark $bookmark (keep-workspace=$keep)"
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

# Default: work is captured on the bookmark, the default workspace revision is
# untouched (no rebase into main), and the workspace is removed.
echo
run_ps "e2e-jj-default" 0 "jj-default.txt" "jj default" "agent: jj default"
assert_jj_file_contains "$TMP_PROJECT" "e2e-jj-default" "jj-default.txt" "jj default"
assert_jj_log_contains "e2e-jj-default" "agent: jj default"
if jj -R "$TMP_PROJECT" file show -r @ "root:jj-default.txt" >/dev/null 2>&1; then
  echo "  BAD   default action changed the default workspace revision"
  fail=1
else
  echo "  ok    default action left the default workspace revision unchanged"
fi
if [ -d "${TMP_PROJECT}-workspaces/e2e-jj-default" ]; then
  echo "  BAD   default workspace was not removed"
  fail=1
else
  echo "  ok    default workspace was removed"
fi

# --keep-workspace: same bookmark capture, but the workspace is left in place.
echo
run_ps "e2e-jj-keep" 1 "jj-keep.txt" "jj keep" "agent: jj keep"
assert_jj_file_contains "$TMP_PROJECT" "e2e-jj-keep" "jj-keep.txt" "jj keep"
assert_jj_log_contains "e2e-jj-keep" "agent: jj keep"
WS_KEEP="${TMP_PROJECT}-workspaces/e2e-jj-keep"
if [ -d "$WS_KEEP" ]; then
  echo "  ok    keep-workspace left the workspace in place"
  assert_jj_file_contains "$WS_KEEP" "e2e-jj-keep" "jj-keep.txt" "jj keep"
else
  echo "  BAD   keep-workspace removed the workspace"
  fail=1
fi

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
