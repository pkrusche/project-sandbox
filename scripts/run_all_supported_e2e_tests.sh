#!/usr/bin/env bash
# Run every supported end-to-end suite on every runtime supported by this host.
#
# Runtimes are detected with `project-sandbox doctor --json`: each of
# apple-container, docker, podman and chroot whose check reports "ok" is
# exercised by one scripts/run-e2e-tests.sh invocation, one after another.
#
# Opt-in suites are enabled per container runtime when they can work here:
#   proxy-endpoints, egress-sidecar   always (non-billable; need network + builds)
#   security                         always with builds; strict coverage gate; uses
#                                     another selected container runtime as peer when available
#   build-proxy                       internet-proxy-locally sources found
#                                     (Podman additionally needs Docker for the fixture)
#   agent-proxy-isolation, agent-proxy
#                                     agentgateway-locally found; they need it listening
#                                     on 127.0.0.1:4000 and fail otherwise
#                                     (agent-proxy makes two billable requests)
#   internet-proxy-isolation          both checkouts found. DESTRUCTIVE: stops and
#                                     restarts both services; billable. Starts a stopped
#                                     Internet proxy (127.0.0.1:18080) itself and stops it
#                                     afterwards; needs the gateway listening
#   copilot, copilot-egress           always (billable; needs a Copilot token);
#                                     pass --no-copilot to leave them out
# A missing checkout skips its suites; a checkout whose service is not listening
# still runs them. Both cases are reported as notes before and after the run.
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
  note "internet proxy not up: nothing listening on $INTERNET_PROXY_HOST:$INTERNET_PROXY_PORT; internet-proxy-isolation will start it and stop it afterwards"
fi

if [ -z "$GW_DIR" ] || [ ! -f "$GW_DIR/run.py" ]; then
  GW_DIR=""
  note "agent proxy not available: no agentgateway-locally checkout at $AGENTGATEWAY_DIR; skipping agent-proxy-isolation, agent-proxy and internet-proxy-isolation"
elif port_open "$AGENT_PROXY_HOST" "$AGENT_PROXY_PORT"; then
  GW_UP=1
else
  note "agent proxy not up: nothing listening on $AGENT_PROXY_HOST:$AGENT_PROXY_PORT; agent-proxy-isolation, agent-proxy and internet-proxy-isolation still run and will fail unless you start it ('./run.py up' in $GW_DIR)"
fi

if [ "$NO_INTERNET_ISOLATION" = 1 ] && [ -n "$IPL_DIR" ] && [ -n "$GW_DIR" ]; then
  note "internet-proxy-isolation disabled by --no-internet-proxy-isolation"
fi
if [ "$WITH_COPILOT" = 0 ]; then
  note "copilot and copilot-egress disabled by --no-copilot"
fi
if [ "$NO_BUILD" = 1 ]; then
  note "--no-build: skipping proxy-endpoints, egress-sidecar, security, build-proxy and copilot-egress"
fi

# --- Per-runtime argument assembly -----------------------------------------
# Select only from the requested/detected runtimes, never an excluded backend.
security_peer() {
  local candidate
  for candidate in "${RUNTIMES[@]}"; do
    if [ "$candidate" != chroot ] && [ "$candidate" != "$1" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 0
}

# Prints one argument per line for the run-e2e-tests.sh invocation of $1.
runtime_args() {
  local rt="$1"
  local peer
  printf '%s\n' --runtime "$rt"
  [ "$rt" = chroot ] && return 0
  [ -n "$IPL_DIR" ] && printf '%s\n' --internet-proxy-dir "$IPL_DIR"
  [ -n "$GW_DIR" ] && printf '%s\n' --agentgateway-dir "$GW_DIR"
  if [ "$NO_BUILD" = 0 ]; then
    printf '%s\n' --with-proxy-endpoints --with-egress-sidecar --with-security
    peer="$(security_peer "$rt")"
    [ -n "$peer" ] && printf '%s\n' --security-peer-runtime "$peer"
    if [ -n "$IPL_DIR" ] && { [ "$rt" != podman ] || command -v docker >/dev/null 2>&1; }; then
      printf '%s\n' --with-build-proxy
    fi
  fi
  if [ -n "$GW_DIR" ]; then
    printf '%s\n' --with-agent-proxy
  fi
  if [ "$NO_INTERNET_ISOLATION" = 0 ] && [ -n "$IPL_DIR" ] && [ -n "$GW_DIR" ]; then
    printf '%s\n' --with-internet-proxy
  fi
  if [ "$WITH_COPILOT" = 1 ]; then
    printf '%s\n' --with-copilot
    [ "$NO_BUILD" = 0 ] && printf '%s\n' --with-copilot-egress
  fi
  return 0
}

for rt in "${RUNTIMES[@]}"; do
  if [ "$rt" != chroot ] && [ "$NO_BUILD" = 0 ] && [ -z "$(security_peer "$rt")" ]; then
    note "security on $rt: no second selected container runtime; cross-runtime coverage needs an explicit --security-peer-runtime or will be incomplete"
  fi
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

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/project-sandbox-e2e-all.XXXXXX")"
trap 'rm -rf "$WORK_DIR"' EXIT

# Sets CMD to the run-e2e-tests.sh invocation for runtime $1.
build_cmd() {
  local line
  local args=()
  while IFS= read -r line; do
    args+=("$line")
  done < <(runtime_args "$1")
  CMD=("$RUNNER" "${args[@]}" ${COMMON_ARGS[@]+"${COMMON_ARGS[@]}"} ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"})
}

# Renders a suite x runtime table from run-e2e-tests.sh "Summary" sections.
# $1 is "plan" or "outcome"; the rest are RUNTIME=OUTPUT_FILE[=EXIT_CODE].
render_table() {
  uv run --no-project python -I - "$@" <<'PY'
import re
import sys

mode, specs = sys.argv[1], sys.argv[2:]
row = re.compile(r"^  (PASS|FAIL|SKIP|RUN)\s+(\S+)(?:  \((.*)\))?$")
runtimes, results, suites, broken = [], {}, [], []
for spec in specs:
    runtime, path, *code = spec.split("=", 2)
    runtimes.append(runtime)
    in_summary = False
    with open(path, errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line == "Summary":
                in_summary = True
                continue
            match = row.match(line) if in_summary else None
            if match:
                status, suite, detail = match.groups()
                if runtime == "chroot" and (detail or "").startswith("pass --with"):
                    # Opt-in suites are never requested for chroot: none can run there.
                    detail = "needs a container runtime"
                results[(runtime, suite)] = (status, detail or "")
                if suite not in suites:
                    suites.append(suite)
    if not in_summary:
        broken.append(f"{runtime}: run-e2e-tests.sh exited {code[0] if code else '?'} without a summary")

width = max([len("Suite"), *map(len, suites)])
cols = [max(len(r), 4) for r in runtimes]
print(("  " + "Suite".ljust(width) + "".join("  " + r.ljust(c) for r, c in zip(runtimes, cols))).rstrip())
print("  " + "-" * width + "".join("  " + "-" * c for c in cols))
counts = {}
for suite in suites:
    cells = []
    for runtime, c in zip(runtimes, cols):
        status = results.get((runtime, suite), ("-", ""))[0]
        counts[status] = counts.get(status, 0) + 1
        cells.append("  " + status.ljust(c))
    print(("  " + suite.ljust(width) + "".join(cells)).rstrip())
print()
order = ("RUN", "PASS", "FAIL", "SKIP")
print("  Totals: " + ", ".join(f"{counts[k]} {k}" for k in order if k in counts))

details = {}
for (runtime, suite), (status, detail) in results.items():
    if status in ("SKIP", "FAIL") and detail:
        details.setdefault((status, suite, detail), []).append(runtime)
for heading, status in (("Failures", "FAIL"), ("Skipped", "SKIP")):
    entries = [(s, d, r) for (st, s, d), r in details.items() if st == status]
    if not entries:
        continue
    print(f"  {heading}:")
    for suite, detail, rts in sorted(entries, key=lambda e: suites.index(e[0])):
        print(f"    {suite} [{', '.join(rts)}]: {detail}")
for line in broken:
    print(f"  ERROR: {line}")
PY
}

# Sweep e2e leftovers on every detected container runtime (chroot leaves none).
CLEANUP_CMD=(uv run python "$ROOT/scripts/e2e-cleanup.py")
CLEANUP_RUNTIMES=0
for rt in "${RUNTIMES[@]}"; do
  if [ "$rt" != chroot ]; then
    CLEANUP_CMD+=(--runtime "$rt")
    CLEANUP_RUNTIMES=1
  fi
done

# --- Test plan ---------------------------------------------------------------
echo "Runtimes to test: ${RUNTIMES[*]}"
echo "Internet proxy:   ${IPL_DIR:-not found} ($([ "$IPL_UP" = 1 ] && echo up || echo down))"
echo "Agent proxy:      ${GW_DIR:-not found} ($([ "$GW_UP" = 1 ] && echo up || echo down))"
echo
PLAN_SPECS=()
for rt in "${RUNTIMES[@]}"; do
  build_cmd "$rt"
  plan_out="$WORK_DIR/plan-$rt.txt"
  if ! "${CMD[@]}" --plan >"$plan_out" 2>&1; then
    echo "ERROR: could not plan the $rt run:" >&2
    sed 's/^/  /' "$plan_out" >&2
    exit 64
  fi
  PLAN_SPECS+=("$rt=$plan_out")
done
echo "========================================"
echo "Test plan"
echo "========================================"
render_table plan "${PLAN_SPECS[@]}"
if [ "$CLEANUP_RUNTIMES" = 1 ]; then
  echo "  Cleanup: e2e-cleanup.py before the first and after the last runtime"
fi
print_notes
echo

RESULT_RT=()
RESULT_RC=()
OUTCOME_SPECS=()
overall=0

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
  build_cmd "$rt"
  echo "========================================"
  echo "Runtime: $rt"
  echo "========================================"
  printf '+'; printf ' %q' "${CMD[@]}"; echo
  if [ "$DRY_RUN" = 1 ]; then
    continue
  fi
  run_out="$WORK_DIR/run-$rt.txt"
  rc=0
  { "${CMD[@]}" 2>&1 | tee "$run_out"; } || rc=$?
  OUTCOME_SPECS+=("$rt=$run_out=$rc")
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

# --- Outcome -----------------------------------------------------------------
echo "========================================"
echo "Outcome"
echo "========================================"
render_table outcome "${OUTCOME_SPECS[@]}"
echo
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
