#!/usr/bin/env bash
# End-to-end host lock verification with real project-sandbox processes.
#
# Runs bash-agent sessions against a throwaway git repository and a private
# XDG_STATE_HOME, and verifies that host locks live in the private state root
# and actually serialize real CLI runs: a launch waits on a held build lock, a
# second session on a leased branch is refused, an unsafe or symlinked locks
# directory is refused, a sandbox cannot mount or see the locks, and no lock
# file is created in the system temp dir.
#
# Requirements: uv and git, plus a container runtime (or Linux unshare for chroot).
#
# Usage:
#   scripts/e2e-host-locks.sh [--runtime chroot|auto|apple-container|docker|podman]
#                             [--base-image IMAGE] [--no-build] [--keep] [--wait SECONDS]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

RUNTIME="auto"
BASE_IMAGE="python:3.12-slim"
NO_BUILD=0
KEEP=0
# How long a launch must stay blocked behind a held lock to count as waiting.
WAIT=8

usage() { sed -n '2,15p' "$0"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --runtime) RUNTIME="${2:?--runtime needs a value}"; shift 2 ;;
    --base-image) BASE_IMAGE="${2:?--base-image needs a value}"; shift 2 ;;
    --no-build) NO_BUILD=1; shift ;;
    --keep) KEEP=1; shift ;;
    --wait) WAIT="${2:?--wait needs a value}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 64 ;;
  esac
done

case "$WAIT" in
  ''|*[!0-9]*) echo "ERROR: --wait must be a whole number of seconds" >&2; exit 64 ;;
esac

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

# Same placement rule as the git/jj workflows: Apple's build VM cannot read the
# macOS per-user temp directory, so container runtimes work under the project.
# The private state root lives next to the repository so session inputs stay
# mountable. Create everything owner-only: a group-writable ancestor of the
# state root is (rightly) refused as unsafe.
if [ "$RUNTIME" = chroot ]; then
  TMP_BASE="${TMPDIR:-/tmp}/project-sandbox-e2e"
else
  TMP_BASE="$ROOT/.project-sandbox/e2e"
fi
(umask 077 && mkdir -p "$TMP_BASE")
RUN_DIR="$(mktemp -d "$TMP_BASE/host-locks-e2e.XXXXXX")"
PROJECT="$RUN_DIR/project"
STATE="$RUN_DIR/state"
LOGS="$RUN_DIR/logs"
mkdir -m 700 "$PROJECT" "$STATE" "$LOGS"
LOCKS="$STATE/project-sandbox/locks"
WORKTREES="${PROJECT}-worktrees"

BG_PIDS=""
cleanup() {
  local pid
  for pid in $BG_PIDS; do
    kill "$pid" 2>/dev/null || true
  done
  # Undo a failed check's tampering so cleanup (or --keep inspection) works.
  [ -d "$LOCKS" ] && [ ! -L "$LOCKS" ] && chmod 700 "$LOCKS" 2>/dev/null || true
  if [ "$KEEP" = 0 ]; then
    rm -rf "$RUN_DIR"
  fi
}
trap cleanup EXIT

fail=0
ok()  { echo "  ok    $*"; }
bad() { echo "  BAD   $*"; fail=1; }

# Octal permission bits, portable across GNU and BSD stat.
perm() { stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1"; }

show_log() {
  echo "  ----- last lines of $1"
  tail -n 40 "$1" | sed 's/^/  | /'
  echo "  -----"
}

host_python() {
  (cd "$ROOT" && XDG_STATE_HOME="$STATE" uv run python -I "$@")
}

# Run one bash-agent session; output goes to $LOGS/<name>.log.
NO_BUILD_ARGS=()
[ "$NO_BUILD" = 1 ] && NO_BUILD_ARGS=(--no-build)
sandbox() {
  local name="$1"
  shift
  (
    cd "$ROOT" && XDG_STATE_HOME="$STATE" uv run project-sandbox \
      --runtime "$RUNTIME" \
      --agent bash \
      --no-forward-credentials \
      --no-firewall \
      --verbose \
      ${NO_BUILD_ARGS[@]+"${NO_BUILD_ARGS[@]}"} \
      "$@" \
      "$PROJECT" "$BASE_IMAGE"
  ) >"$LOGS/$name.log" 2>&1
}

# Start a session in the background; sets BG_PID.
sandbox_bg() {
  sandbox "$@" &
  BG_PID=$!
  BG_PIDS="$BG_PIDS $BG_PID"
}

# wait_until SECONDS COMMAND... : poll once a second until COMMAND succeeds.
wait_until() {
  local deadline=$((SECONDS + $1))
  shift
  until "$@"; do
    [ "$SECONDS" -ge "$deadline" ] && return 1
    sleep 1
  done
}

running() { kill -0 "$1" 2>/dev/null; }
not_running() { ! running "$1"; }

# Holds a real host lock through project_sandbox.host_locks (same code and
# XDG_STATE_HOME as the CLI) until a release file appears.
HOLDER="$RUN_DIR/hold-lock.py"
cat >"$HOLDER" <<'EOF'
import sys
import time
from pathlib import Path

from project_sandbox import host_locks

kind, target, ready, release, limit = sys.argv[1:6]
with host_locks.path_lock(kind, Path(target)):
    Path(ready).touch()
    deadline = time.monotonic() + float(limit)
    while not Path(release).exists() and time.monotonic() < deadline:
        time.sleep(0.2)
EOF

temp_locks() {
  local dir
  dir="$(host_python -c 'import tempfile; print(tempfile.gettempdir())')"
  # Plain glob, not ls|grep, so no match is just an empty list.
  for f in "$dir"/project-sandbox-*.lock; do
    if [ -e "$f" ] || [ -L "$f" ]; then echo "$f"; fi
  done | sort
}

echo "Run directory:  $RUN_DIR"
echo "State root:     $STATE"
echo "Configuration:  runtime=$RUNTIME requested_runtime=$REQUESTED_RUNTIME base_image=$BASE_IMAGE no_build=$NO_BUILD wait=${WAIT}s"

git -C "$PROJECT" init -q
git -C "$PROJECT" config user.name "Project Sandbox E2E"
git -C "$PROJECT" config user.email "project-sandbox-e2e@example.invalid"
printf "base\n" >"$PROJECT/README.md"
git -C "$PROJECT" add README.md
git -C "$PROJECT" commit -qm "initial commit"

TEMP_BEFORE="$(temp_locks)"

# 1. A first session creates the private locks directory (and builds the image
#    the later checks reuse).
echo
echo "Check 1: locks are created in the private state root"
if sandbox warmup --prompt-text "true"; then
  ok "first session succeeded"
else
  bad "first session failed"
  show_log "$LOGS/warmup.log"
  echo
  echo "FAIL - cannot continue without a working session (logs: $LOGS; use --keep to retain)"
  exit 1
fi
if [ -d "$LOCKS" ] && [ ! -L "$LOCKS" ]; then
  ok "locks directory exists: $LOCKS"
  [ "$(perm "$LOCKS")" = 700 ] && ok "locks directory mode is 0700" \
    || bad "locks directory mode is $(perm "$LOCKS"), expected 700"
else
  bad "locks directory missing: $LOCKS"
fi
BUILD_LOCK="$(host_python -c 'import sys; from pathlib import Path; from project_sandbox import host_locks; print(host_locks.lock_path("build", Path(sys.argv[1])))' "$PROJECT/.project-sandbox")"
# The path comes from the code under test, so also pin where it must be.
if [ "$(dirname "$BUILD_LOCK")" != "$LOCKS" ]; then
  bad "build lock path is outside the locks directory: $BUILD_LOCK"
elif [ -f "$BUILD_LOCK" ]; then
  ok "build lock is a file in the locks directory: $(basename "$BUILD_LOCK")"
  [ "$(perm "$BUILD_LOCK")" = 600 ] && ok "build lock mode is 0600" \
    || bad "build lock mode is $(perm "$BUILD_LOCK"), expected 600"
else
  bad "build lock not found at $BUILD_LOCK"
fi
if [ "$RUNTIME" != chroot ]; then
  if compgen -G "$LOCKS/project-sandbox-image-*.lock" >/dev/null; then
    ok "runtime image-store lock is in the locks directory"
  else
    bad "no project-sandbox-image-*.lock in $LOCKS"
  fi
fi

# 2. A launch blocks while another process holds the project's build lock, and
#    proceeds once it is released. This only works if the CLI and the holder
#    agree on the lock location.
echo
echo "Check 2: a launch waits for a held build lock"
rm -f "$RUN_DIR/held" "$RUN_DIR/release" "$PROJECT/after-lock.txt"
host_python "$HOLDER" build "$PROJECT/.project-sandbox" \
  "$RUN_DIR/held" "$RUN_DIR/release" 600 &
HOLDER_PID=$!
BG_PIDS="$BG_PIDS $HOLDER_PID"
if wait_until 60 test -e "$RUN_DIR/held"; then
  ok "holder process took the build lock"
  sandbox_bg blocked --prompt-text "echo ran > /workspace/after-lock.txt"
  BLOCKED_PID=$BG_PID
  sleep "$WAIT"
  if running "$BLOCKED_PID" && [ ! -e "$PROJECT/after-lock.txt" ]; then
    ok "launch was still waiting after ${WAIT}s and its agent had not run"
  else
    bad "launch did not wait for the held build lock"
    show_log "$LOGS/blocked.log"
  fi
  touch "$RUN_DIR/release"
  wait "$HOLDER_PID" || true
  if wait_until 900 not_running "$BLOCKED_PID" && wait "$BLOCKED_PID"; then
    ok "launch completed after the lock was released"
    [ -e "$PROJECT/after-lock.txt" ] && ok "its agent ran" || bad "its agent did not run"
  else
    bad "launch failed or did not finish after the lock was released"
    show_log "$LOGS/blocked.log"
  fi
else
  bad "holder process could not take the build lock"
  kill "$HOLDER_PID" 2>/dev/null || true
fi
rm -f "$PROJECT/after-lock.txt"

# 3. A live --branch session leases its branch; a second session on the same
#    branch is refused immediately, and the lease ends with the session.
echo
echo "Check 3: a leased branch refuses a second session"
LEASE_PROMPT='for i in $(seq 600); do [ -e /workspace/.release ] && break; sleep 1; done; rm -f /workspace/.release'
sandbox_bg lease-owner --branch e2e-lease --prompt-text "$LEASE_PROMPT"
OWNER_PID=$BG_PID
if wait_until 900 test -d "$WORKTREES/e2e-lease"; then
  ok "first session holds branch e2e-lease"
  START=$SECONDS
  if sandbox lease-contender --branch e2e-lease --prompt-text "true"; then
    bad "second session on the leased branch was allowed"
  elif grep -q "already in use" "$LOGS/lease-contender.log"; then
    ok "second session was refused ($((SECONDS - START))s): branch already in use"
  else
    bad "second session failed for another reason"
    show_log "$LOGS/lease-contender.log"
  fi
  touch "$WORKTREES/e2e-lease/.release"
else
  bad "first session never created its worktree"
  show_log "$LOGS/lease-owner.log"
  kill "$OWNER_PID" 2>/dev/null || true
fi
if wait_until 300 not_running "$OWNER_PID" && wait "$OWNER_PID"; then
  ok "first session finished"
  if sandbox lease-after --branch e2e-lease --prompt-text "true"; then
    ok "branch can be used again once the session ended"
  else
    bad "branch still refused after its session ended"
    show_log "$LOGS/lease-after.log"
  fi
else
  bad "first session failed"
  show_log "$LOGS/lease-owner.log"
fi

# 4. A sandbox can neither mount the locks directory nor see it.
echo
echo "Check 4: sandboxes cannot reach the locks"
if sandbox mount-locks --mount "type=bind,source=$LOCKS,target=/locks" --prompt-text "true"; then
  bad "a --mount of the locks directory was accepted"
elif grep -q "overlaps" "$LOGS/mount-locks.log"; then
  ok "a --mount of the locks directory was refused"
else
  bad "the --mount was refused for another reason"
  show_log "$LOGS/mount-locks.log"
fi
VISIBILITY_PROMPT="$(printf 'if [ -e %q ] || grep -q project-sandbox/locks /proc/self/mountinfo 2>/dev/null; then echo visible; else echo hidden; fi > /workspace/locks-visibility.txt' "$LOCKS")"
if sandbox visibility --prompt-text "$VISIBILITY_PROMPT" \
  && [ "$(cat "$PROJECT/locks-visibility.txt" 2>/dev/null)" = hidden ]; then
  ok "the locks directory is not visible inside the sandbox"
else
  bad "the locks directory is visible inside the sandbox (or the session failed)"
  show_log "$LOGS/visibility.log"
fi
rm -f "$PROJECT/locks-visibility.txt"

# 5. An unsafe locks directory stops the launch with a clear error instead of
#    falling back to a shared location.
echo
echo "Check 5: an unsafe locks directory is refused"
if [ ! -d "$LOCKS" ] || [ -L "$LOCKS" ]; then
  bad "skipped: no real locks directory at $LOCKS to tamper with"
else
  chmod 770 "$LOCKS"
  if sandbox shared-dir --prompt-text "true"; then
    bad "launch proceeded with a group-accessible locks directory"
  elif grep -q "Could not open the build lock" "$LOGS/shared-dir.log" \
    && grep -qF "$LOCKS" "$LOGS/shared-dir.log"; then
    ok "group-accessible locks directory was refused, naming it"
    [ "$(perm "$LOCKS")" = 770 ] && ok "its permissions were left alone" \
      || bad "its permissions were changed to $(perm "$LOCKS")"
  else
    bad "launch failed for another reason"
    show_log "$LOGS/shared-dir.log"
  fi
  chmod 700 "$LOCKS"

  mv "$LOCKS" "$LOCKS.real"
  ln -s "$LOCKS.real" "$LOCKS"
  REAL_BEFORE="$(ls -A "$LOCKS.real")"
  if sandbox symlink-dir --prompt-text "true"; then
    bad "launch proceeded with a symlinked locks directory"
  elif grep -q "Could not open the build lock" "$LOGS/symlink-dir.log"; then
    ok "symlinked locks directory was refused"
    [ "$(ls -A "$LOCKS.real")" = "$REAL_BEFORE" ] && ok "nothing was created through the symlink" \
      || bad "files were created through the symlink"
  else
    bad "launch failed for another reason"
    show_log "$LOGS/symlink-dir.log"
  fi
  rm "$LOCKS"
  mv "$LOCKS.real" "$LOCKS"
fi

# 6. None of the above created a lock in the shared temp dir.
echo
echo "Check 6: nothing was locked in the system temp dir"
TEMP_AFTER="$(temp_locks)"
NEW_TEMP="$(comm -13 <(printf '%s\n' "$TEMP_BEFORE") <(printf '%s\n' "$TEMP_AFTER") | sed '/^$/d')"
if [ -z "$NEW_TEMP" ]; then
  ok "no new project-sandbox-*.lock in the temp dir"
else
  bad "new lock files in the temp dir:"
  printf '          %s\n' $NEW_TEMP
fi

echo
if [ "$fail" = 0 ]; then
  echo "PASS"
  if [ "$KEEP" = 1 ]; then
    printf '\nRun directory kept for inspection:\n  %s\n\nRemove when done:\n  rm -rf "%s"\n' "$RUN_DIR" "$RUN_DIR"
  fi
  exit 0
fi
echo "Locks directory contents:"
ls -la "$LOCKS" 2>/dev/null | sed 's/^/  /' || true
if [ "$KEEP" = 1 ]; then
  echo "FAIL - run directory kept for debugging: $RUN_DIR"
else
  echo "FAIL - run directory will be removed (use --keep to retain it): $RUN_DIR"
fi
exit 1
