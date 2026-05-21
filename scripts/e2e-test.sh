#!/usr/bin/env bash
# End-to-end smoke test for project-sandbox.
#
# Creates a throwaway Python hello-world project, runs the tool against it,
# and verifies that every expected artefact was written. By default this passes
# --no-build so the test is portable to hosts without apple/container installed;
# pass --with-container to additionally exercise the image build (requires the
# `container` CLI on PATH and a running container system).
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
PROJECT_KEPT=0
cleanup() {
  if [ "$PROJECT_KEPT" = 0 ]; then
    rm -rf "$TMP_PROJECT"
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
echo

cd "$ROOT"
if [ "$WITH_CONTAINER" = 1 ]; then
  echo "Running: uv run project-sandbox $TMP_PROJECT python:3.12-slim"
  uv run project-sandbox "$TMP_PROJECT" python:3.12-slim
else
  echo "Running: uv run project-sandbox --no-build $TMP_PROJECT python:3.12-slim"
  uv run project-sandbox --no-build "$TMP_PROJECT" python:3.12-slim
fi
echo

PS="$TMP_PROJECT/.project-sandbox"
DC="$TMP_PROJECT/.devcontainer"

REQUIRED=(
  "$PS/Dockerfile"
  "$PS/entrypoint.sh"
  "$PS/init-firewall.sh"
  "$PS/project-sandbox-devcontainer-init"
  "$PS/.gitignore"
  "$PS/claude/settings.json"
  "$PS/codex/config.toml"
  "$DC/devcontainer.json"
  "$TMP_PROJECT/.gitignore"
)

SYMLINKS=(
  "$DC/Dockerfile"
  "$DC/init-firewall.sh"
  "$DC/claude"
  "$DC/codex"
)

fail=0
echo "Checking required files:"
for f in "${REQUIRED[@]}"; do
  if [ -e "$f" ]; then
    echo "  ok    $f"
  else
    echo "  MISS  $f"
    fail=1
  fi
done

echo
echo "Checking devcontainer symlinks:"
for s in "${SYMLINKS[@]}"; do
  if [ -L "$s" ]; then
    target="$(readlink "$s")"
    case "$target" in
      ../.project-sandbox/*) echo "  ok    $s -> $target" ;;
      *) echo "  BAD   $s -> $target (expected ../.project-sandbox/...)" ; fail=1 ;;
    esac
  else
    echo "  MISS  $s (not a symlink)"
    fail=1
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

check_contains "$PS/Dockerfile" "FROM python:3.12-slim"
check_contains "$PS/Dockerfile" "useradd -m -u 1000 -g agent -s /bin/bash agent"
check_contains "$PS/Dockerfile" "/usr/local/bin/jj"
check_contains "$PS/init-firewall.sh" "ipset create allowed-ipv4"
check_contains "$PS/claude/settings.json" "bypassPermissions"
check_contains "$PS/codex/config.toml" 'approval_policy = "never"'
check_contains "$PS/entrypoint.sh" "project-sandbox-init-firewall"
check_contains "$PS/entrypoint.sh" "bash-headless"
check_contains "$TMP_PROJECT/.gitignore" "project-sandbox — do not commit agent secrets"

if command -v python3 >/dev/null 2>&1; then
  if python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$DC/devcontainer.json" 2>/dev/null; then
    echo "  ok    $DC/devcontainer.json parses as JSON"
  else
    echo "  BAD   $DC/devcontainer.json is not valid JSON"
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
  ls -la $TMP_PROJECT
  cat $PS/Dockerfile
  cat $DC/devcontainer.json
  cat $PS/init-firewall.sh

Remove when done:
  rm -rf $TMP_PROJECT
EOF
  exit 0
else
  echo "FAIL — see missing/invalid artefacts above."
  echo "Test project (kept for debugging): $TMP_PROJECT"
  PROJECT_KEPT=1
  exit 1
fi
