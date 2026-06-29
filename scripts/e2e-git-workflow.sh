#!/usr/bin/env bash
# End-to-end git workflow verification for headless bash-agent sessions.
#
# Creates a throwaway git repository, asks the sandboxed bash agent to commit
# changes in a branch worktree, and verifies --after-session=rebase, merge, and
# nothing.
#
# Requirements: uv, git, and a supported container runtime on PATH.
#
# Usage:
#   scripts/e2e-git-workflow.sh [--runtime auto|apple-container|docker|podman]
#                               [--base-image IMAGE] [--no-build] [--keep]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

RUNTIME="auto"
BASE_IMAGE="python:3.12-slim"
NO_BUILD=0
KEEP=0

usage() { sed -n '2,12p' "$0"; }

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
command -v git >/dev/null 2>&1 || { echo "ERROR: git not found on PATH." >&2; exit 64; }

# Apple's `container` build VM cannot read the macOS per-user temp dir
# ($TMPDIR, /var/folders/...), so a build context created there arrives empty
# and the image's COPY steps fail. Keep the throwaway repo under the repo's
# gitignored .project-sandbox/ tree, which the runtime can access (the same
# reason scripts/verify-timeout-teardown.sh does this).
TMP_BASE="$ROOT/.project-sandbox/e2e"
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
  local after="$2"
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
    --after-session "$after"
    --no-forward-credentials
    --no-firewall
    --verbose
  )
  if [ "$NO_BUILD" = 1 ]; then
    cmd+=(--no-build)
  fi
  cmd+=("$TMP_PROJECT" "$BASE_IMAGE")

  echo "Running git $after workflow on branch $branch"
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

assert_git_log_contains() {
  local needle="$1"
  if git_log_contains "$TMP_PROJECT" "$needle" --all; then
    echo "  ok    git history contains: $needle"
  else
    echo "  BAD   git history missing: $needle"
    fail=1
  fi
}

echo "Test git repo: $TMP_PROJECT"
git -C "$TMP_PROJECT" init -q
git -C "$TMP_PROJECT" config user.name "Project Sandbox E2E"
git -C "$TMP_PROJECT" config user.email "project-sandbox-e2e@example.invalid"
printf "base\n" > "$TMP_PROJECT/README.md"
git -C "$TMP_PROJECT" add README.md
git -C "$TMP_PROJECT" commit -qm "initial commit"

# Let the sandbox's agent user write in this disposable repo on Docker/Podman
# hosts where the container UID may not match the host user.
chmod -R a+rwX "$TMP_PROJECT"

echo
run_ps "e2e-git-rebase" "rebase" "git-rebase.txt" "git rebase" "agent: git rebase"
assert_file_contains "$TMP_PROJECT/git-rebase.txt" "git rebase"
assert_git_log_contains "agent: git rebase"
if [ -d "${TMP_PROJECT}-worktrees/e2e-git-rebase" ]; then
  echo "  BAD   rebase worktree was not removed"
  fail=1
else
  echo "  ok    rebase worktree was removed"
fi

echo
run_ps "e2e-git-merge" "merge" "git-merge.txt" "git merge" "agent: git merge"
assert_file_contains "$TMP_PROJECT/git-merge.txt" "git merge"
assert_git_log_contains "agent: git merge"
if [ "$(git -C "$TMP_PROJECT" log -1 --format=%s)" = "Merge agent session: e2e-git-merge" ]; then
  echo "  ok    merge produced the expected merge commit"
else
  echo "  BAD   HEAD is not the expected merge commit"
  fail=1
fi
if [ -d "${TMP_PROJECT}-worktrees/e2e-git-merge" ]; then
  echo "  BAD   merge worktree was not removed"
  fail=1
else
  echo "  ok    merge worktree was removed"
fi

echo
run_ps "e2e-git-nothing" "nothing" "git-nothing.txt" "git nothing" "agent: git nothing"
if [ -e "$TMP_PROJECT/git-nothing.txt" ]; then
  echo "  BAD   nothing action changed the main worktree"
  fail=1
else
  echo "  ok    nothing action left the main worktree unchanged"
fi
WT_NOTHING="${TMP_PROJECT}-worktrees/e2e-git-nothing"
assert_file_contains "$WT_NOTHING/git-nothing.txt" "git nothing"
if git_log_contains "$WT_NOTHING" "agent: git nothing"; then
  echo "  ok    nothing worktree contains the agent commit"
else
  echo "  BAD   nothing worktree is missing the agent commit"
  fail=1
fi

echo
if [ "$fail" = 0 ]; then
  KEEP=1
  cat <<EOF
PASS

Test repository kept for inspection:
  $TMP_PROJECT

Remove when done:
  rm -rf "$TMP_PROJECT" "${TMP_PROJECT}-worktrees"
EOF
  exit 0
fi

KEEP=1
echo "FAIL - test repository kept for debugging: $TMP_PROJECT"
exit 1
