#!/usr/bin/env bash
# Executed by --agent bash inside the real sandbox, with /workspace writable.
set -euo pipefail
export LC_ALL=C
test "$(id -u)" != 0
cd /workspace

git_fixture() {
  git -c safe.directory=/workspace -c core.hooksPath=/dev/null \
    -c commit.gpgsign=false -c user.name='VCS E2E' \
    -c user.email=vcs-e2e@example.invalid "$@"
}

deny_git_write() {
  local log="$1"
  shift
  if git_fixture "$@" > "$log" 2>&1; then
    echo "ERROR: git $* unexpectedly succeeded" >&2
    exit 1
  fi
  # Reject false positives such as missing identity or 'nothing to commit'.
  grep -Eiq 'Read-only file system|Permission denied' "$log"
}

case "${1:?expected access mode}" in
  full)
    # The image supplies jj, so this suite needs only Git on the host.
    jj git init --colocate /workspace
    printf 'private Git metadata\n' > .git/vcs-access-canary
    printf 'private jj metadata\n' > .jj/vcs-access-canary
    printf 'committed in container\n' > full-access.txt
    git_fixture add full-access.txt
    git_fixture commit -m 'vcs-access: commit from full sandbox'
    git_fixture rev-parse HEAD > full-commit.txt
    ;;
  read-only)
    test "$(cat .git/vcs-access-canary)" = 'private Git metadata'
    test "$(cat .jj/vcs-access-canary)" = 'private jj metadata'
    git_fixture log --format=%s > commit-titles.txt
    printf 'source stays writable\n' > read-only-edit.txt
    deny_git_write read-only-add.log add read-only-edit.txt
    deny_git_write read-only-commit.log commit --allow-empty -m 'must not persist'
    # Both stores must reject direct writes as well as Git commands.
    python3 -I - <<'PY'
import errno
from pathlib import Path
for root in (".git", ".jj"):
    canary = Path(root) / "vcs-access-canary"
    for name, operation in (
        ("write", lambda: canary.write_text("tampered\n")),
        ("unlink", canary.unlink),
        ("rename file", lambda: canary.rename("stolen-metadata")),
        ("rename store", lambda: Path(root).rename(root + "-moved")),
        ("remove store", lambda: Path(root).rmdir()),
    ):
        try:
            operation()
        except OSError as error:
            if error.errno not in (errno.EROFS, errno.EACCES, errno.EPERM, errno.EBUSY, errno.EXDEV):
                raise
        else:
            raise RuntimeError(f"metadata {name} succeeded: {root}")
PY
    ;;
  hidden)
    python3 -I - <<'PY'
import json
import os
from pathlib import Path
paths = json.loads(Path("metadata-files.json").read_text())
assert {".git/HEAD", ".git/vcs-access-canary", ".jj/vcs-access-canary"} <= set(paths)
for path in paths:
    assert not os.path.lexists(path), f"original metadata visible: {path}"
# Empty mount-point directories remain; no original contents may be visible.
for root in (".git", ".jj"):
    assert Path(root).is_dir(), f"missing mask: {root}"
    assert not list(Path(root).iterdir()), f"nonempty metadata mask: {root}"
PY
    if git_fixture rev-parse --git-dir; then
      echo 'ERROR: Git found a repository with hidden metadata' >&2
      exit 1
    fi
    printf 'source stays writable\n' > hidden-access.txt
    ;;
  *) echo "Unknown access mode: $1" >&2; exit 64 ;;
esac
printf 'ok\n' > "$1.ok"
