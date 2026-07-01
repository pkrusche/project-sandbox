#!/usr/bin/env bash
# End-to-end git workflow verification for headless bash-agent sessions.
#
# Creates a throwaway git repository, asks the sandboxed bash agent to commit
# changes in a branch worktree, and verifies the single after-session action:
# the work lands on the branch (never on the main checkout), the worktree is
# removed by default, and --keep-workspace leaves it in place for reuse.
#
# Requirements: Linux with unshare, uv, and git. Container runtimes are optional.
#
# Usage:
#   scripts/e2e-git-workflow.sh [--runtime chroot|auto|apple-container|docker|podman]
#                               [--base-image IMAGE] [--no-build] [--keep]
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

usage() { sed -n '2,13p' "$0"; }

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
  chroot) [ "$(uname -s)" = Linux ] && command -v unshare >/dev/null 2>&1 || { echo "ERROR: chroot requires Linux and unshare." >&2; exit 64; } ;;
  apple-container) command -v container >/dev/null 2>&1 || { echo "ERROR: container CLI not found." >&2; exit 64; } ;;
  docker|podman) command -v "$RUNTIME" >/dev/null 2>&1 || { echo "ERROR: $RUNTIME CLI not found." >&2; exit 64; } ;;
  *) echo "ERROR: unsupported --runtime '$RUNTIME'" >&2; exit 64 ;;
esac

command -v uv >/dev/null 2>&1 || { echo "ERROR: uv not found on PATH." >&2; exit 64; }
command -v git >/dev/null 2>&1 || { echo "ERROR: git not found on PATH." >&2; exit 64; }

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
TMP_PROJECT="$(mktemp -d "$TMP_BASE/git-e2e.XXXXXX")"
cleanup() {
  if [ "$KEEP" = 0 ]; then
    rm -rf "$TMP_PROJECT" "${TMP_PROJECT}-worktrees"
  fi
}
trap cleanup EXIT

fail=0

run_ps() {
  local branch="$1"
  local keep="$2"
  local file="$3"
  local text="$4"
  local message="$5"
  local prompt

  prompt=$(
    printf "set -euo pipefail\n"
    printf "git config --global --add safe.directory /workspace || true\n"
    printf "printf '%%s\\\\n' %q > %q\n" "$text" "$file"
    printf "git add %q\n" "$file"
    printf "git commit -m %q\n" "$message"
  )

  local cmd=(
    uv run project-sandbox
    --runtime "$RUNTIME"
    --agent bash
    --prompt-text "$prompt"
    --branch "$branch"
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

  echo "Running git workflow on branch $branch (keep-workspace=$keep)"
  (cd "$ROOT" && "${cmd[@]}")
}

assert_file_contains() {
  local file="$1"
  local needle="$2"
  if grep -qF -- "$needle" "$file"; then
    echo "  ok    $file contains: $needle"
  else
    echo "  BAD   $file missing: $needle"
    fail=1
  fi
}

git_log_contains() {
  local repo="$1"
  local needle="$2"
  shift 2

  # Do not pipe `git log` into `grep -q` under pipefail: grep can close the
  # pipe after a match and cause git to fail with SIGPIPE.
  [ -n "$(git -C "$repo" log "$@" --fixed-strings --grep="$needle" --format=%s -1)" ]
}

assert_branch_log_contains() {
  local branch="$1"
  local needle="$2"
  if git_log_contains "$TMP_PROJECT" "$needle" "$branch"; then
    echo "  ok    branch $branch history contains: $needle"
  else
    echo "  BAD   branch $branch history missing: $needle"
    fail=1
  fi
}

refute_main_log_contains() {
  local needle="$1"
  if git_log_contains "$TMP_PROJECT" "$needle" HEAD; then
    echo "  BAD   main HEAD history unexpectedly contains: $needle"
    fail=1
  else
    echo "  ok    main HEAD history does not contain: $needle"
  fi
}

print_git_debug_info() {
  local worktree

  printf '\nGit debug information\n'
  printf '\nMain worktree status:\n'
  git -C "$TMP_PROJECT" status --short --branch || true

  printf '\nRegistered worktrees:\n'
  git -C "$TMP_PROJECT" worktree list --porcelain || true

  printf '\nBranch refs:\n'
  git -C "$TMP_PROJECT" show-ref --heads || true

  printf '\nRecent commits (including reflogs):\n'
  git -C "$TMP_PROJECT" log --graph --decorate --oneline --all --reflog -30 || true

  printf '\nRecent reflog entries:\n'
  git -C "$TMP_PROJECT" reflog --all --date=iso -30 || true

  if [ ! -d "${TMP_PROJECT}-worktrees" ]; then
    printf '\nNo worktree directory exists at %s\n' "${TMP_PROJECT}-worktrees"
    return
  fi

  for worktree in "${TMP_PROJECT}-worktrees"/*; do
    [ -d "$worktree" ] || continue
    printf '\nRetained worktree: %s\n' "$worktree"
    git -C "$worktree" status --short --branch || true
    git -C "$worktree" log -1 --decorate --format='HEAD: %H%nParents: %P%nSubject: %s' || true
  done
}

exit_with_failure() {
  print_git_debug_info
  echo
  if [ "$KEEP" = 1 ]; then
    echo "FAIL - test repository kept for debugging: $TMP_PROJECT"
  else
    echo "FAIL - test repository will be removed (use --keep to retain it): $TMP_PROJECT"
  fi
  exit 1
}

echo "Test git repo: $TMP_PROJECT"
echo "Configuration: runtime=$RUNTIME requested_runtime=$REQUESTED_RUNTIME base_image=$BASE_IMAGE no_build=$NO_BUILD"
git -C "$TMP_PROJECT" init -q
git -C "$TMP_PROJECT" config user.name "Project Sandbox E2E"
git -C "$TMP_PROJECT" config user.email "project-sandbox-e2e@example.invalid"
printf "base\n" > "$TMP_PROJECT/README.md"
git -C "$TMP_PROJECT" add README.md
git -C "$TMP_PROJECT" commit -qm "initial commit"

# Let the sandbox's agent user write in this disposable repo on Docker/Podman
# hosts where the container UID may not match the host user.
chmod -R a+rwX "$TMP_PROJECT"

# Default: work lands on the branch, main checkout is untouched, worktree removed.
echo
if ! run_ps "e2e-git-default" 0 "git-default.txt" "git default" "agent: git default"; then
  echo "  BAD   project-sandbox failed during the default workflow"
  exit_with_failure
fi
if [ -e "$TMP_PROJECT/git-default.txt" ]; then
  echo "  BAD   default action changed the main worktree"
  fail=1
else
  echo "  ok    default action left the main worktree unchanged"
fi
assert_branch_log_contains "e2e-git-default" "agent: git default"
refute_main_log_contains "agent: git default"
if [ -d "${TMP_PROJECT}-worktrees/e2e-git-default" ]; then
  echo "  BAD   default worktree was not removed"
  fail=1
else
  echo "  ok    default worktree was removed"
fi

# --keep-workspace: same branch capture, but the worktree is left in place.
echo
if ! run_ps "e2e-git-keep" 1 "git-keep.txt" "git keep" "agent: git keep"; then
  echo "  BAD   project-sandbox failed during the keep-workspace workflow"
  exit_with_failure
fi
assert_branch_log_contains "e2e-git-keep" "agent: git keep"
refute_main_log_contains "agent: git keep"
WT_KEEP="${TMP_PROJECT}-worktrees/e2e-git-keep"
if [ -d "$WT_KEEP" ]; then
  echo "  ok    keep-workspace left the worktree in place"
  assert_file_contains "$WT_KEEP/git-keep.txt" "git keep"
else
  echo "  BAD   keep-workspace removed the worktree"
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
  rm -rf "$TMP_PROJECT" "${TMP_PROJECT}-worktrees"
EOF
  fi
  exit 0
fi

exit_with_failure
