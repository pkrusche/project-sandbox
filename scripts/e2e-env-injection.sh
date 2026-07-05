#!/usr/bin/env bash
# End-to-end API-key env injection verification for headless bash-agent sessions.
#
# Creates a throwaway project, injects one variable from --api-key-env-file and
# one from --api-key-env, then verifies the sandboxed bash agent sees both exact
# values and can write them back to the workspace.
#
# Usage:
#   scripts/e2e-env-injection.sh [--runtime chroot|auto|apple-container|docker|podman]
#                                [--base-image IMAGE] [--no-build] [--keep]
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

usage() { sed -n '2,10p' "$0"; }

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
# throwaway project under the repo because Apple's build VM cannot read the
# macOS per-user temp directory ($TMPDIR, /var/folders/...).
if [ "$RUNTIME" = chroot ]; then
  TMP_BASE="${TMPDIR:-/tmp}/project-sandbox-e2e"
else
  TMP_BASE="$ROOT/.project-sandbox/e2e"
fi
mkdir -p "$TMP_BASE"
TMP_PROJECT="$(mktemp -d "$TMP_BASE/env-e2e.XXXXXX")"
cleanup() {
  if [ "$KEEP" = 0 ]; then
    rm -rf "$TMP_PROJECT"
  fi
}
trap cleanup EXIT

fail=0
ok()  { echo "  ok    $*"; }
bad() { echo "  BAD   $*"; fail=1; }

assert_file_equals() {
  local file="$1"
  local expected="$2"
  if [ ! -f "$file" ]; then
    bad "$file was not written"
    return
  fi
  local actual
  actual="$(cat "$file")"
  if [ "$actual" = "$expected" ]; then
    ok "$file contains expected value"
  else
    bad "$file contained '$actual' (expected '$expected')"
  fi
}

DIRECT_ENV_NAME="PROJECT_SANDBOX_E2E_DIRECT_API_KEY"
DIRECT_ENV_VALUE="direct-secret-value"
FILE_ENV_NAME="PROJECT_SANDBOX_E2E_FILE_API_KEY"
FILE_ENV_VALUE="file-secret-value"
HOST_ENV_FILE="$TMP_PROJECT/api-keys.env"
DIRECT_OUT="$TMP_PROJECT/direct-env.txt"
FILE_OUT="$TMP_PROJECT/file-env.txt"

printf "# staged for e2e\n%s=%s\n" \
  "$FILE_ENV_NAME" "$FILE_ENV_VALUE" > "$HOST_ENV_FILE"

printf "# env injection e2e\n" > "$TMP_PROJECT/README.md"

prompt=$(
  printf "set -euo pipefail\n"
  printf '[ "${%s:-}" = %q ]\n' "$DIRECT_ENV_NAME" "$DIRECT_ENV_VALUE"
  printf '[ "${%s:-}" = %q ]\n' "$FILE_ENV_NAME" "$FILE_ENV_VALUE"
  printf "printf '%%s\\n' \"\$%s\" > %q\n" "$DIRECT_ENV_NAME" "$(basename "$DIRECT_OUT")"
  printf "printf '%%s\\n' \"\$%s\" > %q\n" "$FILE_ENV_NAME" "$(basename "$FILE_OUT")"
)

cmd=(
  uv run project-sandbox
  --runtime "$RUNTIME"
  --agent bash
  --prompt-text "$prompt"
  --no-forward-credentials
  --no-firewall
  --verbose
  --api-key-env-file "$HOST_ENV_FILE"
  --api-key-env "$DIRECT_ENV_NAME"
)
if [ "$NO_BUILD" = 1 ]; then
  cmd+=(--no-build)
fi
cmd+=("$TMP_PROJECT" "$BASE_IMAGE")

echo "Test project: $TMP_PROJECT"
echo "Configuration: runtime=$RUNTIME requested_runtime=$REQUESTED_RUNTIME base_image=$BASE_IMAGE no_build=$NO_BUILD"
echo
echo "Running env injection check"
if (
  export "$DIRECT_ENV_NAME=$DIRECT_ENV_VALUE"
  cd "$ROOT"
  "${cmd[@]}"
); then
  ok "sandbox session completed"
else
  bad "sandbox session failed"
fi

assert_file_equals "$DIRECT_OUT" "$DIRECT_ENV_VALUE"
assert_file_equals "$FILE_OUT" "$FILE_ENV_VALUE"

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
