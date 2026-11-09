#!/usr/bin/env bash
# End-to-end verification of isolated VCS sessions (headless bash agent).
#
# Creates a throwaway colocated jj repository with an ignored data/ folder, then
# runs sandboxed sessions against it and checks, on the host:
#   1. a session that edits, adds and deletes files, commits with jj and git,
#      plants .git/hooks/pre-commit and .git/config entries in its private
#      repository, reads a --share folder and fails to write it, leaves exactly
#      one commit on the bookmark; the agent's commit message, hooks and config
#      never reach the main repository, and the session directory and private
#      repository are removed;
#   2. a second session on the same bookmark (recreated directories) continues
#      from the first session's result;
#   3. a session whose container is killed by --timeout still has its files
#      recorded on the bookmark, and its session directory is kept.
#
# Requirements: uv and jj on the host. Container runtimes are optional.
#
# Usage:
#   scripts/e2e-isolated-vcs.sh [--runtime chroot|auto|apple-container|docker|podman]
#                               [--base-image IMAGE] [--no-build] [--keep]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

RUNTIME="auto"
BASE_IMAGE="python:3.12-slim"
NO_BUILD=0
KEEP=0

usage() { sed -n '2,21p' "$0"; }

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

# The chroot jail only exposes the host's /usr, /bin, /lib* and /etc, so a jj
# installed elsewhere is invisible to the agent. Bind the host binary read-only
# to a fixed jail path and put that on the agent's PATH. Chroot is a
# workflow-test runtime, not a security boundary.
EXTRA_ARGS=()
PROMPT_PREFIX=""
if [ "$RUNTIME" = chroot ]; then
  JJ_HOST_BIN="$(readlink -f "$(command -v jj)")"
  case "$JJ_HOST_BIN" in
    /usr/*|/bin/*|/lib/*|/lib64/*|/etc/*) ;;
    *,*)
      echo "ERROR: jj path contains a comma and cannot be passed to --mount: $JJ_HOST_BIN" >&2
      exit 64
      ;;
    *)
      JJ_JAIL_DIR=/opt/project-sandbox-e2e/bin
      EXTRA_ARGS=(--mount "type=bind,source=$JJ_HOST_BIN,target=$JJ_JAIL_DIR/jj,readonly")
      PROMPT_PREFIX="export PATH=$JJ_JAIL_DIR:\$PATH"
      ;;
  esac
fi

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

# Container runtimes keep the throwaway repo under the project because Apple's
# build VM cannot read the macOS per-user temp directory ($TMPDIR, /var/folders).
if [ "$RUNTIME" = chroot ]; then
  TMP_BASE="${TMPDIR:-/tmp}/project-sandbox-e2e"
else
  TMP_BASE="$ROOT/.project-sandbox/e2e"
fi
mkdir -p "$TMP_BASE"
TMP_PROJECT="$(mktemp -d "$TMP_BASE/isolated-e2e.XXXXXX")"
WS_ROOT="${TMP_PROJECT}-workspaces"
cleanup() {
  if [ "$KEEP" = 0 ]; then
    rm -rf "$TMP_PROJECT" "$WS_ROOT"
  fi
}
trap cleanup EXIT

fail=0
ok() { echo "  ok    $*"; }
bad() { echo "  BAD   $*"; fail=1; }

# run_session BOOKMARK PROMPT [extra project-sandbox args...]
run_session() {
  local bookmark="$1" prompt="$2"
  shift 2
  local cmd=(
    uv run project-sandbox
    --runtime "$RUNTIME"
    --agent bash
    --prompt-text "$prompt"
    --branch "$bookmark"
    --share data
    --no-forward-credentials
    --no-firewall
    --log "$TMP_BASE/$(basename "$TMP_PROJECT")-$bookmark.log"
    ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}
    "$@"
  )
  if [ "$NO_BUILD" = 1 ]; then
    cmd+=(--no-build)
  fi
  cmd+=("$TMP_PROJECT" "$BASE_IMAGE")
  echo "Running isolated session on bookmark $bookmark"
  (cd "$ROOT" && "${cmd[@]}")
}

session_log() { cat "$TMP_BASE/$(basename "$TMP_PROJECT")-$1.log" 2>/dev/null || true; }

hash_tree() { (cd "$1" && find . -type f -exec shasum {} + 2>/dev/null | sort | shasum); }

echo "Test jj repo: $TMP_PROJECT"
echo "Configuration: runtime=$RUNTIME requested_runtime=$REQUESTED_RUNTIME base_image=$BASE_IMAGE no_build=$NO_BUILD"
jj git init --colocate "$TMP_PROJECT" >/dev/null 2>&1
jj -R "$TMP_PROJECT" config set --repo user.name "Project Sandbox E2E"
jj -R "$TMP_PROJECT" config set --repo user.email "project-sandbox-e2e@example.invalid"
printf 'base\n' > "$TMP_PROJECT/README.md"
printf 'old\n' > "$TMP_PROJECT/obsolete.txt"
printf 'data/\n.project-sandbox/\n' > "$TMP_PROJECT/.gitignore"
mkdir "$TMP_PROJECT/data"
printf 'a,b\n1,2\n' > "$TMP_PROJECT/data/in.csv"
jj -R "$TMP_PROJECT" commit -m "initial commit" >/dev/null 2>&1
MAIN_GIT_CONFIG="$(shasum < "$TMP_PROJECT/.git/config")"
MAIN_HOOKS="$(hash_tree "$TMP_PROJECT/.git/hooks")"

# --- 1. Edit, commit, plant metadata, use the share --------------------------
PROMPT1=$(cat <<EOF
set -euo pipefail
${PROMPT_PREFIX}
cd /workspace
test -d .jj/repo || { echo "NO PRIVATE STORE"; exit 1; }
test -z "\$(jj diff --summary)" || { echo "NOT CLEAN AT START"; exit 1; }
printf 'changed by agent\n' > README.md
printf 'new\n' > added.txt
rm obsolete.txt
jj diff --summary
git status --short
jj commit -m "agent private commit"
git log --oneline -3
mkdir -p .git/hooks
printf '#!/bin/sh\ntouch /tmp/pwned\n' > .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
git config core.fsmonitor /tmp/evil
cat data/in.csv
if ( echo x > data/written-by-agent.txt ) 2>/dev/null; then echo "SHARE WAS WRITABLE"; exit 1; fi
test -z "\$(git status --porcelain -- data)" || { echo "SHARE SHOWN AS NEW"; exit 1; }
echo AGENT-DONE
EOF
)
echo
if run_session e2e-iso "$PROMPT1"; then ok "session 1 exited 0"; else bad "session 1 failed"; session_log e2e-iso; fi
if session_log e2e-iso | grep -q AGENT-DONE; then ok "agent ran jj, git and the share checks"; else bad "agent checks did not complete"; session_log e2e-iso | tail -20; fi
SUMMARY="$(cd "$TMP_PROJECT" && jj diff --summary -r e2e-iso 2>&1 | sort)"
EXPECTED="$(printf 'A added.txt\nD obsolete.txt\nM README.md\n' | sort)"
if [ "$SUMMARY" = "$EXPECTED" ]; then ok "bookmark holds exactly the agent's changes"; else bad "unexpected bookmark diff: $SUMMARY"; fi
PARENT_DESC="$(jj -R "$TMP_PROJECT" log --no-graph -r 'e2e-iso-' -T description)"
if [ "$PARENT_DESC" = "initial commit"$'\n' ] || [ "$PARENT_DESC" = "initial commit" ]; then ok "result is one commit on the start revision"; else bad "result parent is not the start revision: $PARENT_DESC"; fi
if jj -R "$TMP_PROJECT" log --no-graph -r 'all()' -T description | grep -q "agent private commit"; then
  bad "agent's commit message reached the main repository"
else
  ok "agent's commit series stayed private"
fi
[ "$(shasum < "$TMP_PROJECT/.git/config")" = "$MAIN_GIT_CONFIG" ] && ok "main .git/config unchanged" || bad "main .git/config changed"
[ "$(hash_tree "$TMP_PROJECT/.git/hooks")" = "$MAIN_HOOKS" ] && ok "main .git/hooks unchanged" || bad "main .git/hooks changed"
[ ! -e "$TMP_PROJECT/data/written-by-agent.txt" ] && ok "read-only share not written" || bad "read-only share was written"
private_left() { find "$WS_ROOT/.session-vcs" -maxdepth 1 -name "$1.*" ! -name '.*' 2>/dev/null | grep -c . || true; }
[ ! -e "$WS_ROOT/e2e-iso" ] && [ "$(private_left e2e-iso)" = 0 ] && ok "session directory and private repository removed" || bad "session directories left behind"

# --- 2. Same bookmark again: directories are recreated at the same paths -----
PROMPT2=$(cat <<EOF
set -euo pipefail
${PROMPT_PREFIX}
cd /workspace
grep -q 'changed by agent' README.md || { echo "DID NOT START FROM BOOKMARK"; exit 1; }
test -z "\$(jj diff --summary)" || { echo "NOT CLEAN AT START"; exit 1; }
printf 'second\n' > second.txt
git status --short
echo AGENT-DONE
EOF
)
echo
if run_session e2e-iso "$PROMPT2"; then ok "session 2 exited 0"; else bad "session 2 failed"; session_log e2e-iso; fi
if jj -R "$TMP_PROJECT" file show -r e2e-iso root:second.txt 2>/dev/null | grep -q second; then
  ok "second session continued from the bookmark"
else
  bad "second session's file missing on the bookmark"
fi

# --- 3. Killed container: files are still recorded ---------------------------
PROMPT3=$(cat <<EOF
${PROMPT_PREFIX}
cd /workspace
printf 'left behind\n' > killed.txt
sleep 600
EOF
)
echo
if run_session e2e-killed "$PROMPT3" --timeout 45; then
  bad "timed-out session exited 0"
else
  ok "timed-out session exited non-zero"
fi
if jj -R "$TMP_PROJECT" file show -r e2e-killed root:killed.txt 2>/dev/null | grep -q 'left behind'; then
  ok "killed session's files recorded on the bookmark"
else
  bad "killed session's files not recorded"
fi
[ -d "$WS_ROOT/e2e-killed" ] && [ "$(private_left e2e-killed)" = 1 ] && ok "killed session's directory and private repository kept" || bad "killed session's directories removed"

echo
if [ "$fail" = 0 ]; then
  echo "PASS"
  if [ "$KEEP" = 1 ]; then
    printf '\nTest repository kept for inspection:\n  %s\nRemove when done:\n  rm -rf "%s" "%s"\n' \
      "$TMP_PROJECT" "$TMP_PROJECT" "$WS_ROOT"
  fi
  exit 0
fi
if [ "$KEEP" = 1 ]; then
  echo "FAIL - test repository kept for debugging: $TMP_PROJECT"
else
  echo "FAIL - test repository will be removed (use --keep to retain it): $TMP_PROJECT"
fi
exit 1
