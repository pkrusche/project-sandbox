#!/usr/bin/env bash
# Build a Vite app and test both Chromium installations through --node-npm twice.
# Usage: scripts/e2e-node-npm.sh [--runtime auto|docker|podman|apple-container] [--keep]
# Requires host npm, uv, a running container runtime, and build-time Internet access.
# Uses a Bash agent; no AI credentials or billable model requests are needed.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME=auto
KEEP=0
usage() { sed -n '2,5p' "$0"; }
while [ $# -gt 0 ]; do
  case "$1" in
    --runtime) RUNTIME="${2:?--runtime needs a value}"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 64 ;;
  esac
done
case "$RUNTIME" in
  auto|docker|podman|apple-container) ;;
  *) echo "ERROR: unsupported runtime: $RUNTIME" >&2; exit 64 ;;
esac
for tool in uv npm; do
  command -v "$tool" >/dev/null 2>&1 || { echo "ERROR: $tool not found." >&2; exit 64; }
done
if [ "$RUNTIME" = auto ]; then
  if [ "$(uname -s)" = Darwin ] && command -v container >/dev/null 2>&1; then
    RUNTIME=apple-container
  elif command -v docker >/dev/null 2>&1; then
    RUNTIME=docker
  elif command -v podman >/dev/null 2>&1; then
    RUNTIME=podman
  else
    echo "ERROR: no supported container runtime found." >&2
    exit 64
  fi
fi
RUNTIME_TOOL="$RUNTIME"
[ "$RUNTIME" != apple-container ] || RUNTIME_TOOL=container
command -v "$RUNTIME_TOOL" >/dev/null 2>&1 || { echo "ERROR: $RUNTIME_TOOL not found." >&2; exit 64; }

# Apple's build VM cannot read the macOS per-user temporary directory.
mkdir -p "$ROOT/.project-sandbox/e2e"
WORKDIR="$(mktemp -d "$ROOT/.project-sandbox/e2e/node-npm.XXXXXX")"
cleanup() {
  local status=$?
  if [ "$KEEP" = 1 ] || [ "$status" != 0 ]; then
    echo "Workdir retained: $WORKDIR"
  else
    rm -rf "$WORKDIR"
  fi
}
trap cleanup EXIT
echo "Workdir: $WORKDIR"
echo "Runtime: $RUNTIME"

cat > "$WORKDIR/package.json" <<'JSON'
{
  "name": "project-sandbox-node-npm-e2e",
  "version": "1.0.0",
  "private": true,
  "type": "module",
  "scripts": {
    "start": "vite --host 127.0.0.1 --port 5173 --strictPort",
    "build": "vite build",
    "test:e2e": "playwright test"
  }
}
JSON
# Resolve and lock the dependency on the host without installing host binaries.
(cd "$WORKDIR" && npm install --package-lock-only --ignore-scripts --save-dev --save-exact @playwright/test vite lodash-es)

cat > "$WORKDIR/index.html" <<'HTML'
<!doctype html><html><head><title>Sandbox smoke test</title>
<link rel="icon" href="data:,"></head>
<body><h1>Node/npm sandbox</h1><button id="increment">Increment</button>
<output id="count">0</output><script type="module" src="/src/main.js"></script>
</body></html>
HTML

mkdir -p "$WORKDIR/src"
cat > "$WORKDIR/src/main.js" <<'JS'
import { sum } from 'lodash-es';

let count = 0;
document.querySelector('#increment').addEventListener('click', () => {
  count = sum([count, 1]);
  document.querySelector('#count').textContent = String(count);
});
JS

cat > "$WORKDIR/vite.config.js" <<'JS'
import { defineConfig } from 'vite';

export default defineConfig({
  // Exercise actual dependency pre-bundling and writes to node_modules/.vite.
  optimizeDeps: { include: ['lodash-es'] },
});
JS

cat > "$WORKDIR/playwright.config.js" <<'JS'
import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests/e2e',
  outputDir: './test-results',
  workers: 1,
  reporter: 'list',
  use: {
    browserName: 'chromium',
    headless: true,
    baseURL: 'http://127.0.0.1:5173',
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  projects: [
    {
      name: 'system-chromium',
      use: {
        launchOptions: {
          executablePath: '/usr/bin/chromium',
          args: ['--no-sandbox'],
        },
      },
    },
    {
      name: 'playwright-chromium',
      // No executablePath override: use the image's locked Playwright browser.
      use: { launchOptions: { args: ['--no-sandbox'] } },
    },
  ],
  webServer: {
    command: 'npm start',
    url: 'http://127.0.0.1:5173',
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
JS

mkdir -p "$WORKDIR/tests/e2e"
cat > "$WORKDIR/tests/e2e/home.spec.js" <<'JS'
import { test, expect } from '@playwright/test';

test('page renders and responds to clicks', async ({ page }, testInfo) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => {
    if (message.type() === 'error') errors.push(message.text());
  });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Node/npm sandbox' })).toBeVisible();
  await page.getByRole('button', { name: 'Increment' }).click();
  await expect(page.locator('#count')).toHaveText('1');
  await page.screenshot({ path: testInfo.outputPath('home.png') });
  expect(errors).toEqual([]);
});
JS

# A host-only sentinel proves the mount hides host dependencies and writes
# made inside the container do not reach the host directory.
mkdir -p "$WORKDIR/node_modules"
printf 'host-only\n' > "$WORKDIR/node_modules/host-sentinel"
cat > "$WORKDIR/run-smoke.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
test "$(id -u)" != 0
test "$(stat -f -c %T /workspace/node_modules)" = tmpfs
mountpoint -q /workspace/node_modules
test ! -e node_modules/host-sentinel
test ! -e node_modules/session-marker
test ! -e node_modules/.vite
test -x /usr/bin/chromium
test -x node_modules/.bin/playwright
test -x node_modules/.bin/vite
test "${PROJECT_SANDBOX_NO_FIREWALL:-0}" != 1
test "$(stat -f -c %T /dev/shm)" = tmpfs
read -r shm_block_size shm_blocks < <(stat -f -c '%S %b' /dev/shm)
shm_bytes=$((shm_block_size * shm_blocks))
if [ "$shm_bytes" != "$2" ]; then
  echo "ERROR: /dev/shm is $shm_bytes bytes; expected $2 bytes." >&2
  exit 1
fi
echo "Verified /dev/shm: $shm_bytes bytes"
node --input-type=module <<'JS'
import assert from 'node:assert/strict';
import { accessSync, constants } from 'node:fs';
import { chromium } from '@playwright/test';

assert.equal(process.env.PLAYWRIGHT_BROWSERS_PATH, '/opt/ms-playwright');
assert.ok(chromium.executablePath().startsWith('/opt/ms-playwright/'));
accessSync(chromium.executablePath(), constants.X_OK);
JS
npm run build
test -s dist/index.html
test -n "$(find dist/assets -type f -name '*.js' -size +0c -print -quit)"
npm run test:e2e
# These must be emitted by Vite, not created manually by the smoke script.
test -s node_modules/.vite/deps/_metadata.json
test -n "$(find node_modules/.vite/deps -type f -name '*.js' -size +0c -print -quit)"
mkdir -p node_modules/.cache
printf 'container-only\n' > node_modules/.cache/smoke
touch node_modules/session-marker
printf 'passed\n' > "$1"
SH

cd "$ROOT"
for session in 1 2; do
  # First exercise the flavor's default; then verify an explicit override.
  SANDBOX_ARGS=(--node-npm --runtime "$RUNTIME")
  EXPECTED_SHM_BYTES=2147483648
  if [ "$session" = 2 ]; then
    SANDBOX_ARGS+=(--shm-size 1g)
    EXPECTED_SHM_BYTES=1073741824
  fi
  echo "Session $session: build Vite and test both Chromium installations with the firewall enabled"
  uv run project-sandbox "${SANDBOX_ARGS[@]}" \
    --agent bash --no-forward-credentials --timeout 300 \
    --prompt-text "bash /workspace/run-smoke.sh session-$session.txt $EXPECTED_SHM_BYTES" "$WORKDIR"
  test "$(cat "$WORKDIR/session-$session.txt")" = passed
  test "$(cat "$WORKDIR/node_modules/host-sentinel")" = host-only
  test "$(find "$WORKDIR/node_modules" -mindepth 1 -maxdepth 1 | wc -l | tr -d ' ')" = 1
done
echo "PASS: default/overridden shared memory, Vite build/cache writes, both Chromium installations, host isolation, and fresh dependencies in two sessions."
