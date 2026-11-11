#!/usr/bin/env python3
"""Compute the state key used by ``run-e2e-tests.sh --incremental``.

A suite that passed is recorded under a key made of two parts:

* the source fingerprint: a hash of every tracked file of the checkout, as the
  working copy currently is (uncommitted edits included, ignored files not).
  In a jj repository this is the snapshotted working-copy commit's tree; in a
  plain git checkout it is the tree ``git add -A`` would produce. Two checkouts
  with identical contents share a fingerprint, so describing or rebasing a jj
  change without touching files keeps earlier results valid;
* the configuration: the runtime and every option that changes how a suite
  runs (base image, --no-build, fixture hosts, CA certificates, ...), passed
  as ``NAME=VALUE`` arguments. Options that only select which suites run
  (``--only``, ``--with-*``) are deliberately left out by the caller.

Prints ``<source>/<runtime>-<config>``, a relative directory below the state
directory. ``PROJECT_SANDBOX_E2E_SOURCE_FINGERPRINT`` overrides the source
fingerprint (used by tests).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# jj template printing one line per tracked file: path, type, executable bit
# and object id. Conflicted entries have no normal value and print "conflict".
JJ_FILE_TEMPLATE = (
    'self.path() ++ "\\0" ++ self.file_type() ++ "\\0" '
    '++ if(self.executable(), "x", "-") ++ "\\0" '
    '++ if(self.normal_value(), self.normal_value(), "conflict") ++ "\\n"'
)


class FingerprintError(Exception):
    pass


def _run(argv: list[str], cwd: Path, env: dict[str, str] | None = None) -> bytes:
    proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, check=False)
    if proc.returncode != 0:
        raise FingerprintError(
            f"{' '.join(argv[:3])} failed: {proc.stderr.decode(errors='replace').strip()}"
        )
    return proc.stdout


def jj_fingerprint(root: Path) -> str | None:
    """Tree hash of the jj working copy, or None when ROOT is not a jj workspace."""
    if shutil.which("jj") is None:
        return None
    probe = subprocess.run(
        ["jj", "root", "--ignore-working-copy"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        return None
    # Without --ignore-working-copy jj snapshots the working copy first, so
    # uncommitted edits are part of the fingerprint.
    listing = _run(["jj", "file", "list", "-r", "@", "-T", JJ_FILE_TEMPLATE], cwd=root)
    return "jj-" + hashlib.sha256(listing).hexdigest()


def git_fingerprint(root: Path) -> str | None:
    """Tree id of the git working tree (tracked + untracked, minus ignored)."""
    if shutil.which("git") is None:
        return None
    probe = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        return None
    toplevel = Path(probe.stdout.decode().strip())
    with tempfile.TemporaryDirectory() as tmp:
        # A throwaway index leaves the user's staging area untouched.
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        has_head = (
            subprocess.run(
                ["git", "rev-parse", "--verify", "-q", "HEAD"],
                cwd=toplevel,
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
        if has_head:
            _run(["git", "read-tree", "HEAD"], cwd=toplevel, env=env)
        _run(["git", "add", "-A", "."], cwd=toplevel, env=env)
        tree = _run(["git", "write-tree"], cwd=toplevel, env=env).decode().strip()
    return "git-" + tree


def source_fingerprint(root: Path) -> str:
    override = os.environ.get("PROJECT_SANDBOX_E2E_SOURCE_FINGERPRINT")
    if override:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", override):
            raise FingerprintError(
                "PROJECT_SANDBOX_E2E_SOURCE_FINGERPRINT may only contain "
                "letters, digits, '.', '_' and '-'"
            )
        return override
    for compute in (jj_fingerprint, git_fingerprint):
        fingerprint = compute(root)
        if fingerprint is not None:
            return fingerprint
    raise FingerprintError(
        f"{root} is neither a jj workspace nor a git checkout; "
        "--incremental cannot tell which source the results belong to"
    )


def config_hash(settings: list[str]) -> str:
    digest = hashlib.sha256()
    for setting in settings:
        digest.update(setting.encode())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--runtime", required=True)
    p.add_argument(
        "settings",
        nargs="*",
        metavar="NAME=VALUE",
        help="options that change how suites run",
    )
    args = p.parse_args(argv)
    if not re.fullmatch(r"[a-z0-9-]+", args.runtime):
        p.error(f"invalid runtime name: {args.runtime!r}")
    try:
        source = source_fingerprint(args.root)
    except FingerprintError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    # Keep the directory name short; 32 hex digits are plenty to tell trees apart.
    kind, _, digest = source.partition("-")
    source_dir = f"{kind}-{digest[:32]}" if digest else source
    print(f"{source_dir}/{args.runtime}-{config_hash(args.settings)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
