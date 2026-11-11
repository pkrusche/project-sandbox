#!/usr/bin/env bash
# Run all project-sandbox end-to-end tests available on this host.
#
# Sequentially executes:
#   1. smoke                    — artifact-generation smoke test (no container needed)
#   2. env-injection            — bash-agent env/API-key forwarding
#   3. git-workflow             — git rebase/merge/nothing workflows
#   4. jj-workflow              — jj rebase/merge/nothing workflows (skipped if jj not on PATH)
#   5. dockerfile-tamper        — Dockerfile integrity and override behavior
#   6. timeout-teardown         — container/VM cleanup after timeout
#   7. python-uv                — --python-uv interpreter/venv usable by the agent
#   8. pi-ollama                — Pi + Ollama networking/config
#   9. internet-proxy-smoke     — availability-gated routing/bypass smoke test
#  10. internet-proxy-isolation — routing/bypass/failure audit (explicit opt-in)
#  11. agent-proxy-isolation    — gateway-only network/credential audit (explicit opt-in)
#  12. agent-proxy              — real Pi/OpenCode gateway calls (explicit opt-in)
#  13. node-npm                 — headless Playwright and ephemeral npm dependencies
#  14. proxy-endpoints          — controlled remote HTTP/TLS fixtures (explicit opt-in)
#  15. build-proxy              — build access through TLS-intercepting Squid (explicit opt-in;
#                                 on Podman the Squid fixture runs on Docker)
#  16. copilot                  — real GitHub Copilot CLI sessions (explicit opt-in; billable)
#  17. copilot-egress           — Copilot CLI egress capture and Copilot-only profile check
#                                 through an intercepting Squid fixture (explicit opt-in; billable)
#  18. egress-sidecar           — --egress sidecar policy, TLS interception, shared sidecar, parent
#                                 chaining and a loopback-only local service (explicit opt-in; needs
#                                 network and builds)
#  19. vcs-access               — full/read-only/hidden metadata mounts with a Bash agent
#  20. firewall                 — firewall sudo revocation in fresh Bash-agent containers
#  21. security                 — TODO verification and hardening release gate (explicit opt-in)
#
# The console shows one progress line per suite. Each suite's output is captured
# in its own temp log file; a failing suite's log is kept and its path reported,
# a passing suite's log is deleted unless --keep is given.
#
# Usage:
#   scripts/run-e2e-tests.sh [--runtime chroot|auto|apple-container|docker|podman]
#                            [--base-image IMAGE] [--no-build] [--keep]
#                            [--only SUITE] [--list] [--plan] [--all-tests]
#                            [--incremental] [--strict-security-coverage]
#                            [--with-agent-proxy] [--with-internet-proxy]
#                            [--with-proxy-endpoints --proxy-fixture-host IP]
#                            [--with-build-proxy --internet-proxy-dir DIR]
#                            [--with-copilot [--copilot-host TENANT.ghe.com]
#                             [--copilot-capture-dir DIR]]
#                            [--with-copilot-egress [--copilot-egress-report-dir DIR]]
#                            [--with-egress-sidecar] [--with-security]
#                            [--proxy-fixture-ipv6-host IP]
#                            [--proxy-fixture-hostname NAME]
#                            [--ca-cert PATH ...]
#
#   --runtime NAME   runtime forwarded to workflow scripts (default: auto)
#   --base-image IMG base image forwarded to workflow scripts (default: python:3.12-slim)
#   --no-build       forward --no-build to workflow scripts
#   --keep           keep temporary directories on failure for debugging, and
#                    keep the captured log of every suite, not just failures
#   --only SUITE     run just one suite by name (default: run every available suite).
#                    Naming an opt-in suite is itself the opt-in, so --only agent-proxy,
#                    --only agent-proxy-isolation, --only internet-proxy-isolation,
#                    --only proxy-endpoints, --only build-proxy, --only copilot,
#                    --only copilot-egress, and --only egress-sidecar need no additional
#                    --with-* flag.
#   --list           print the suite names accepted by --only and exit
#   --incremental    skip suites that already passed for the current source and
#                    configuration, and record each suite that passes now. The
#                    source is the checkout's current file contents (jj working
#                    copy, or git working tree incl. uncommitted edits); the
#                    configuration is the runtime plus every option that changes
#                    how a suite runs (not --only/--with-*/--keep). State lives in
#                    .e2e-state/ (gitignored; PROJECT_SANDBOX_E2E_STATE_DIR
#                    overrides it). Failed and skipped suites always run again.
#                    External checkouts (--internet-proxy-dir, --agentgateway-dir)
#                    and host services are not part of the key
#   --plan           decide every suite as a real run would, print RUN/SKIP/FAIL
#                    per suite and the summary, but execute nothing (no orphan
#                    reaping either); exits 0
#   --all-tests      run every suite, including all opt-in ones (implies
#                    --with-agent-proxy, --with-internet-proxy,
#                    --with-proxy-endpoints, --with-build-proxy, --with-copilot,
#                    --with-copilot-egress, and --with-egress-sidecar). Requires
#                    --internet-proxy-dir and --agentgateway-dir,
#                    a real container runtime (build-proxy on Podman also needs
#                    Docker for its fixture), and builds enabled; billable and destructive
#   --with-agent-proxy
#                    run proxy isolation plus the billable agent checker
#   --with-internet-proxy
#                    run the destructive two-service Internet isolation audit;
#                    also requires --internet-proxy-dir and --agentgateway-dir
#   --blocked-url URL
#                    URL the Internet proxy must deny in the isolation audit
#                    (default: https://example.com)
#   --ca-cert PATH   bake a proxy CA certificate into Internet-proxy test images;
#                    repeat for certificate chains
#   --with-proxy-endpoints
#                    run temporary, non-billable remote HTTP/TLS proxy fixtures;
#                    requires a real runtime and builds
#   --with-build-proxy
#                    run build/CA and runtime isolation checks using IPL Squid;
#                    requires Docker, Apple container, or Podman (plus Docker
#                    for the fixture), --internet-proxy-dir, and builds enabled
#   --with-copilot   run the billable GitHub Copilot CLI checks (scripts/e2e-copilot.py);
#                    needs a Copilot subscription and a token from pass
#                    (copilot-github-token) or `gh auth login`. Without this flag
#                    (or --only copilot / --all-tests) no token lookup happens
#   --copilot-host HOST
#                    also run the Copilot `tenant` check against a <tenant>.ghe.com host
#   --copilot-capture-dir DIR
#                    keep each Copilot check's redacted JSONL stream and exit code in DIR
#   --with-copilot-egress
#                    run scripts/e2e-copilot-egress.py: capture the Copilot CLI's hosts and
#                    paths through an intercepting Squid fixture, verify a Copilot-only
#                    profile, check GitHub repository access is blocked, then repeat the
#                    session and the block probes through the real --egress sidecar with
#                    the Copilot profile on. Billable (a few short Copilot requests); same
#                    token sources as --with-copilot
#   --copilot-egress-report-dir DIR
#                    write the copilot-egress report there (default: .project-sandbox/)
#   --with-security
#                    include the security verification suite alongside other suites;
#                    enabled automatically by the all-runtime runner when builds are enabled.
#                    Known limitations (XFAIL) and missing prerequisites (SKIP) are
#                    listed in its log but do not fail it
#   --strict-security-coverage
#                    release gate: fail the security suite on any XFAIL or SKIP too
#   --security-nat64-origin HOST
#                    controlled origin routed through the deployment NAT64 translator
#   --security-peer-runtime NAME
#                    second live runtime for concurrent policy edits in --only security
#   --security-fixture-host IP
#                    container-reachable host IPv4 for security firewall probes
#   --with-egress-sidecar
#                    run scripts/e2e-egress-sidecar.py: real --egress sidecar sessions graded
#                    from inside the sandbox and from the sidecar log, the intercepted
#                    api.github.com (Copilot profile), parent-proxy chaining through an
#                    intercepting fixture, and a loopback-only local service. Not
#                    billable; needs network access and image builds
#   --proxy-fixture-host IP
#                    override local fixture IP (default: outbound IPv4 source address)
#   --proxy-fixture-ipv6-host IP
#                    optional local IPv6 IP for an additional HTTP gateway check
#   --proxy-fixture-hostname NAME
#                    DNS name resolving to the fixture IP; enables the HTTPS
#                    scenario (HTTPS endpoints must be named, e.g. 192-168-1-10.sslip.io)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

SUITES=(
  smoke
  env-injection
  git-workflow
  jj-workflow
  vcs-access
  firewall
  dockerfile-tamper
  timeout-teardown
  python-uv
  node-npm
  pi-ollama
  internet-proxy-smoke
  internet-proxy-isolation
  agent-proxy-isolation
  agent-proxy
  proxy-endpoints
  build-proxy
  copilot
  copilot-egress
  egress-sidecar
  security
)

RUNTIME="auto"
BASE_IMAGE="python:3.12-slim"
NO_BUILD=0
KEEP=0
ONLY=""
PLAN=0
INCREMENTAL=0
STRICT_SECURITY_COVERAGE=0
WITH_AGENT_PROXY=0
WITH_INTERNET_PROXY=0
WITH_PROXY_ENDPOINTS=0
WITH_BUILD_PROXY=0
WITH_COPILOT=0
COPILOT_HOST=""
COPILOT_CAPTURE_DIR=""
WITH_COPILOT_EGRESS=0
WITH_EGRESS_SIDECAR=0
WITH_SECURITY=0
SECURITY_PEER_RUNTIME=""
SECURITY_FIXTURE_HOST=""
SECURITY_NAT64_ORIGIN=""
COPILOT_EGRESS_REPORT_DIR=""
ALL_TESTS=0
PROXY_FIXTURE_HOST=""
PROXY_FIXTURE_IPV6_HOST=""
PROXY_FIXTURE_HOSTNAME=""
BLOCKED_URL="https://example.com"
INTERNET_PROXY_DIR=""
AGENTGATEWAY_DIR=""
CA_CERTS=()

# Print the leading comment block (everything after the shebang up to the first
# non-comment line) so the header stays the single source of usage truth.
usage() { sed -n '2,${/^#/!q;p;}' "$0"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --security-nat64-origin) SECURITY_NAT64_ORIGIN="${2:?--security-nat64-origin needs a value}"; shift 2 ;;
    --security-peer-runtime) SECURITY_PEER_RUNTIME="${2:?--security-peer-runtime needs a value}"; shift 2 ;;
    --security-fixture-host) SECURITY_FIXTURE_HOST="${2:?--security-fixture-host needs a value}"; shift 2 ;;
    --runtime)    RUNTIME="${2:?--runtime needs a value}"; shift 2 ;;
    --base-image) BASE_IMAGE="${2:?--base-image needs a value}"; shift 2 ;;
    --no-build)   NO_BUILD=1; shift ;;
    --keep)       KEEP=1; shift ;;
    --only)       ONLY="${2:?--only needs a value}"; shift 2 ;;
    --list)       printf '%s\n' "${SUITES[@]}"; exit 0 ;;
    --plan)       PLAN=1; shift ;;
    --incremental) INCREMENTAL=1; shift ;;
    --strict-security-coverage) STRICT_SECURITY_COVERAGE=1; shift ;;
    --all-tests)  ALL_TESTS=1; shift ;;
    --with-agent-proxy) WITH_AGENT_PROXY=1; shift ;;
    --with-internet-proxy) WITH_INTERNET_PROXY=1; shift ;;
    --with-build-proxy) WITH_BUILD_PROXY=1; shift ;;
    --with-proxy-endpoints) WITH_PROXY_ENDPOINTS=1; shift ;;
    --with-copilot) WITH_COPILOT=1; shift ;;
    --copilot-host) COPILOT_HOST="${2:?--copilot-host needs a value}"; shift 2 ;;
    --copilot-capture-dir) COPILOT_CAPTURE_DIR="${2:?--copilot-capture-dir needs a value}"; shift 2 ;;
    --with-copilot-egress) WITH_COPILOT_EGRESS=1; shift ;;
    --with-egress-sidecar) WITH_EGRESS_SIDECAR=1; shift ;;
    --with-security) WITH_SECURITY=1; shift ;;
    --copilot-egress-report-dir) COPILOT_EGRESS_REPORT_DIR="${2:?--copilot-egress-report-dir needs a value}"; shift 2 ;;
    --proxy-fixture-host) PROXY_FIXTURE_HOST="${2:?--proxy-fixture-host needs a value}"; shift 2 ;;
    --proxy-fixture-ipv6-host) PROXY_FIXTURE_IPV6_HOST="${2:?--proxy-fixture-ipv6-host needs a value}"; shift 2 ;;
    --proxy-fixture-hostname) PROXY_FIXTURE_HOSTNAME="${2:?--proxy-fixture-hostname needs a value}"; shift 2 ;;
    --blocked-url) BLOCKED_URL="${2:?--blocked-url needs a value}"; shift 2 ;;
    --internet-proxy-dir) INTERNET_PROXY_DIR="${2:?--internet-proxy-dir needs a value}"; shift 2 ;;
    --agentgateway-dir) AGENTGATEWAY_DIR="${2:?--agentgateway-dir needs a value}"; shift 2 ;;
    --ca-cert)     CA_CERTS+=("${2:?--ca-cert needs a value}"); shift 2 ;;
    -h|--help)    usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 64 ;;
  esac
done

if [ "$ALL_TESTS" = 1 ]; then
  WITH_AGENT_PROXY=1
  WITH_INTERNET_PROXY=1
  WITH_PROXY_ENDPOINTS=1
  WITH_BUILD_PROXY=1
  WITH_COPILOT=1
  WITH_COPILOT_EGRESS=1
  WITH_EGRESS_SIDECAR=1
  WITH_SECURITY=1
  missing=()
  [ -n "$INTERNET_PROXY_DIR" ] || missing+=(--internet-proxy-dir)
  [ -n "$AGENTGATEWAY_DIR" ] || missing+=(--agentgateway-dir)
  if [ ${#missing[@]} -gt 0 ]; then
    echo "--all-tests also requires: ${missing[*]}" >&2
    exit 64
  fi
  if [ "$NO_BUILD" = 1 ]; then
    echo "--all-tests cannot be combined with --no-build" >&2
    exit 64
  fi
fi

if [ -n "$ONLY" ]; then
  known=0
  for suite in "${SUITES[@]}"; do
    [ "$suite" = "$ONLY" ] && known=1
  done
  if [ "$known" = 0 ]; then
    echo "Unknown suite: $ONLY" >&2
    echo "Available suites: ${SUITES[*]}" >&2
    exit 64
  fi
  # Selecting an opt-in suite by name is an explicit request to run it.
  case "$ONLY" in
    agent-proxy|agent-proxy-isolation) WITH_AGENT_PROXY=1 ;;
    internet-proxy-isolation) WITH_INTERNET_PROXY=1 ;;
    proxy-endpoints) WITH_PROXY_ENDPOINTS=1 ;;
    build-proxy) WITH_BUILD_PROXY=1 ;;
    copilot) WITH_COPILOT=1 ;;
    copilot-egress) WITH_COPILOT_EGRESS=1 ;;
    egress-sidecar) WITH_EGRESS_SIDECAR=1 ;;
    security) WITH_SECURITY=1 ;;
  esac
fi

# --incremental: results are kept per source fingerprint and configuration in
# $STATE_DIR, one marker file per suite that passed. Only settings that change
# how a suite behaves belong in the key; suite-selection flags do not.
STATE_DIR=""
CACHED=0
if [ "$INCREMENTAL" = 1 ]; then
  STATE_ROOT="${PROJECT_SANDBOX_E2E_STATE_DIR:-$ROOT/.e2e-state}"
  STATE_SETTINGS=(
    "BASE_IMAGE=$BASE_IMAGE"
    "NO_BUILD=$NO_BUILD"
    "BLOCKED_URL=$BLOCKED_URL"
    "INTERNET_PROXY_DIR=$INTERNET_PROXY_DIR"
    "AGENTGATEWAY_DIR=$AGENTGATEWAY_DIR"
    "PROXY_FIXTURE_HOST=$PROXY_FIXTURE_HOST"
    "PROXY_FIXTURE_IPV6_HOST=$PROXY_FIXTURE_IPV6_HOST"
    "PROXY_FIXTURE_HOSTNAME=$PROXY_FIXTURE_HOSTNAME"
    "COPILOT_HOST=$COPILOT_HOST"
    "SECURITY_PEER_RUNTIME=$SECURITY_PEER_RUNTIME"
    "SECURITY_FIXTURE_HOST=$SECURITY_FIXTURE_HOST"
    "SECURITY_NAT64_ORIGIN=$SECURITY_NAT64_ORIGIN"
    "STRICT_SECURITY_COVERAGE=$STRICT_SECURITY_COVERAGE"
  )
  for cert in ${CA_CERTS[@]+"${CA_CERTS[@]}"}; do
    STATE_SETTINGS+=("CA_CERT=$cert")
  done
  if ! STATE_KEY="$(uv run --no-project python -I "$ROOT/scripts/e2e-state.py" \
      --root "$ROOT" --runtime "$RUNTIME" "${STATE_SETTINGS[@]}")"; then
    echo "ERROR: --incremental could not fingerprint the source in $ROOT." >&2
    exit 64
  fi
  STATE_DIR="$STATE_ROOT/$STATE_KEY"
  echo "Incremental: results for this source and configuration in $STATE_DIR"
fi

# True when the suite already passed for this source and configuration.
passed_before() { [ -n "$STATE_DIR" ] && [ -f "$STATE_DIR/$1.pass" ]; }

record_pass() {
  [ -n "$STATE_DIR" ] || return 0
  mkdir -p "$STATE_DIR"
  printf 'passed %s in %ss\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$2" >"$STATE_DIR/$1.pass"
}

# True when the suite should run in this invocation: either no --only filter was
# given, or this is the selected suite.
selected() { [ -z "$ONLY" ] || [ "$ONLY" = "$1" ]; }

overall_fail=0
# Parallel arrays holding one entry per suite considered by this invocation, in
# execution order, for the closing summary. Bash 3.2 (stock macOS) has no
# associative arrays, so keep status/name/detail aligned by index.
RESULT_STATUS=()
RESULT_NAME=()
RESULT_DETAIL=()

record_result() {
  RESULT_STATUS+=("$1")
  RESULT_NAME+=("$2")
  RESULT_DETAIL+=("${3:-}")
}

# Suite output is captured per suite; the console only carries progress lines.
# A passing suite's log is deleted unless --keep was given; a failing suite's
# log is always retained so the failure can be inspected afterwards.
LOG_TMP_BASE="${TMPDIR:-/tmp}"
LOG_DIR="$(mktemp -d "${LOG_TMP_BASE%/}/project-sandbox-e2e-logs.XXXXXX")"
LOGS_KEPT=0

if [ -n "$ONLY" ]; then
  SUITE_TOTAL=1
else
  SUITE_TOTAL=${#SUITES[@]}
fi
SUITE_INDEX=0

# Print the leading half of a progress line; the outcome completes it.
suite_progress() {
  SUITE_INDEX=$((SUITE_INDEX + 1))
  printf '[%d/%d] %-26s ' "$SUITE_INDEX" "$SUITE_TOTAL" "$1"
}

run_suite() {
  local name="$1"; shift
  local log="$LOG_DIR/$name.log"
  local start=$SECONDS
  suite_progress "$name"
  if passed_before "$name"; then
    printf 'PASS (cached)\n'
    record_result PASS "$name" "incremental: passed earlier on this source"
    CACHED=$((CACHED + 1))
    return 0
  fi
  if [ "$PLAN" = 1 ]; then
    printf 'RUN\n'
    record_result RUN "$name"
    return 0
  fi
  if "$@" >"$log" 2>&1; then
    printf 'PASS (%ds)\n' "$((SECONDS - start))"
    record_pass "$name" "$((SECONDS - start))"
    if [ "$KEEP" = 1 ]; then
      record_result PASS "$name" "log: $log"
      LOGS_KEPT=1
    else
      record_result PASS "$name"
      rm -f "$log"
    fi
  else
    printf 'FAIL (%ds)  log: %s\n' "$((SECONDS - start))" "$log"
    tail -n 20 "$log" | sed 's/^/       | /'
    record_result FAIL "$name" "log: $log"
    LOGS_KEPT=1
    overall_fail=1
  fi
}

skip_suite() {
  suite_progress "$1"
  printf 'SKIP (%s)\n' "$2"
  record_result SKIP "$1" "$2"
}

# Report a suite that cannot even start because this invocation is missing a
# prerequisite it explicitly asked for. That is a failure, not a skip.
fail_suite() {
  suite_progress "$1"
  printf 'FAIL (%s)\n' "$2"
  record_result FAIL "$1" "$2"
  overall_fail=1
}

# Build common flags for the workflow scripts
WORKFLOW_ARGS=(--runtime "$RUNTIME" --base-image "$BASE_IMAGE")
[ "$NO_BUILD" = 1 ] && WORKFLOW_ARGS+=(--no-build)
[ "$KEEP"     = 1 ] && WORKFLOW_ARGS+=(--keep)

# Resolve a concrete runtime for checks that require a real container and do
# not accept chroot/auto. Selecting chroot deliberately keeps the aggregate
# suite host-only even if Docker or Podman happens to be installed.
CONTAINER_RUNTIME=""
case "$RUNTIME" in
  apple-container|docker|podman) CONTAINER_RUNTIME="$RUNTIME" ;;
  auto)
    if [ "$(uname -s)" = Darwin ] && command -v container >/dev/null 2>&1; then
      CONTAINER_RUNTIME="apple-container"
    elif command -v docker >/dev/null 2>&1; then
      CONTAINER_RUNTIME="docker"
    elif command -v podman >/dev/null 2>&1; then
      CONTAINER_RUNTIME="podman"
    fi
    ;;
esac

# A SIGKILLed sandbox CLI from an earlier run cannot stop its own container;
# stop such orphans so they cannot interfere with this run. Best-effort.
if [ -n "$CONTAINER_RUNTIME" ] && [ "$PLAN" = 0 ]; then
  (cd "$ROOT" && uv run python "$ROOT/scripts/e2e-reap-orphans.py" \
    --runtime "$CONTAINER_RUNTIME") || true
fi

# 1. Basic artifact-generation smoke test (no container required)
if selected smoke; then
  run_suite "smoke" "$ROOT/scripts/e2e-test.sh"
fi

# 2. Env/API-key forwarding via headless bash-agent
if selected env-injection; then
  run_suite "env-injection" "$ROOT/scripts/e2e-env-injection.sh" "${WORKFLOW_ARGS[@]}"
fi

# 3. Git workflow: rebase / merge / nothing
if selected git-workflow; then
  run_suite "git-workflow" "$ROOT/scripts/e2e-git-workflow.sh" "${WORKFLOW_ARGS[@]}"
fi

# 4. Jj workflow: rebase / merge / nothing (only if jj is on PATH)
if selected jj-workflow; then
  if command -v jj >/dev/null 2>&1; then
    run_suite "jj-workflow" "$ROOT/scripts/e2e-jj-workflow.sh" "${WORKFLOW_ARGS[@]}"
  else
    skip_suite "jj-workflow" "jj not found on PATH"
  fi
fi

# VCS access needs real mounts and builds one image for all three access modes.
if selected vcs-access; then
  if [ -n "$CONTAINER_RUNTIME" ] && [ "$NO_BUILD" = 0 ]; then
    VCS_ARGS=(--runtime "$CONTAINER_RUNTIME" --base-image "$BASE_IMAGE")
    [ "$KEEP" = 1 ] && VCS_ARGS+=(--keep)
    run_suite "vcs-access" uv run python "$ROOT/scripts/e2e-vcs-access.py" "${VCS_ARGS[@]}"
  else
    skip_suite "vcs-access" "select a container runtime without --no-build"
  fi
fi

# Firewall initialization must run in fresh real containers, with builds enabled.
if selected firewall; then
  if [ -n "$CONTAINER_RUNTIME" ] && [ "$NO_BUILD" = 0 ]; then
    FIREWALL_ARGS=(--runtime "$CONTAINER_RUNTIME" --base-image "$BASE_IMAGE")
    [ "$KEEP" = 1 ] && FIREWALL_ARGS+=(--keep)
    run_suite "firewall" uv run python "$ROOT/scripts/e2e-firewall.py" "${FIREWALL_ARGS[@]}"
  else
    skip_suite "firewall" "select a container runtime without --no-build"
  fi
fi

# 5. Dockerfile tamper detection always runs its portable host checks. Add its
# real-container override check when this invocation permits a build.
if selected dockerfile-tamper; then
  TAMPER_ARGS=()
  [ "$KEEP" = 1 ] && TAMPER_ARGS+=(--keep)
  if [ -n "$CONTAINER_RUNTIME" ] && [ "$NO_BUILD" = 0 ]; then
    TAMPER_ARGS+=(--with-container --runtime "$CONTAINER_RUNTIME" --base-image "$BASE_IMAGE")
  fi
  run_suite "dockerfile-tamper" "$ROOT/scripts/e2e-dockerfile-tamper.sh" "${TAMPER_ARGS[@]}"
fi

# 6. Timeout teardown necessarily builds and kills a real container. Skip it
# for chroot, unavailable auto runtimes, and explicit --no-build runs.
if selected timeout-teardown; then
  if [ -n "$CONTAINER_RUNTIME" ] && [ "$NO_BUILD" = 0 ]; then
    run_suite "timeout-teardown" \
      "$ROOT/scripts/verify-timeout-teardown.sh" --runtime "$CONTAINER_RUNTIME"
  else
    skip_suite "timeout-teardown" "select a container runtime without --no-build"
  fi
fi

# 7. --python-uv interpreter/venv usability. Builds its own image from a
# synthesised Dockerfile, so it needs a real runtime and cannot honour --no-build.
if selected python-uv; then
  if [ -n "$CONTAINER_RUNTIME" ] && [ "$NO_BUILD" = 0 ]; then
    PYTHON_UV_ARGS=(--runtime "$CONTAINER_RUNTIME")
    [ "$KEEP" = 1 ] && PYTHON_UV_ARGS+=(--keep)
    run_suite "python-uv" "$ROOT/scripts/e2e-python-uv.sh" "${PYTHON_UV_ARGS[@]}"
  else
    skip_suite "python-uv" "select a container runtime without --no-build"
  fi
fi

# Node/npm builds its own image and requires a real runtime and host npm.
if selected node-npm; then
  if [ -n "$CONTAINER_RUNTIME" ] && [ "$NO_BUILD" = 0 ] && command -v npm >/dev/null 2>&1; then
    NODE_NPM_ARGS=(--runtime "$CONTAINER_RUNTIME")
    [ "$KEEP" = 1 ] && NODE_NPM_ARGS+=(--keep)
    run_suite "node-npm" "$ROOT/scripts/e2e-node-npm.sh" "${NODE_NPM_ARGS[@]}"
  else
    skip_suite "node-npm" "requires host npm and a container runtime without --no-build"
  fi
fi

# 8. Pi + Ollama: networking and baked config (only if Ollama is reachable;
# chroot cannot run --agent pi, so fall back to auto runtime detection)
if selected pi-ollama; then
  if command -v curl >/dev/null 2>&1 && curl -sf --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    PI_OLLAMA_RUNTIME="$RUNTIME"
    [ "$PI_OLLAMA_RUNTIME" = chroot ] && PI_OLLAMA_RUNTIME=auto
    PI_OLLAMA_ARGS=(--runtime "$PI_OLLAMA_RUNTIME" --base-image "$BASE_IMAGE")
    [ "$NO_BUILD" = 1 ] && PI_OLLAMA_ARGS+=(--no-build)
    [ "$KEEP"     = 1 ] && PI_OLLAMA_ARGS+=(--keep)
    run_suite "pi-ollama" "$ROOT/scripts/e2e-pi-ollama.sh" "${PI_OLLAMA_ARGS[@]}"
  else
    skip_suite "pi-ollama" "Ollama not reachable on 127.0.0.1:11434"
  fi
fi

# 9. Non-destructive Internet-proxy smoke test. The script itself detects an
# absent listener and reports a successful skip before creating anything.
if selected internet-proxy-smoke; then
  if [ -n "$CONTAINER_RUNTIME" ]; then
    INTERNET_SMOKE_ARGS=(--runtime "$CONTAINER_RUNTIME" --base-image "$BASE_IMAGE")
    [ "$NO_BUILD" = 1 ] && INTERNET_SMOKE_ARGS+=(--no-build)
    [ "$KEEP" = 1 ] && INTERNET_SMOKE_ARGS+=(--keep)
    for ca_cert in ${CA_CERTS[@]+"${CA_CERTS[@]}"}; do
      INTERNET_SMOKE_ARGS+=(--ca-cert "$ca_cert")
    done
    run_suite "internet-proxy-smoke" \
      uv run python "$ROOT/scripts/e2e-internet-proxy-smoke.py" "${INTERNET_SMOKE_ARGS[@]}"
  else
    skip_suite "internet-proxy-smoke" "select a real container runtime"
  fi
fi

# 10. The combined Internet-proxy audit deliberately controls both external
# services and makes real AI requests. Never availability-gate this destructive test.
if selected internet-proxy-isolation; then
  if [ "$WITH_INTERNET_PROXY" = 1 ]; then
    if [ -z "$CONTAINER_RUNTIME" ] || [ -z "$INTERNET_PROXY_DIR" ] || [ -z "$AGENTGATEWAY_DIR" ]; then
      fail_suite "internet-proxy-isolation" \
        "requires a real runtime, --internet-proxy-dir, and --agentgateway-dir"
    else
      INTERNET_AUDIT_ARGS=(
        --runtime "$CONTAINER_RUNTIME" --base-image "$BASE_IMAGE"
        --blocked-url "$BLOCKED_URL" --internet-proxy-dir "$INTERNET_PROXY_DIR"
        --agentgateway-dir "$AGENTGATEWAY_DIR"
      )
      [ "$NO_BUILD" = 1 ] && INTERNET_AUDIT_ARGS+=(--no-build)
      [ "$KEEP" = 1 ] && INTERNET_AUDIT_ARGS+=(--keep)
      for ca_cert in ${CA_CERTS[@]+"${CA_CERTS[@]}"}; do
        INTERNET_AUDIT_ARGS+=(--ca-cert "$ca_cert")
      done
      run_suite "internet-proxy-isolation" \
        uv run python "$ROOT/scripts/e2e-internet-proxy-isolation.py" "${INTERNET_AUDIT_ARGS[@]}"
    fi
  else
    skip_suite "internet-proxy-isolation" "pass --with-internet-proxy; destructive and billable"
  fi
fi

# 11–12. Gateway-only isolation is non-billable, followed by two real requests.
# Never trigger either merely because a listener happens to be present.
if [ "$WITH_AGENT_PROXY" = 1 ]; then
  if selected agent-proxy-isolation; then
    if [ -n "$CONTAINER_RUNTIME" ]; then
      PROXY_ISOLATION_ARGS=(--runtime "$CONTAINER_RUNTIME" --base-image "$BASE_IMAGE")
      [ "$NO_BUILD" = 1 ] && PROXY_ISOLATION_ARGS+=(--no-build)
      [ "$KEEP" = 1 ] && PROXY_ISOLATION_ARGS+=(--keep)
      run_suite "agent-proxy-isolation" \
        uv run python "$ROOT/scripts/e2e-agent-proxy-isolation.py" "${PROXY_ISOLATION_ARGS[@]}"
    else
      fail_suite "agent-proxy-isolation" \
        "select apple-container, docker, podman, or an available auto runtime"
    fi
  fi
  if selected agent-proxy; then
    run_suite "agent-proxy" \
      uv run python "$ROOT/scripts/check-agent-proxy.py" --base-image "$BASE_IMAGE" \
        --runtime "${CONTAINER_RUNTIME:-auto}"
  fi
else
  if selected agent-proxy-isolation; then
    skip_suite "agent-proxy-isolation" "pass --with-agent-proxy"
  fi
  if selected agent-proxy; then
    skip_suite "agent-proxy" "pass --with-agent-proxy; makes two billable requests"
  fi
fi

# Controlled remote endpoints are opt-in even when a runtime is available.
if selected proxy-endpoints; then
  if [ "$WITH_PROXY_ENDPOINTS" = 1 ]; then
    if [ -z "$CONTAINER_RUNTIME" ] || [ "$NO_BUILD" = 1 ]; then
      fail_suite "proxy-endpoints" "requires a real runtime, and builds enabled"
    else
      ENDPOINT_ARGS=(--runtime "$CONTAINER_RUNTIME" --base-image "$BASE_IMAGE")
      [ -n "$PROXY_FIXTURE_HOST" ] && ENDPOINT_ARGS+=(--fixture-host "$PROXY_FIXTURE_HOST")
      [ -n "$PROXY_FIXTURE_IPV6_HOST" ] && ENDPOINT_ARGS+=(--ipv6-host "$PROXY_FIXTURE_IPV6_HOST")
      [ -n "$PROXY_FIXTURE_HOSTNAME" ] && ENDPOINT_ARGS+=(--fixture-hostname "$PROXY_FIXTURE_HOSTNAME")
      [ "$KEEP" = 1 ] && ENDPOINT_ARGS+=(--keep)
      run_suite "proxy-endpoints" uv run python "$ROOT/scripts/e2e-proxy-endpoints.py" "${ENDPOINT_ARGS[@]}"
    fi
  else
    skip_suite "proxy-endpoints" "pass --with-proxy-endpoints"
  fi
fi

if selected build-proxy; then
  if [ "$WITH_BUILD_PROXY" = 1 ]; then
    if [ -z "$CONTAINER_RUNTIME" ] || [ -z "$INTERNET_PROXY_DIR" ] || [ "$NO_BUILD" = 1 ] \
        || { [ "$CONTAINER_RUNTIME" = podman ] && ! command -v docker >/dev/null 2>&1; }; then
      fail_suite "build-proxy" "requires a real runtime (Podman also needs Docker for the fixture), --internet-proxy-dir, and builds enabled"
    else
      BUILD_PROXY_ARGS=(--runtime "$CONTAINER_RUNTIME" --base-image "$BASE_IMAGE" --internet-proxy-dir "$INTERNET_PROXY_DIR")
      [ -n "$PROXY_FIXTURE_HOST" ] && BUILD_PROXY_ARGS+=(--fixture-host "$PROXY_FIXTURE_HOST")
      [ "$KEEP" = 1 ] && BUILD_PROXY_ARGS+=(--keep)
      run_suite "build-proxy" uv run python "$ROOT/scripts/e2e-build-proxy.py" "${BUILD_PROXY_ARGS[@]}"
    fi
  else
    skip_suite "build-proxy" "pass --with-build-proxy, --internet-proxy-dir,"
  fi
fi

# 16. Real Copilot sessions are billable and need a token, so they never run
# just because one is available; without the opt-in nothing is looked up.
if selected copilot; then
  if [ "$WITH_COPILOT" = 1 ]; then
    if [ -z "$CONTAINER_RUNTIME" ]; then
      fail_suite "copilot" "select apple-container, docker, podman, or an available auto runtime"
    else
      COPILOT_ARGS=(--runtime "$CONTAINER_RUNTIME" --base-image "$BASE_IMAGE")
      [ -n "$COPILOT_HOST" ] && COPILOT_ARGS+=(--copilot-host "$COPILOT_HOST")
      [ -n "$COPILOT_CAPTURE_DIR" ] && COPILOT_ARGS+=(--capture-dir "$COPILOT_CAPTURE_DIR")
      [ "$NO_BUILD" = 1 ] && COPILOT_ARGS+=(--no-build)
      [ "$KEEP" = 1 ] && COPILOT_ARGS+=(--keep)
      run_suite "copilot" uv run python "$ROOT/scripts/e2e-copilot.py" "${COPILOT_ARGS[@]}"
    fi
  else
    skip_suite "copilot" "pass --with-copilot; makes billable Copilot requests"
  fi
fi

# 17. Copilot egress capture/profile check: billable, token-gated like copilot.
if selected copilot-egress; then
  if [ "$WITH_COPILOT_EGRESS" = 1 ]; then
    if [ -z "$CONTAINER_RUNTIME" ]; then
      fail_suite "copilot-egress" "select apple-container, docker, podman, or an available auto runtime"
    else
      COPILOT_EGRESS_ARGS=(--runtime "$CONTAINER_RUNTIME" --base-image "$BASE_IMAGE")
      [ -n "$COPILOT_EGRESS_REPORT_DIR" ] && COPILOT_EGRESS_ARGS+=(--report-dir "$COPILOT_EGRESS_REPORT_DIR")
      # No --no-build: each run bakes a fresh fixture CA into the image.
      [ "$KEEP" = 1 ] && COPILOT_EGRESS_ARGS+=(--keep)
      run_suite "copilot-egress" uv run python "$ROOT/scripts/e2e-copilot-egress.py" "${COPILOT_EGRESS_ARGS[@]}"
    fi
  else
    skip_suite "copilot-egress" "pass --with-copilot-egress; makes billable Copilot requests"
  fi
fi

# 18. --egress sidecar end to end: needs network and builds, so opt-in.
if selected egress-sidecar; then
  if [ "$WITH_EGRESS_SIDECAR" = 1 ]; then
    if [ -z "$CONTAINER_RUNTIME" ]; then
      fail_suite "egress-sidecar" "select apple-container, docker, podman, or an available auto runtime"
    else
      EGRESS_SIDECAR_ARGS=(--runtime "$CONTAINER_RUNTIME" --base-image "$BASE_IMAGE")
      [ "$NO_BUILD" = 1 ] && EGRESS_SIDECAR_ARGS+=(--no-build)
      [ "$KEEP" = 1 ] && EGRESS_SIDECAR_ARGS+=(--keep)
      run_suite "egress-sidecar" uv run python "$ROOT/scripts/e2e-egress-sidecar.py" "${EGRESS_SIDECAR_ARGS[@]}"
    fi
  else
    skip_suite "egress-sidecar" "pass --with-egress-sidecar; needs network and builds"
  fi
fi

# Security records known gaps as XFAIL and missing prerequisites as SKIP; both
# stay visible in the -ra summary of the suite log. --strict-security-coverage
# turns them into failures for a release gate.
if selected security; then
  if [ "$WITH_SECURITY" = 1 ]; then
    if [ "$NO_BUILD" = 1 ]; then
      fail_suite "security" "security verification requires builds"
    else
      SECURITY_ARGS=(--security-runtime "${CONTAINER_RUNTIME:-host}")
      [ "$STRICT_SECURITY_COVERAGE" = 1 ] && SECURITY_ARGS+=(--strict-coverage)
      [ -n "$SECURITY_PEER_RUNTIME" ] && SECURITY_ARGS+=(--peer-runtime "$SECURITY_PEER_RUNTIME")
      [ -n "$SECURITY_FIXTURE_HOST" ] && SECURITY_ARGS+=(--fixture-host "$SECURITY_FIXTURE_HOST")
      [ -n "$SECURITY_NAT64_ORIGIN" ] && SECURITY_ARGS+=(--nat64-origin "$SECURITY_NAT64_ORIGIN")
      run_suite "security" uv run pytest -ra -q "$ROOT/scripts/security_e2e" "${SECURITY_ARGS[@]}"
    fi
  else
    skip_suite "security" "pass --with-security or --only security; includes known limitations and live security probes"
  fi
fi

# Closing summary: every suite this invocation considered, with its outcome.
passed=0
failed=0
skipped=0
planned=0
echo "========================================"
echo "Summary"
echo "========================================"
for i in "${!RESULT_NAME[@]}"; do
  status="${RESULT_STATUS[$i]}"
  detail="${RESULT_DETAIL[$i]}"
  case "$status" in
    PASS) passed=$((passed + 1)) ;;
    FAIL) failed=$((failed + 1)) ;;
    SKIP) skipped=$((skipped + 1)) ;;
    RUN) planned=$((planned + 1)) ;;
  esac
  if [ -n "$detail" ]; then
    printf '  %-4s %s  (%s)\n' "$status" "${RESULT_NAME[$i]}" "$detail"
  else
    printf '  %-4s %s\n' "$status" "${RESULT_NAME[$i]}"
  fi
done
echo
if [ "$PLAN" = 1 ]; then
  echo "Plan: $planned suite(s) would run, $failed cannot start, $skipped skipped."
  [ "$CACHED" = 0 ] || echo "Incremental: $CACHED suite(s) already passed on this source."
  rmdir "$LOG_DIR" 2>/dev/null || true
  exit 0
fi
echo "Ran $((passed + failed - CACHED)) suite(s): $((passed - CACHED)) passed, $failed failed, $skipped skipped."
[ "$CACHED" = 0 ] || echo "Incremental: $CACHED suite(s) not rerun; they passed earlier on this source."
if [ "$LOGS_KEPT" = 1 ]; then
  echo "Suite logs kept in: $LOG_DIR"
else
  rmdir "$LOG_DIR" 2>/dev/null || true
fi

if [ "$overall_fail" = 0 ]; then
  echo "All e2e suites PASSED."
  exit 0
else
  echo "One or more e2e suites FAILED."
  exit 1
fi
