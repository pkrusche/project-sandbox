#!/bin/bash
# Spike: isolated sessions with a host-owned jj workspace and an agent-private
# throwaway repository over the same files.
#
# Everything happens under one fresh temporary directory, which is printed at
# the end and can be deleted. HOME and XDG_CONFIG_HOME point inside it, so jj
# and git read no user config and write nothing outside it.
#
# The container's bind mounts over /workspace/.jj and /workspace/.git are
# simulated by swapping directories. No container is started.
set -euo pipefail

S="$(mktemp -d "${TMPDIR:-/tmp}/isolated-spike.XXXXXX")"
export HOME="$S/home" XDG_CONFIG_HOME="$S/home/.config"
export JJ_USER=spike JJ_EMAIL=spike@example.invalid
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
mkdir -p "$XDG_CONFIG_HOME"
cd "$S"

fails=0
check() { # check "<description>" <command...>
  local what="$1"; shift
  if "$@" >/dev/null 2>&1; then echo "PASS  $what"; else echo "FAIL  $what"; fails=$((fails + 1)); fi
}
step() { echo; echo "== $*"; }

jj --version

step "Main repository (colocated), with committed and uncommitted work"
mkdir main
(
  cd main
  jj git init --colocate . 2>&1 | sed 's/^/    /'
  printf 'hello\n' > a.txt
  printf 'ignored.log\n' > .gitignore
  echo x > ignored.log
  jj commit -m "base work" 2>&1 | sed 's/^/    /'
  printf 'wip\n' > wip.txt
)

step "1. Host workspace W, non-colocated, started at main's working copy"
jj -R main workspace add --no-colocate -r @ --name sess1 W 2>&1 | sed 's/^/    /'
check "W has the committed file" test -f W/a.txt
check "W has main's uncommitted file" test -f W/wip.txt
check "W has no ignored file" test ! -e W/ignored.log
check "W has no .git" test ! -e W/.git

step "2. Build the agent-private repository before any agent exists"
mkdir state
mv W/.jj state/host-jj
(
  cd W
  jj git init --colocate . 2>&1 | sed 's/^/    /'
  jj commit -m "session base" 2>&1 | sed 's/^/    /'
)
mv W/.jj state/agent-jj
mv W/.git state/agent-git
mv state/host-jj W/.jj
mkdir W/.git   # empty placeholder: the bind-mount target on the host

step "3. Host view right after setup (expect: no changes)"
jj -R W st 2>&1 | sed 's/^/    /'
check "host workspace is clean after setup" \
  bash -c 'test -z "$(jj -R W diff --summary 2>/dev/null)"'

step "4. Simulated session: agent sees only its private .jj and .git"
mv W/.jj state/host-jj
rmdir W/.git
mv state/agent-jj W/.jj
mv state/agent-git W/.git
(
  cd W
  check "agent jj sees one base commit and a clean tree" \
    bash -c 'test -z "$(jj diff --summary 2>/dev/null)"'
  printf 'hello agent\n' > a.txt
  mkdir sub
  echo new > sub/n.txt
  jj commit -m "agent commit 1" 2>&1 | sed 's/^/    /'
  echo more >> sub/n.txt
  ln -s /etc/passwd link
  rm wip.txt
  check "agent git works in its private repo" git status --short
  echo "    agent's own log:"
  jj log --no-graph -T 'change_id.short() ++ " " ++ description.first_line() ++ "\n"' 2>&1 | sed 's/^/      /'
)

step "5. Session over: restore the host's metadata; agent metadata is set aside unread"
mv W/.jj state/agent-jj
mv W/.git state/agent-git
mv state/host-jj W/.jj
mkdir W/.git

step "6. Host snapshot, straight into the main repository"
jj -R W st 2>&1 | sed 's/^/    /'
jj -R W describe -m "session sess1 snapshot" 2>&1 | sed 's/^/    /'
summary="$(cd main && jj diff --summary -r 'sess1@' 2>&1)"
echo "$summary" | sed 's/^/    /'
check "modified file recorded" grep -q '^M a.txt' <<<"$summary"
check "new file recorded" grep -q '^A sub/n.txt' <<<"$summary"
check "deleted file recorded" grep -q '^D wip.txt' <<<"$summary"
check "symlink recorded" grep -q '^A link' <<<"$summary"
check "empty .git placeholder not recorded" bash -c '! grep -q "\.git" <<<"$1"' _ "$summary"
check "agent commit message absent from main" \
  bash -c '! jj -R main log --no-graph -r "all()" -T description | grep -q "agent commit 1"'
check "symlink stored as a link, not as file content" \
  bash -c 'cd main && jj diff --git -r "sess1@" link 2>/dev/null | grep -q "new file mode 120000"'

step "7. Main repository afterwards"
jj -R main log --no-graph -r 'all()' \
  -T 'change_id.short() ++ " " ++ working_copies ++ " " ++ description.first_line() ++ "\n"' 2>&1 | sed 's/^/    /'

echo
echo "Config written under the spike HOME:"
find "$HOME" -type f 2>/dev/null | sed 's/^/    /' | head -20
echo
echo "Failures: $fails"
echo "Scratch directory (safe to delete): $S"
exit "$fails"
