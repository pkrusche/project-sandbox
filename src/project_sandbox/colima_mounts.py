"""Check that the host paths a Colima launch mounts are visible in the VM.

Docker can bind-mount only what the VM exports. The default Colima profile
exports the home directory and nothing else, so a source under ``/private/tmp``
or ``$TMPDIR`` fails at ``docker run`` with ``bind source path does not exist``.
This module asks the VM itself, read-only, so the launch fails early and names
the path and the Colima mount to add (design.md D8, docs/colima-spikes.md D).
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from .container_cli import Runtime

Runner = Callable[..., subprocess.CompletedProcess]

# Prints each path the VM cannot see from the host, one per line. A path counts
# as exported only when it exists on a host-backed mount: the VM has plain
# directories with the same names (``/private/tmp``) that are not the Mac's.
# Without findmnt the check falls back to existence alone.
_SCRIPT = """
for p in "$@"; do
  if [ -e "$p" ]; then
    if ! command -v findmnt >/dev/null 2>&1; then continue; fi
    case "$(findmnt -T "$p" -n -o FSTYPE 2>/dev/null)" in
      virtiofs|9p|fuse.sshfs) continue ;;
    esac
  fi
  printf "%s\\n" "$p"
done
"""


def mount_sources(argv: Sequence[str]) -> list[Path]:
    """Host sources of every ``--mount type=bind,...`` in ``argv``, in order."""
    sources: list[Path] = []
    for index, token in enumerate(argv[:-1]):
        if token != "--mount":
            continue
        fields = dict(
            part.split("=", 1) for part in argv[index + 1].split(",") if "=" in part
        )
        if fields.get("type") != "bind":
            continue
        source = fields.get("source") or fields.get("src")
        if source:
            sources.append(Path(source))
    return sources


def probe_path(source: Path) -> Path | None:
    """The path to look for in the VM: ``source``, or its nearest existing parent.

    Staging that is created later does not exist on the host yet, but it is
    reachable from the VM exactly when its nearest existing ancestor is, and
    the ancestor is then required to sit on a host-backed mount.
    """
    for candidate in (source, *source.parents):
        if candidate == Path(candidate.anchor):
            return None
        if candidate.exists():
            return candidate
    return None


def missing_in_vm(
    runtime: Runtime,
    sources: Iterable[Path],
    *,
    run: Runner = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
) -> list[Path] | None:
    """Probe paths that the VM cannot see, or ``None`` when it cannot be asked.

    One read-only ``colima ssh`` call covers every path. Returns ``None`` (the
    caller warns) when the ``colima`` binary is not on PATH.
    """
    assert runtime.profile is not None
    probes = list(dict.fromkeys(p for s in sources if (p := probe_path(s))))
    if not probes:
        return []
    if which("colima") is None:
        return None
    try:
        proc = run(
            [
                "colima",
                "ssh",
                "--profile",
                runtime.profile,
                "--",
                "sh",
                "-c",
                _SCRIPT,
                "sh",
                *map(str, probes),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f"Could not ask Colima which paths the VM exports: {exc}")
    if proc.returncode != 0:
        raise SystemExit(
            f"Could not ask Colima profile {runtime.profile!r} which paths the VM "
            f"exports: {(proc.stderr or proc.stdout).strip() or 'colima ssh failed'}"
        )
    missing = {Path(line) for line in proc.stdout.splitlines() if line}
    return [p for p in probes if p in missing]


def not_visible_message(runtime: Runtime, missing: Sequence[Path]) -> str:
    profile = runtime.profile
    lines = [
        (
            f"These host paths are not visible inside Colima profile {profile!r}, "
            "so the sandbox cannot mount them:"
        )
    ]
    lines += [f"  {path}" for path in missing]
    lines += [
        (
            "Colima exports only your home directory by default. Move the project "
            "under it, or add a mount to the profile and restart it:"
        ),
        f"  colima stop --profile {profile}",
        f"  colima start --profile {profile} "
        + " ".join(f"--mount {shlex.quote(str(path))}:w" for path in missing),
    ]
    return "\n".join(lines)


def check(
    runtime: Runtime,
    sources: Iterable[Path],
    *,
    run: Runner = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
    warn: Callable[[str], None] | None = None,
) -> None:
    """Raise ``SystemExit`` naming every mount source the VM cannot see."""
    missing = missing_in_vm(runtime, sources, run=run, which=which)
    if missing is None:
        (warn or (lambda m: print(m, file=sys.stderr)))(
            "[W] colima is not on PATH, so mount visibility was not checked; "
            "Docker will report any missing path when the sandbox starts."
        )
        return
    if missing:
        raise SystemExit(not_visible_message(runtime, missing))
