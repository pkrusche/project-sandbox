#!/usr/bin/env bash
# Run every supported end-to-end suite on every runtime supported by this host.
#
# Runtimes are detected with `project-sandbox doctor --json`: each of
# apple-container, docker, podman and chroot whose check reports "ok" is
# exercised by one scripts/run-e2e-tests.sh invocation, one after another.
#
# Opt-in suites are enabled per container runtime when they can work here:
#   proxy-endpoints, egress-sidecar   always (non-billable; need network + builds)
#   security                         always with builds; XFAIL/SKIP pass unless
#                                     --strict-security-coverage is given. Its
#                                     cross-runtime policy-edit test needs a second
#                                     live runtime and runs only with
#                                     --security-peer-runtime NAME (skipped otherwise)
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
#   colima                            colima on PATH and the profile (default
#                                     project-sandbox) exists; runs once, with the
#                                     first container runtime. A stopped profile is
#                                     started before the plan (`colima start
#                                     --profile NAME --activate=false
#                                     --ssh-config=false`, its stored configuration,
#                                     leaving the active Docker context alone) and
#                                     stopped again afterwards. A missing profile is
#                                     never created: create it with `project-sandbox
#                                     colima init`. Pass --no-colima to leave it out
#
# Preflight, before any suite runs:
#   Docker context    the active context (and DOCKER_HOST/DOCKER_CONTEXT) is shown.
#                     The docker runtime and plain `docker` calls use it, so the
#                     run is refused when the docker runtime is selected and the
#                     context is a Colima one (colima, colima-*, or an endpoint
#                     under ~/.colima): it would test a Colima VM, not Docker.
#                     Starting the Colima profile must not change it; if it does,
#                     the run stops.
#   Colima profile    the profile must use Colima's docker runtime, and
#                     `project-sandbox doctor --colima-profile NAME` must confirm
#                     its context (colima-NAME) is that profile's Docker socket and
#                     its engine answers, and the VM must resolve a public name
#                     (registry-1.docker.io) within 45s. Otherwise the colima suite
#                     is not run and the check is reported as FAIL colima-preflight.
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
#       [--no-replace-existing] [--no-incremental] [--strict-security-coverage]
#       [--colima-profile NAME] [--colima-start-timeout SECONDS] [--no-colima]
#       [--security-peer-runtime NAME]
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
#   --colima-profile NAME
#                     Colima profile for the colima suite (default: project-sandbox)
#   --colima-start-timeout SECONDS
#                     give up on starting a stopped profile after this long
#                     (default 600); the half-started profile is stopped again
#   --no-colima       skip the colima suite and never start a Colima profile
#   --security-peer-runtime NAME
#                     a second detected container runtime for the security suite's
#                     cross-runtime policy-edit test, forwarded to every other
#                     runtime's run. Off by default: the test then skips. Both
#                     runtimes start a global sidecar, so NAME must be usable on
#                     its own (for docker, check the active Docker context)
#   --no-build        forwarded; also drops opt-in suites that require builds
#   --keep            forwarded to run-e2e-tests.sh
#   --no-replace-existing
#                     refuse to replace an existing global egress sidecar. By
#                     default --replace-existing is forwarded, so the
#                     egress-sidecar, copilot-egress, colima and security suites replace
#                     (and stop) a sidecar that is already running instead of
#                     failing; running sidecar-mode sandboxes lose egress
#   --strict-security-coverage
#                     forwarded: fail security on known limitations and skips too
#   --no-incremental  rerun every suite. By default --incremental is forwarded:
#                     suites that already passed on the current source and
#                     configuration are skipped, per runtime (state in .e2e-state/)
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
WITH_COLIMA=1
COLIMA_PROFILE="project-sandbox"
COLIMA_START_TIMEOUT=600
SECURITY_PEER=""
NO_INTERNET_ISOLATION=0
NO_BUILD=0
DRY_RUN=0
COMMON_ARGS=()
EXTRA_ARGS=()

CLEANUP=1
REPLACE_EXISTING=1
INCREMENTAL=1

usage() { sed -n '2,${/^#/!q;p;}' "$0"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --runtimes) RUNTIMES_FILTER="${2:?--runtimes needs a value}"; shift 2 ;;
    --internet-proxy-dir) INTERNET_PROXY_DIR="${2:?--internet-proxy-dir needs a value}"; shift 2 ;;
    --agentgateway-dir) AGENTGATEWAY_DIR="${2:?--agentgateway-dir needs a value}"; shift 2 ;;
    --no-copilot) WITH_COPILOT=0; shift ;;
    --no-colima) WITH_COLIMA=0; shift ;;
    --colima-profile) COLIMA_PROFILE="${2:?--colima-profile needs a value}"; shift 2 ;;
    --colima-start-timeout)
      COLIMA_START_TIMEOUT="${2:?--colima-start-timeout needs a value}"; shift 2
      case "$COLIMA_START_TIMEOUT" in
        ''|*[!0-9]*|0) echo "ERROR: --colima-start-timeout needs a positive number of seconds." >&2; exit 64 ;;
      esac
      ;;
    --security-peer-runtime) SECURITY_PEER="${2:?--security-peer-runtime needs a value}"; shift 2 ;;
    --no-internet-proxy-isolation) NO_INTERNET_ISOLATION=1; shift ;;
    --no-build) NO_BUILD=1; COMMON_ARGS+=(--no-build); shift ;;
    --keep) COMMON_ARGS+=(--keep); shift ;;
    --no-replace-existing) REPLACE_EXISTING=0; shift ;;
    --incremental) INCREMENTAL=1; shift ;;
    --no-incremental) INCREMENTAL=0; shift ;;
    --strict-security-coverage) COMMON_ARGS+=(--strict-security-coverage); shift ;;
    --base-image) COMMON_ARGS+=(--base-image "${2:?--base-image needs a value}"); shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --no-cleanup) CLEANUP=0; shift ;;
    --) shift; EXTRA_ARGS+=("$@"); break ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 64 ;;
  esac
done

command -v uv >/dev/null 2>&1 || { echo "ERROR: uv not found on PATH." >&2; exit 64; }

if [ "$REPLACE_EXISTING" = 1 ]; then
  COMMON_ARGS+=(--replace-existing)
fi
if [ "$INCREMENTAL" = 1 ]; then
  COMMON_ARGS+=(--incremental)
fi

NOTES=()
note() { NOTES+=("$1"); }

# Absolute path of an existing directory, or empty when it does not exist.
resolve_dir() { (cd "$1" 2>/dev/null && pwd) || true; }

# True when something accepts TCP connections on HOST PORT. Loopback refusals
# return immediately, so no timeout helper is needed.
port_open() { (exec 3<>"/dev/tcp/$1/$2") >/dev/null 2>&1; }

# Runs "$@" in the foreground (Ctrl-C still reaches it) and kills it after $1
# seconds; exits 124 on timeout, like coreutils timeout (absent on macOS).
run_with_timeout() {
  local secs="$1"
  shift
  uv run --no-project python -I -c '
import subprocess, sys
try:
    sys.exit(subprocess.run(sys.argv[2:], timeout=float(sys.argv[1])).returncode)
except subprocess.TimeoutExpired:
    print("timed out after " + sys.argv[1] + "s: " + " ".join(sys.argv[2:]), file=sys.stderr)
    sys.exit(124)
except KeyboardInterrupt:
    sys.exit(130)
' "$secs" "$@"
}

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

if [ -n "$SECURITY_PEER" ]; then
  peer_ok=0
  for have in ${RUNTIMES[@]+"${RUNTIMES[@]}"}; do
    [ "$have" = "$SECURITY_PEER" ] && [ "$have" != chroot ] && peer_ok=1
  done
  if [ "$peer_ok" = 0 ]; then
    echo "ERROR: --security-peer-runtime '$SECURITY_PEER' is not an available container runtime on this host." >&2
    exit 64
  fi
fi

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

# --- Docker context ----------------------------------------------------------
# The docker runtime (and every plain `docker` call of the suites) targets the
# active Docker context; only the colima suite names its own (colima-PROFILE).
DOCKER_CTX=""
DOCKER_CTX_HOST=""

# The active context's name, or empty when docker is missing or cannot tell.
docker_context_name() {
  command -v docker >/dev/null 2>&1 || return 0
  docker context show 2>/dev/null || true
}

# The active context's Docker endpoint (DOCKER_HOST when that is set).
docker_context_host() {
  command -v docker >/dev/null 2>&1 || return 0
  docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null || true
}

# True when context $1 / endpoint $2 is a Colima profile's engine.
is_colima_context() {
  case "$1" in colima|colima-*) return 0 ;; esac
  case "$2" in */.colima/*) return 0 ;; esac
  return 1
}

describe_docker_context() {
  local text="${DOCKER_CTX:-unknown}"
  [ -n "$DOCKER_CTX_HOST" ] && text="$text ($DOCKER_CTX_HOST)"
  [ -n "${DOCKER_HOST:-}" ] && text="$text; DOCKER_HOST is set"
  [ -n "${DOCKER_CONTEXT:-}" ] && text="$text; DOCKER_CONTEXT=$DOCKER_CONTEXT"
  printf '%s' "$text"
}

DOCKER_CTX="$(docker_context_name)"
DOCKER_CTX_HOST="$(docker_context_host)"
if [ -n "$DOCKER_CTX$DOCKER_CTX_HOST" ] && is_colima_context "$DOCKER_CTX" "$DOCKER_CTX_HOST"; then
  docker_selected=0
  for rt in "${RUNTIMES[@]}"; do
    [ "$rt" = docker ] && docker_selected=1
  done
  if [ "$docker_selected" = 1 ]; then
    echo "ERROR: the active Docker context is $(describe_docker_context), a Colima engine." >&2
    echo "  The docker runtime would test that Colima VM instead of Docker. Switch back" >&2
    echo "  (for example 'docker context use desktop-linux', and unset DOCKER_HOST /" >&2
    echo "  DOCKER_CONTEXT), or leave docker out with --runtimes." >&2
    exit 64
  fi
  note "the active Docker context is $(describe_docker_context), a Colima engine; plain docker calls go to that VM"
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

if [ -z "$GW_DIR" ] || [ ! -f "$GW_DIR/pyproject.toml" ]; then
  GW_DIR=""
  note "agent proxy not available: no agentgateway-locally checkout at $AGENTGATEWAY_DIR; skipping agent-proxy-isolation, agent-proxy and internet-proxy-isolation"
elif port_open "$AGENT_PROXY_HOST" "$AGENT_PROXY_PORT"; then
  GW_UP=1
else
  note "agent proxy not up: nothing listening on $AGENT_PROXY_HOST:$AGENT_PROXY_PORT; agent-proxy-isolation, agent-proxy and internet-proxy-isolation still run and will fail unless you start it ('uv run agw up' in $GW_DIR)"
fi

if [ "$NO_INTERNET_ISOLATION" = 1 ] && [ -n "$IPL_DIR" ] && [ -n "$GW_DIR" ]; then
  note "internet-proxy-isolation disabled by --no-internet-proxy-isolation"
fi
if [ "$WITH_COPILOT" = 0 ]; then
  note "copilot and copilot-egress disabled by --no-copilot"
fi
if [ "$NO_BUILD" = 1 ]; then
  note "--no-build: skipping proxy-endpoints, egress-sidecar, security, build-proxy, copilot-egress and colima"
fi

# --- Colima detection -------------------------------------------------------
# The colima suite targets its own profile, not a --runtime, so it runs once:
# with the first selected container runtime (chroot only when nothing else is).
# COLIMA_STATE: off | absent | error | missing | running | stopped.
COLIMA_STATE=off
COLIMA_STARTED=0
COLIMA_RT=""
for rt in "${RUNTIMES[@]}"; do
  if [ "$rt" != chroot ]; then COLIMA_RT="$rt"; break; fi
done
[ -n "$COLIMA_RT" ] || COLIMA_RT="${RUNTIMES[0]}"

# Prints the profile's lowercased status and its Colima runtime (tab-separated)
# from `colima list --json`, or "missing".
colima_profile_status() {
  printf '%s' "$1" | uv run --no-project python -I -c '
import json, sys
want = sys.argv[1]
for line in sys.stdin:
    try:
        item = json.loads(line)
    except ValueError:
        continue
    for entry in item if isinstance(item, list) else [item]:
        if isinstance(entry, dict) and entry.get("name") == want:
            status = str(entry.get("status") or "unknown").lower()
            print(status + "\t" + str(entry.get("runtime") or "unknown"))
            sys.exit(0)
print("missing")
' "$COLIMA_PROFILE"
}

if [ "$WITH_COLIMA" = 0 ]; then
  note "colima disabled by --no-colima"
elif [ "$NO_BUILD" = 0 ]; then
  if ! command -v colima >/dev/null 2>&1; then
    COLIMA_STATE=absent
    note "colima not available: colima not on PATH; skipping colima"
  elif ! COLIMA_LIST="$(colima list --json 2>&1)"; then
    COLIMA_STATE=error
    note "colima not available: 'colima list --json' failed: ${COLIMA_LIST:-no output}; skipping colima"
  else
    IFS="$(printf '\t')" read -r colima_status colima_runtime <<<"$(colima_profile_status "$COLIMA_LIST")"
    # Only a reported runtime other than docker disqualifies the profile.
    if [ "$colima_status" != missing ] && [ "${colima_runtime:-unknown}" != unknown ] \
      && [ "$colima_runtime" != docker ]; then
      colima_status=wrong-runtime
    fi
    case "$colima_status" in
      running) COLIMA_STATE=running ;;
      wrong-runtime)
        COLIMA_STATE=error
        note "colima profile '$COLIMA_PROFILE' uses Colima's ${colima_runtime:-unknown} runtime, not docker; skipping colima. Create a project-sandbox profile with: uv run project-sandbox colima init --colima-profile NAME"
        ;;
      missing)
        COLIMA_STATE=missing
        note "colima profile '$COLIMA_PROFILE' does not exist; skipping colima. Create it with: uv run project-sandbox colima init --colima-profile $COLIMA_PROFILE --workspace-root $(dirname "$ROOT")"
        ;;
      *)
        COLIMA_STATE=stopped
        note "colima profile '$COLIMA_PROFILE' is not running; it is started before the plan and stopped again afterwards"
        ;;
    esac
  fi
fi

# True when this invocation passes the colima suite to run-e2e-tests.sh.
colima_selected() { [ "$COLIMA_STATE" = running ] || [ "$COLIMA_STATE" = stopped ]; }

# --- Per-runtime argument assembly -----------------------------------------
# Prints one argument per line for the run-e2e-tests.sh invocation of $1.
runtime_args() {
  local rt="$1"
  printf '%s\n' --runtime "$rt"
  if [ "$rt" = "$COLIMA_RT" ] && colima_selected; then
    printf '%s\n' --with-colima --colima-profile "$COLIMA_PROFILE"
  fi
  [ "$rt" = chroot ] && return 0
  [ -n "$IPL_DIR" ] && printf '%s\n' --internet-proxy-dir "$IPL_DIR"
  [ -n "$GW_DIR" ] && printf '%s\n' --agentgateway-dir "$GW_DIR"
  if [ "$NO_BUILD" = 0 ]; then
    printf '%s\n' --with-proxy-endpoints --with-egress-sidecar --with-security
    # Opt-in: the peer runs its own sidecar, and is pointless on itself.
    if [ -n "$SECURITY_PEER" ] && [ "$SECURITY_PEER" != "$rt" ]; then
      printf '%s\n' --security-peer-runtime "$SECURITY_PEER"
    fi
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

if [ "$NO_BUILD" = 0 ] && [ -z "$SECURITY_PEER" ]; then
  note "security: the cross-runtime policy-edit test skips; pass --security-peer-runtime NAME to run it"
fi
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

RESULT_RT=()
RESULT_RC=()
overall=0

# The flags project-sandbox passes on every start (colima_profile.PROTECTIVE_FLAGS):
# keep the active Docker context and ~/.ssh/config as they are.
COLIMA_START_FLAGS=(--activate=false --ssh-config=false)

# Starts a stopped Colima profile so the colima suite is not skipped. Run
# before planning: run-e2e-tests.sh decides the suite by asking the profile.
start_colima() {
  [ "$COLIMA_STATE" = stopped ] || return 0
  echo "========================================"
  echo "Colima start ($COLIMA_PROFILE)"
  echo "========================================"
  echo "+ colima start --profile $COLIMA_PROFILE ${COLIMA_START_FLAGS[*]}"
  if [ "$DRY_RUN" = 1 ]; then
    echo
    return 0
  fi
  local rc=0
  # Owned from here on: a failed, timed-out or interrupted start can leave the
  # VM half-booted, and stop_colima (also the EXIT trap) must stop it again.
  COLIMA_STARTED=1
  run_with_timeout "$COLIMA_START_TIMEOUT" \
    colima start --profile "$COLIMA_PROFILE" "${COLIMA_START_FLAGS[@]}" || rc=$?
  RESULT_RT+=("colima-start")
  RESULT_RC+=("$rc")
  if [ "$rc" = 0 ]; then
    COLIMA_STATE=running
  else
    COLIMA_STATE=error
    if [ "$rc" = 124 ]; then
      note "colima profile '$COLIMA_PROFILE' did not start within ${COLIMA_START_TIMEOUT}s; skipping colima. A start stuck at 'boot scripts must have finished' usually means the VM cannot resolve names (see the DNS section of docs/development.md)"
    else
      note "colima profile '$COLIMA_PROFILE' did not start (exit $rc); skipping colima"
    fi
    overall=1
  fi
  echo
}

# Prints the colima section of `doctor --json --colima-profile NAME` and fails
# unless the profile's own check is ok and no colima check failed.
check_colima_doctor() {
  local json
  json="$(cd "$ROOT" && uv run project-sandbox doctor --json --colima-profile "$COLIMA_PROFILE" 2>/dev/null)" || true
  printf '%s' "$json" | uv run --no-project python -I -c '
import json, sys
want = "colima (" + sys.argv[1] + ")"
try:
    checks = json.load(sys.stdin).get("checks", [])
except ValueError:
    print("  FAIL  project-sandbox doctor --json gave no usable output")
    sys.exit(1)
checks = [c for c in checks if c.get("section") == "colima"]
for c in checks:
    hint = " -> " + c["hint"] if c.get("hint") else ""
    status = (c.get("status") or "?").upper()
    print("  %-5s %s: %s%s" % (status, c.get("name"), c.get("detail"), hint))
own = [c for c in checks if c.get("name") == want]
if not own:
    print("  FAIL  doctor reported no " + want + " check")
ok = bool(own) and own[0].get("status") == "ok"
sys.exit(0 if ok and not any(c.get("status") == "fail" for c in checks) else 1)
' "$COLIMA_PROFILE"
}

# Fails unless the profile's VM resolves a public name. Lima's boot scripts,
# image pulls and builds all need it; a dead resolver makes them hang or time
# out (i/o timeout on the nameserver) instead of failing clearly.
check_colima_dns() {
  local out rc=0
  out="$(run_with_timeout 45 colima ssh --profile "$COLIMA_PROFILE" -- \
    sh -c 'getent hosts registry-1.docker.io || { grep "^nameserver" /etc/resolv.conf; exit 1; }' 2>&1)" || rc=$?
  if [ "$rc" = 0 ]; then
    echo "  OK    colima ($COLIMA_PROFILE) DNS: registry-1.docker.io resolves in the VM"
    return 0
  fi
  echo "  FAIL  colima ($COLIMA_PROFILE) DNS: the VM cannot resolve registry-1.docker.io (exit $rc)"
  printf '%s\n' "$out" | sed -n '1,5s/^/          /p'
  echo "          -> Lima's resolver is the gateway (192.168.5.2); see the DNS section of docs/development.md"
  return 1
}

# Checks, before any suite runs, that starting Colima left the active Docker
# context alone and that the Colima profile is the engine the suite expects.
preflight() {
  echo "========================================"
  echo "Preflight"
  echo "========================================"
  echo "Docker context:   $(describe_docker_context)"
  if [ "$DRY_RUN" = 0 ] && [ -n "$DOCKER_CTX$DOCKER_CTX_HOST" ]; then
    local now now_host
    now="$(docker_context_name)"
    now_host="$(docker_context_host)"
    if [ "$now" != "$DOCKER_CTX" ] || [ "$now_host" != "$DOCKER_CTX_HOST" ]; then
      echo "ERROR: the active Docker context changed from ${DOCKER_CTX:-?} ($DOCKER_CTX_HOST)" >&2
      echo "  to ${now:-?} ($now_host) while preparing the run. Restore it with" >&2
      echo "  'docker context use ${DOCKER_CTX:-default}' and retry." >&2
      exit 1
    fi
  fi
  if colima_selected; then
    echo "Colima profile:   $COLIMA_PROFILE (context colima-$COLIMA_PROFILE)"
    if [ "$COLIMA_STATE" != running ]; then
      echo "+ uv run project-sandbox doctor --json --colima-profile $COLIMA_PROFILE  (after the start)"
    elif check_colima_doctor && check_colima_dns; then
      :
    else
      COLIMA_STATE=error
      RESULT_RT+=("colima-preflight")
      RESULT_RC+=(1)
      overall=1
      note "colima profile '$COLIMA_PROFILE' failed the preflight check; skipping colima (see Preflight)"
    fi
  fi
  echo
}

# Stops the profile again, but only when this script started it.
stop_colima() {
  [ "$COLIMA_STARTED" = 1 ] || return 0
  COLIMA_STARTED=0
  echo "========================================"
  echo "Colima stop ($COLIMA_PROFILE)"
  echo "========================================"
  echo "+ colima stop --profile $COLIMA_PROFILE"
  local rc=0
  colima stop --profile "$COLIMA_PROFILE" || rc=$?
  RESULT_RT+=("colima-stop")
  RESULT_RC+=("$rc")
  [ "$rc" = 0 ] || overall=1
  echo
}

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/project-sandbox-e2e-all.XXXXXX")"
# An interrupted run still stops a profile it started.
trap 'stop_colima; rm -rf "$WORK_DIR"' EXIT

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
  local colima_rt=""
  colima_selected && colima_rt="$COLIMA_RT"
  COLIMA_RT_FOR_TABLE="$colima_rt" uv run --no-project python -I - "$@" <<'PY'
import os
import re
import sys

mode, specs = sys.argv[1], sys.argv[2:]
colima_rt = os.environ.get("COLIMA_RT_FOR_TABLE", "")
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
                if (
                    suite == "colima"
                    and colima_rt
                    and runtime != colima_rt
                    and (detail or "").startswith("pass --with")
                ):
                    # The colima suite is requested for one runtime only.
                    detail = f"runs once, with {colima_rt}"
                elif runtime == "chroot" and (detail or "").startswith("pass --with"):
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

if [ "$CLEANUP" = 0 ]; then
  note "e2e-cleanup.py skipped by --no-cleanup"
  CLEANUP_RUNTIMES=0
fi

# --- Test plan ---------------------------------------------------------------
echo "Runtimes to test: ${RUNTIMES[*]}"
echo "Internet proxy:   ${IPL_DIR:-not found} ($([ "$IPL_UP" = 1 ] && echo up || echo down))"
echo "Agent proxy:      ${GW_DIR:-not found} ($([ "$GW_UP" = 1 ] && echo up || echo down))"
echo "Colima profile:   $COLIMA_PROFILE ($COLIMA_STATE)"
echo "Docker context:   $(describe_docker_context)"
echo
start_colima
preflight
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

OUTCOME_SPECS=()

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
  [ "$COLIMA_STATE" = stopped ] && echo "+ colima stop --profile $COLIMA_PROFILE" && echo
  echo "Dry run: nothing executed."
  exit 0
fi
stop_colima

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
