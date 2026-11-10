"""Subprocess/kernel checks for host locks; no container runtime needed."""

import os
import subprocess
import sys

import pytest
from conftest import ROOT, run, wait_for

from project_sandbox import host_locks

LOCK_CHILD = """
from pathlib import Path
import sys, time
from project_sandbox.host_locks import path_lock
with path_lock('security-e2e', Path(sys.argv[1]), blocking=sys.argv[2] == 'wait'):
    Path(sys.argv[3]).touch()
    time.sleep(float(sys.argv[4]))
"""


def test_live_lock_contention_and_symlink(case_dir):
    target = case_dir / "target"
    lock = host_locks.lock_path("security-e2e", target)
    owner = subprocess.Popen(
        [
            sys.executable,
            "-c",
            LOCK_CHILD,
            str(target),
            "wait",
            str(case_dir / "ready"),
            "30",
        ],
        cwd=ROOT,
    )
    try:
        wait_for(lambda: (case_dir / "ready").exists(), processes=(owner,))
        inode = lock.stat().st_ino
        contender = run(
            [
                sys.executable,
                "-c",
                LOCK_CHILD,
                target,
                "nonblocking",
                case_dir / "entered",
                "0",
            ],
            check=False,
            timeout=5,
        )
        assert contender.returncode != 0 and "BlockingIOError" in contender.stdout
        assert lock.stat().st_ino == inode and not (case_dir / "entered").exists()
    finally:
        owner.terminate()
        owner.wait(timeout=5)
    run(
        [
            sys.executable,
            "-c",
            LOCK_CHILD,
            target,
            "nonblocking",
            case_dir / "released",
            "0",
        ]
    )
    assert (case_dir / "released").exists()
    lock.unlink()
    sentinel = case_dir / "sentinel"
    sentinel.write_text("never truncate")
    lock.symlink_to(sentinel)
    try:
        denied = run(
            [
                sys.executable,
                "-c",
                LOCK_CHILD,
                target,
                "nonblocking",
                case_dir / "symlink-entered",
                "0",
            ],
            check=False,
        )
        assert denied.returncode != 0 and not (case_dir / "symlink-entered").exists()
        assert sentinel.read_text() == "never truncate"
    finally:
        lock.unlink()


def test_host_lock_private_directory_and_inode_validation(case_dir):
    target = case_dir / "target"
    lock = host_locks.lock_path("security-e2e", target)
    # A FIFO opened O_RDWR does not block: this detects missing regular-file
    # validation without opening a device or touching another user's files.
    os.mkfifo(lock, 0o600)
    try:
        result = run(
            [
                sys.executable,
                "-c",
                LOCK_CHILD,
                target,
                "nonblocking",
                case_dir / "fifo-entered",
                "0",
            ],
            check=False,
            timeout=5,
        )
        if result.returncode == 0 or lock.parent.stat().st_mode & 0o077:
            pytest.xfail(
                "TODO: host locks lack private-directory and regular-inode validation"
            )
        assert not (case_dir / "fifo-entered").exists()
    finally:
        lock.unlink()


def test_host_lock_wait_is_bounded(case_dir):
    target = case_dir / "target"
    lock = host_locks.lock_path("security-e2e", target)
    owner = subprocess.Popen(
        [
            sys.executable,
            "-c",
            LOCK_CHILD,
            str(target),
            "wait",
            str(case_dir / "ready"),
            "30",
        ],
        cwd=ROOT,
    )
    try:
        wait_for(lambda: (case_dir / "ready").exists(), processes=(owner,))
        try:
            contender = run(
                [
                    sys.executable,
                    "-c",
                    LOCK_CHILD,
                    target,
                    "wait",
                    case_dir / "entered",
                    "0",
                ],
                check=False,
                timeout=3,
            )
        except subprocess.TimeoutExpired:
            pytest.xfail("TODO: blocking host locks have no bounded wait/diagnostic")
        assert contender.returncode != 0 and not (case_dir / "entered").exists()
    finally:
        owner.terminate()
        owner.wait(timeout=5)
        lock.unlink(missing_ok=True)
