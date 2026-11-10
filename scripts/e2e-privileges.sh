#!/usr/bin/env bash
# End-to-end check of the session privilege model (isolate-firewall-helpers).
#
# Starts a firewalled headless bash session with a local Internet-proxy
# destination (a plain TCP listener on the host), so the root stage's endpoint
# helper runs. Before launch it plants /workspace/ipaddress.py, socket.py, ...
# markers that would record any import as root. Inside the session it asserts:
#   - sudo -n true and sudo -n .../project-sandbox-init-firewall fail;
#   - /proc/self/status shows NoNewPrivs 1, a zero CapBnd/CapEff and a
#     non-root UID;
#   - iptables -F and ip6tables -F fail;
#   - the session shell is PID 1 (every stage execs);
#   - writes into /workspace/.devcontainer fail (always masked read-only);
# then, while the session waits, the host checks that `exec --user agent`
# lands as agent and records what a plain `exec` lands as. Afterwards no
# marker may have fired, the host .devcontainer holds only its .gitignore, and
# the project has no .project-sandbox.
#
# Usage:
#   scripts/e2e-privileges.sh [--runtime auto|apple-container|docker|podman]
#                             [--base-image IMAGE] [--no-build] [--keep]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

RUNTIME="auto"
BASE_IMAGE="python:3.12-slim"
NO_BUILD=0
KEEP=0

usage() { sed -n '2,19p' "$0"; }

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
case "$RUNTIME" in
  apple-container) RT=container ;;
  docker|podman) RT="$RUNTIME" ;;
  *) echo "ERROR: unsupported --runtime '$RUNTIME' (the chroot backend has no privilege model)" >&2; exit 64 ;;
esac
command -v "$RT" >/dev/null 2>&1 || { echo "ERROR: $RT CLI not found." >&2; exit 64; }
command -v uv >/dev/null 2>&1 || { echo "ERROR: uv not found on PATH." >&2; exit 64; }

# Under the repo: Apple's build VM cannot read the macOS per-user temp dir.
TMP_BASE="$ROOT/.project-sandbox/e2e"
mkdir -p "$TMP_BASE"
TMP_PROJECT="$(mktemp -d "$TMP_BASE/privileges-e2e.XXXXXX")"
LISTENER_PID=""
cleanup() {
  [ -n "$LISTENER_PID" ] && kill "$LISTENER_PID" 2>/dev/null || true
  if [ "$KEEP" = 0 ]; then
    rm -rf "$TMP_PROJECT"
  fi
}
trap cleanup EXIT

fail=0
ok()  { echo "  ok    $*"; }
bad() { echo "  BAD   $*"; fail=1; }

printf "# privileges e2e\n" > "$TMP_PROJECT/README.md"
for module in ipaddress socket shlex ssl pathlib; do
  cat > "$TMP_PROJECT/$module.py" <<EOF
import os
with open("/workspace/MARKER-$module", "a") as marker:
    marker.write("imported by uid %d\n" % os.getuid())
EOF
done

# A TCP listener stands in for the Internet proxy: the host and in-sandbox
# preflights only connect, and the session never sends traffic through it.
PORT_FILE="$TMP_PROJECT/.listener-port"
python3 -I -c '
import socket, sys
server = socket.create_server(("127.0.0.1", 0))
open(sys.argv[1], "w").write(str(server.getsockname()[1]))
while True:
    conn, _ = server.accept()
    conn.close()
' "$PORT_FILE" &
LISTENER_PID=$!
disown "$LISTENER_PID" 2>/dev/null || true
for _ in $(seq 50); do [ -s "$PORT_FILE" ] && break; sleep 0.1; done
PORT="$(cat "$PORT_FILE")"

# The session runs as bash -lc from /workspace. Every check runs from / and
# never starts Python, so the agent itself cannot fire a marker.
prompt=$(cat <<'EOF'
set -u
cd /
out=/workspace/privileges.out
check()      { n="$1"; shift; if "$@" >/dev/null 2>&1; then echo "$n=ok"; else echo "$n=FAIL"; fi; }
check_fails(){ n="$1"; shift; if "$@" >/dev/null 2>&1; then echo "$n=FAIL"; else echo "$n=ok"; fi; }
{
  check_fails sudo_true sudo -n true
  check_fails sudo_firewall sudo -n /usr/local/bin/project-sandbox-init-firewall
  check no_new_privs grep -Eq '^NoNewPrivs:[[:space:]]+1$' /proc/self/status
  check cap_bnd_zero grep -Eq '^CapBnd:[[:space:]]+0+$' /proc/self/status
  check cap_eff_zero grep -Eq '^CapEff:[[:space:]]+0+$' /proc/self/status
  check non_root test "$(id -u)" != 0
  check_fails iptables_flush /usr/sbin/iptables -F
  check_fails ip6tables_flush /usr/sbin/ip6tables -F
  check workspace_cwd test "$(cd /workspace && pwd)" = /workspace
  # root stage -> setpriv -> entrypoint -> bash all exec, so this shell is PID 1.
  check pid1 test "$$" = 1
  # .devcontainer is always masked read-only (private-session-state): an
  # agent cannot plant a devcontainer.json whose initializeCommand the host runs.
  check_fails devcontainer_write sh -c 'echo "{}" > /workspace/.devcontainer/devcontainer.json'
  check_fails devcontainer_mkdir mkdir /workspace/.devcontainer/sub
  grep -E '^(Uid|Gid|Groups|NoNewPrivs|Cap(Inh|Prm|Eff|Bnd|Amb)):' /proc/self/status | sed 's/^/status /'
} > "$out" 2>&1
hostname > /workspace/ready
for _ in $(seq 180); do [ -e /workspace/exec-done ] && exit 0; sleep 1; done
echo "exec_wait=FAIL" >> "$out"
EOF
)

cmd=(
  uv run project-sandbox
  --runtime "$RUNTIME"
  --agent bash
  --prompt-text "$prompt"
  --no-forward-credentials
  --internet-proxy "http://127.0.0.1:$PORT"
)
if [ "$NO_BUILD" = 1 ]; then
  cmd+=(--no-build)
fi
cmd+=("$TMP_PROJECT" "$BASE_IMAGE")

echo "Test project: $TMP_PROJECT"
echo "Configuration: runtime=$RUNTIME base_image=$BASE_IMAGE no_build=$NO_BUILD listener=127.0.0.1:$PORT"
echo
( cd "$ROOT" && "${cmd[@]}" ) > "$TMP_PROJECT/session.log" 2>&1 &
SESSION_PID=$!

for _ in $(seq 900); do
  [ -s "$TMP_PROJECT/ready" ] && break
  kill -0 "$SESSION_PID" 2>/dev/null || break
  sleep 1
done
if [ -s "$TMP_PROJECT/ready" ]; then
  container_ref="$(cat "$TMP_PROJECT/ready")"
  # Docker and Podman set the hostname to the short container ID; Apple
  # container to the container name.
  if ! "$RT" exec "$container_ref" true >/dev/null 2>&1; then
    container_ref="$("$RT" ps --format '{{.Names}}' 2>/dev/null | grep -m1 "$container_ref" || true)"
  fi
  as_agent="$("$RT" exec --user agent "$container_ref" id -un 2>&1 || true)"
  plain="$("$RT" exec "$container_ref" id -un 2>&1 || true)"
  if [ "$as_agent" = agent ]; then ok "exec --user agent lands as agent"; else bad "exec --user agent landed as '$as_agent'"; fi
  echo "  info  a plain exec lands as '$plain'"
else
  bad "session never became ready"
fi
touch "$TMP_PROJECT/exec-done"
if wait "$SESSION_PID"; then ok "sandbox session completed"; else bad "sandbox session failed"; fi

if [ -f "$TMP_PROJECT/privileges.out" ]; then
  for name in sudo_true sudo_firewall no_new_privs cap_bnd_zero cap_eff_zero non_root iptables_flush ip6tables_flush workspace_cwd pid1 devcontainer_write devcontainer_mkdir; do
    if grep -qx "$name=ok" "$TMP_PROJECT/privileges.out"; then ok "$name"; else bad "$name"; fi
  done
  grep -q '=FAIL' "$TMP_PROJECT/privileges.out" && bad "$(grep '=FAIL' "$TMP_PROJECT/privileges.out" | tr '\n' ' ')"
  sed -n 's/^status /  info  /p' "$TMP_PROJECT/privileges.out"
else
  bad "privileges.out was not written"
fi
if grep -q 'Firewall initialized' "$TMP_PROJECT/session.log" || grep -q 'Policy: Internet proxy only' "$TMP_PROJECT/session.log"; then
  ok "firewall initialized before the session"
fi
# The host side of the mask: .devcontainer holds only its .gitignore, and the
# launch wrote no generated state into the project.
dc_entries="$(ls -A "$TMP_PROJECT/.devcontainer" 2>/dev/null | tr '\n' ' ')"
if [ "$dc_entries" = ".gitignore " ]; then ok "host .devcontainer holds only .gitignore"; else bad "host .devcontainer holds: $dc_entries"; fi
if [ -e "$TMP_PROJECT/.project-sandbox" ]; then bad "the launch wrote $TMP_PROJECT/.project-sandbox"; else ok "no .project-sandbox in the project"; fi

markers=("$TMP_PROJECT"/MARKER-*)
if [ -e "${markers[0]}" ]; then
  bad "workspace modules were imported: $(cat "$TMP_PROJECT"/MARKER-* | tr '\n' ' ')"
else
  ok "no workspace module was imported"
fi

echo
if [ "$fail" = 0 ]; then
  echo "PASS"
  exit 0
fi
echo "Session log:"
sed 's/^/  | /' "$TMP_PROJECT/session.log" | tail -40
if [ "$KEEP" = 1 ]; then
  echo "FAIL - test project kept for debugging: $TMP_PROJECT"
else
  echo "FAIL - test project will be removed (use --keep to retain it): $TMP_PROJECT"
fi
exit 1
