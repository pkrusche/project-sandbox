#!/usr/bin/env bash
# Run every supported end-to-end suite on every runtime supported by this host.
#
# Runtimes are detected with `project-sandbox doctor --json`: each of
# apple-container, docker, podman and chroot whose check reports "ok" is
# exercised by one scripts/run-e2e-tests.sh invocation, one after another.
#
# Opt-in suites are enabled per container runtime when they can work here:
#   proxy-endpoints, egress-sidecar   always (non-billable; need network + builds)
#   build-proxy                       internet-proxy-locally sources found
#                                     (Podman additionally needs Docker for the fixture)
#   agent-proxy-isolation, agent-proxy
#                                     agentgateway-locally found AND listening on
#                                     127.0.0.1:4000 (agent-proxy makes two billable requests)
#   internet-proxy-isolation          both services found AND listening
#                                     (127.0.0.1:18080 and 127.0.0.1:4000). DESTRUCTIVE:
#                                     stops and restarts both services; billable
#   copilot, copilot-egress           always (billable; needs a Copilot token);
#                                     pass --no-copilot to leave them out
# Anything unavailable is skipped with a note printed before and after the run.
# The chroot runtime only runs the suites run-e2e-tests.sh supports without a
# container; no opt-in suite is requested for it.
#
# scripts/e2e-cleanup.py runs for the detected container runtimes before the
# first and after the last runtime, sweeping containers, forwarders and images
# left by earlier or current e2e runs. A failed cleanup fails the overall run.
#
# Usage:
#   scripts/run_all_supported_e2e_tests.sh [--runtimes LIST]
#       [--internet-proxy-dir DIR] [--agentgateway-dir DIR]
#       [--no-copilot] [--no-internet-proxy-isolation] [--no-build] [--keep]
#       [--base-image IMAGE] [--dry-run] [-- RUN-E2E-TESTS-OPTIONS...]
#
#   --runtimes LIST   comma-separated subset of detected runtimes to run
#                     (default: every runtime doctor reports as ok)
#   --internet-proxy-dir DIR
#                     internet-proxy-locally checkout (default: ../internet-proxy-locally)
#   --agentgateway-dir DIR
#                     agentgateway-locally checkout (default: ../agentgateway-locally)
#   --no-copilot      skip the billable copilot and copilot-egress suites
#   --no-internet-proxy-isolation
#                     never run the destructive two-service isolation audit
#   --no-build        forwarded; also drops opt-in suites that require builds
#   --keep            forwarded to run-e2e-tests.sh
#   --base-image IMG  forwarded to run-e2e-tests.sh
#   --dry-run         print the detected plan and commands without running them
#   -- ...            pass the remaining options to every run-e2e-tests.sh run
#                     (for example --ca-cert PATH or --proxy-fixture-hostname NAME)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNNER="$ROOT/scripts/run-e2e-tests.sh"

# Default endpoints of the two local services (and of the e2e scripts using them).
INTERNET_PROXY_HOST=127.0.0.1
INTERNET_PROXY_PORT=18080
AGENT_PROXY_HOST=127.0.0.1
AGENT_PROXY_PORT=4000

KNOWN_RUNTIMES=(apple-container docker podman chroot)

RUNTIMES_FILTER=""
INTERNET_PROXY_DIR="$ROOT/../internet-proxy-locally"
AGENTGATEWAY_DIR="$ROOT/../agentgateway-locally"
WITH_COPILOT=1
NO_INTERNET_ISOLATION=0
NO_BUILD=0
DRY_RUN=0
COMMON_ARGS=()
EXTRA_ARGS=()

usage() { sed -n '2,${/^#/!q;p;}' "$0"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --runtimes) RUNTIMES_FILTER="${2:?--runtimes needs a value}"; shift 2 ;;
    --internet-proxy-dir) INTERNET_PROXY_DIR="${2:?--internet-proxy-dir needs a value}"; shift 2 ;;
    --agentgateway-dir) AGENTGATEWAY_DIR="${2:?--agentgateway-dir needs a value}"; shift 2 ;;
    --no-copilot) WITH_COPILOT=0; shift ;;
    --no-internet-proxy-isolation) NO_INTERNET_ISOLATION=1; shift ;;
    --no-build) NO_BUILD=1; COMMON_ARGS+=(--no-build); shift ;;
    --keep) COMMON_ARGS+=(--keep); shift ;;
    --base-image) COMMON_ARGS+=(--base-image "${2:?--base-image needs a value}"); shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --) shift; EXTRA_ARGS+=("$@"); break ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 64 ;;
  esac
done

command -v uv >/dev/null 2>&1 || { echo "ERROR: uv not found on PATH." >&2; exit 64; }

NOTES=()
note() { NOTES+=("$1"); }

# Absolute path of an existing directory, or empty when it does not exist.
resolve_dir() { (cd "$1" 2>/dev/null && pwd) || true; }

# True when something accepts TCP connections on HOST PORT. Loopback refusals
# return immediately, so no timeout helper is needed.
port_open() { (exec 3<>"/dev/tcp/$1/$2") >/dev/null 2>&1; }

# --- Runtime detection ------------------------------------------------------
if ! DOCTOR_JSON="$(cd "$ROOT" && uv run project-sandbox doctor --json 2>/dev/null)"; then
  # doctor exits non-zero when some check fails; its JSON is still usable.
  [ -n "${DOCTOR_JSON:-}" ] || { echo "ERROR: project-sandbox doctor --json failed." >&2; exit 1; }
fi
DETECTED="$(printf '%s' "$DOCTOR_JSON" | uv run --no-project python -I -c '
import json, sys
known = sys.argv[1:]
checks = json.load(sys.stdin).get("checks", [])
ok = {c["name"]: c for c in checks if c.get("section") == "runtimes"}
for name in known:
    c = ok.get(name)
    if c is None:
        continue
    print(("ok" if c.get("status") == "ok" else "no") + "\t" + name + "\t" + (c.get("detail") or ""))
' "${KNOWN_RUNTIMES[@]}")"

RUNTIMES=()
while IFS="$(printf '\t')" read -r status name detail; do
  [ -n "${name:-}" ] || continue
  if [ "$status" = ok ]; then
    RUNTIMES+=("$name")
  else
    note "runtime $name not available: ${detail:-unsupported}"
  fi
done <<EOF
$DETECTED
EOF

if [ -n "$RUNTIMES_FILTER" ]; then
  FILTERED=()
  IFS=',' read -r -a WANTED <<<"$RUNTIMES_FILTER"
  for want in "${WANTED[@]}"; do
    found=0
    for have in ${RUNTIMES[@]+"${RUNTIMES[@]}"}; do
      [ "$have" = "$want" ] && found=1
    done
    if [ "$found" = 1 ]; then
      FILTERED+=("$want")
    else
      echo "ERROR: runtime '$want' is not available on this host." >&2
      exit 64
    fi
  done
  RUNTIMES=("${FILTERED[@]}")
fi

if [ ${#RUNTIMES[@]} -eq 0 ]; then
  echo "ERROR: no supported runtime detected; see 'uv run project-sandbox doctor'." >&2
  exit 1
fi

# --- Proxy service detection ------------------------------------------------
IPL_DIR="$(resolve_dir "$INTERNET_PROXY_DIR")"
GW_DIR="$(resolve_dir "$AGENTGATEWAY_DIR")"
IPL_UP=0
GW_UP=0

if [ -z "$IPL_DIR" ] || [ ! -f "$IPL_DIR/pyproject.toml" ]; then
  IPL_DIR=""
  note "internet proxy not available: no internet-proxy-locally checkout at $INTERNET_PROXY_DIR; skipping build-proxy and internet-proxy-isolation"
elif port_open "$INTERNET_PROXY_HOST" "$INTERNET_PROXY_PORT"; then
  IPL_UP=1
else
  note "internet proxy not up: nothing listening on $INTERNET_PROXY_HOST:$INTERNET_PROXY_PORT (start it with 'uv run ipl up' in $IPL_DIR); skipping internet-proxy-isolation"
fi

if [ -z "$GW_DIR" ] || [ ! -f "$GW_DIR/run.py" ]; then
  GW_DIR=""
  note "agent proxy not available: no agentgateway-locally checkout at $AGENTGATEWAY_DIR; skipping agent-proxy-isolation, agent-proxy and internet-proxy-isolation"
elif port_open "$AGENT_PROXY_HOST" "$AGENT_PROXY_PORT"; then
  GW_UP=1
else
  note "agent proxy not up: nothing listening on $AGENT_PROXY_HOST:$AGENT_PROXY_PORT (start it with './run.py up' in $GW_DIR); skipping agent-proxy-isolation, agent-proxy and internet-proxy-isolation"
fi

if [ "$NO_INTERNET_ISOLATION" = 1 ] && [ "$IPL_UP" = 1 ] && [ "$GW_UP" = 1 ]; then
  note "internet-proxy-isolation disabled by --no-internet-proxy-isolation"
fi
if [ "$WITH_COPILOT" = 0 ]; then
  note "copilot and copilot-egress disabled by --no-copilot"
fi
if [ "$NO_BUILD" = 1 ]; then
  note "--no-build: skipping proxy-endpoints, egress-sidecar, build-proxy, internet-proxy-isolation and copilot-egress"
fi

# --- Per-runtime argument assembly -----------------------------------------
# Prints one argument per line for the run-e2e-tests.sh invocation of $1.
runtime_args() {
  local rt="$1"
  printf '%s\n' --runtime "$rt"
  [ "$rt" = chroot ] && return 0
  [ -n "$IPL_DIR" ] && printf '%s\n' --internet-proxy-dir "$IPL_DIR"
  [ -n "$GW_DIR" ] && printf '%s\n' --agentgateway-dir "$GW_DIR"
  if [ "$NO_BUILD" = 0 ]; then
    printf '%s\n' --with-proxy-endpoints --with-egress-sidecar
    if [ -n "$IPL_DIR" ] && { [ "$rt" != podman ] || command -v docker >/dev/null 2>&1; }; then
      printf '%s\n' --with-build-proxy
    fi
  fi
  if [ "$GW_UP" = 1 ]; then
    printf '%s\n' --with-agent-proxy
  fi
  if [ "$NO_BUILD" = 0 ] && [ "$NO_INTERNET_ISOLATION" = 0 ] && [ "$IPL_UP" = 1 ] && [ "$GW_UP" = 1 ]; then
    printf '%s\n' --with-internet-proxy
  fi
  if [ "$WITH_COPILOT" = 1 ]; then
    printf '%s\n' --with-copilot
    [ "$NO_BUILD" = 0 ] && printf '%s\n' --with-copilot-egress
  fi
  return 0
}

for rt in "${RUNTIMES[@]}"; do
  if [ "$rt" = podman ] && [ -n "$IPL_DIR" ] && [ "$NO_BUILD" = 0 ] && ! command -v docker >/dev/null 2>&1; then
    note "build-proxy skipped on podman: its Squid fixture needs Docker"
  fi
done

print_notes() {
  [ ${#NOTES[@]} -gt 0 ] || return 0
  echo "Notes:"
  for n in "${NOTES[@]}"; do
    echo "  - $n"
  done
}

echo "Runtimes to test: ${RUNTIMES[*]}"
echo "Internet proxy:   ${IPL_DIR:-not found} ($([ "$IPL_UP" = 1 ] && echo up || echo down))"
echo "Agent proxy:      ${GW_DIR:-not found} ($([ "$GW_UP" = 1 ] && echo up || echo down))"
print_notes
echo

RESULT_RT=()
RESULT_RC=()
overall=0

# Sweep e2e leftovers on every detected container runtime (chroot leaves none).
CLEANUP_CMD=(uv run python "$ROOT/scripts/e2e-cleanup.py")
CLEANUP_RUNTIMES=0
for rt in "${RUNTIMES[@]}"; do
  if [ "$rt" != chroot ]; then
    CLEANUP_CMD+=(--runtime "$rt")
    CLEANUP_RUNTIMES=1
  fi
done

# Runs the cleanup and records it in the summary as cleanup-$1.
run_cleanup() {
  [ "$CLEANUP_RUNTIMES" = 1 ] || return 0
  echo "========================================"
  echo "Cleanup ($1)"
  echo "========================================"
  printf '+'; printf ' %q' "${CLEANUP_CMD[@]}"; echo
  [ "$DRY_RUN" = 1 ] && return 0
  local rc=0
  (cd "$ROOT" && "${CLEANUP_CMD[@]}") || rc=$?
  RESULT_RT+=("cleanup-$1")
  RESULT_RC+=("$rc")
  [ "$rc" = 0 ] || overall=1
  echo
}

run_cleanup before
for rt in "${RUNTIMES[@]}"; do
  ARGS=()
  while IFS= read -r line; do
    ARGS+=("$line")
  done < <(runtime_args "$rt")
  CMD=("$RUNNER" "${ARGS[@]}" ${COMMON_ARGS[@]+"${COMMON_ARGS[@]}"} ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"})
  echo "========================================"
  echo "Runtime: $rt"
  echo "========================================"
  printf '+'; printf ' %q' "${CMD[@]}"; echo
  if [ "$DRY_RUN" = 1 ]; then
    continue
  fi
  rc=0
  "${CMD[@]}" || rc=$?
  RESULT_RT+=("$rt")
  RESULT_RC+=("$rc")
  [ "$rc" = 0 ] || overall=1
  echo
done
run_cleanup after

if [ "$DRY_RUN" = 1 ]; then
  echo "Dry run: nothing executed."
  exit 0
fi

echo "========================================"
echo "Overall summary"
echo "========================================"
for i in "${!RESULT_RT[@]}"; do
  if [ "${RESULT_RC[$i]}" = 0 ]; then
    printf '  PASS %s\n' "${RESULT_RT[$i]}"
  else
    printf '  FAIL %s  (exit %s)\n' "${RESULT_RT[$i]}" "${RESULT_RC[$i]}"
  fi
done
print_notes
if [ "$overall" = 0 ]; then
  echo "All runtimes and cleanups PASSED."
else
  echo "One or more runtimes or cleanups FAILED."
fi
exit "$overall"
