"""Create and maintain a dedicated Colima profile that exports only what the
sandbox mounts (``project-sandbox colima init`` and ``colima add-root``).

A default Colima profile exports the whole home directory read-write. Here the
profile exports exactly the workspace roots the user names plus the four state
subtrees the sandbox bind-mounts. Everything that runs ``colima`` takes its
runner as a parameter and the ``colima start`` command is plain data
(:func:`build_start_argv`), so the behavior is testable without a VM.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import colima_mounts, container_cli, egress_sidecar, mount_storage, observability
from .colima_mounts import Export

Runner = Callable[..., subprocess.CompletedProcess]

DEFAULT_CPU = 4
DEFAULT_MEMORY_GIB = 8.0
# Flags `colima start` must still offer; a rename must fail before a VM starts.
REQUIRED_START_FLAGS = (
    "--activate",
    "--ssh-config",
    "--mount",
    "--mount-type",
    "--vm-type",
)
SYSTEM_DIRS = ("/", "/Users", "/System", "/Library", "/private", "/var", "/etc")
# Relative to the home directory.
CREDENTIAL_DIRS = (".ssh", ".aws", ".gnupg", ".config/gcloud", ".docker")
RECORD_DIRNAME = "colima-profiles"


class ProfileError(SystemExit):
    """A refusal with a message; the CLI prints it and exits non-zero."""


@dataclass(frozen=True)
class Size:
    cpu: int = DEFAULT_CPU
    memory: float = DEFAULT_MEMORY_GIB
    disk: int | None = None


def state_root() -> Path:
    """``$XDG_STATE_HOME/project-sandbox``: parent of every sandbox state tree."""
    return observability.state_dir().parent


# --- Root validation -------------------------------------------------------


def _contains(parent: Path, child: Path) -> bool:
    return parent == child or parent in child.parents


def validate_root(
    path: str | Path,
    *,
    home: Path | None = None,
    state: Path | None = None,
) -> Path:
    """Resolve ``path`` and refuse it unless it is safe to export (design D4).

    Returns the symlink-resolved path, which is the one that is exported.
    """
    raw = Path(path).expanduser()
    if not raw.is_absolute():
        raise ProfileError(f"Workspace root must be an absolute path: {path}")
    resolved = raw.resolve()
    if not resolved.is_dir():
        raise ProfileError(f"Workspace root is not an existing directory: {path}")
    if ":" in str(resolved) or "," in str(resolved):
        raise ProfileError(
            f"Workspace root {resolved} contains ':' or ',', which `colima start "
            "--mount` cannot express"
        )
    home = (home or Path.home()).resolve()
    if _contains(resolved, home):
        raise ProfileError(
            f"Workspace root {resolved} is the home directory or contains it; "
            "exporting the home directory is exactly what a dedicated profile "
            "avoids. Name a directory that holds only your projects."
        )
    system = {Path(p) for p in SYSTEM_DIRS} | {Path(p).resolve() for p in SYSTEM_DIRS}
    if resolved in system:
        raise ProfileError(
            f"Workspace root {resolved} is a system directory and cannot be exported"
        )
    for relative in CREDENTIAL_DIRS:
        credential = (home / relative).resolve()
        if _contains(resolved, credential) or _contains(credential, resolved):
            raise ProfileError(
                f"Workspace root {resolved} is or contains the credential location "
                f"{credential}, which must not be visible to the VM"
            )
    state = (state or state_root()).resolve()
    if _contains(state, resolved) or _contains(resolved, state):
        raise ProfileError(
            f"Workspace root {resolved} overlaps the sandbox state directory "
            f"{state}, which holds session records and credentials staging"
        )
    return resolved


# --- The start command, as data -------------------------------------------


def init_mounts(roots: Iterable[Path], state_dirs: Iterable[Path]) -> list[Export]:
    """Every export a fresh profile gets: roots, then state subtrees, all writable."""
    unique = dict.fromkeys(str(p) for p in (*roots, *state_dirs))
    return [Export(path, True) for path in unique]


def _mount_args(mounts: Sequence[Export]) -> list[str]:
    args: list[str] = []
    for mount in mounts:
        args += ["--mount", f"{mount.path}{':w' if mount.writable else ''}"]
    return args


# The two flags that protect the user's own Docker context and ~/.ssh/config.
# They apply per start, so every start the project runs passes them.
PROTECTIVE_FLAGS = ("--activate=false", "--ssh-config=false")


def build_start_argv(profile: str, mounts: Sequence[Export], size: Size) -> list[str]:
    """``colima start`` for a new profile."""
    argv = [
        "colima",
        "start",
        "--profile",
        profile,
        "--runtime",
        "docker",
        "--vm-type",
        "vz",
        "--mount-type",
        "virtiofs",
        "--cpu",
        str(size.cpu),
        "--memory",
        f"{size.memory:g}",
    ]
    if size.disk is not None:
        argv += ["--disk", str(size.disk)]
    return [*argv, *_mount_args(mounts), *PROTECTIVE_FLAGS]


def build_restart_argv(profile: str, mounts: Sequence[Export]) -> list[str]:
    """``colima start`` for an existing, stopped profile (saved size is kept)."""
    return [
        "colima",
        "start",
        "--profile",
        profile,
        *_mount_args(mounts),
        *PROTECTIVE_FLAGS,
    ]


def check_start_flags(help_text: str) -> None:
    """Fail naming the first required flag that ``colima start --help`` lacks."""
    for flag in REQUIRED_START_FLAGS:
        if not any(
            token.rstrip(",") == flag
            for line in help_text.splitlines()
            for token in line.split()
        ):
            raise ProfileError(
                f"This colima does not offer `colima start {flag}`; refusing to "
                "start a profile without it. Update colima."
            )


# --- State directories -----------------------------------------------------


def check_state_dirs(state_dirs: Iterable[Path]) -> None:
    """Refuse a state tree behind a symlink (design D3)."""
    for directory in state_dirs:
        resolved = directory.resolve()
        if resolved != directory:
            raise ProfileError(
                f"State directory {directory} is behind a symlink (it resolves to "
                f"{resolved}), so the export and the sandbox's mount source would "
                "not match. Set XDG_STATE_HOME to the resolved directory "
                f"{resolved.parent.parent} and retry."
            )


def create_state_dirs(state_dirs: Iterable[Path]) -> None:
    """Mount sources must exist; the sandbox creates them lazily otherwise."""
    for directory in state_dirs:
        mount_storage.ensure_private_dirs(directory)


# --- The roots record (design D6) -----------------------------------------


def record_path(profile: str) -> Path:
    return state_root() / RECORD_DIRNAME / f"{profile}.json"


def read_record(profile: str) -> list[str] | None:
    """The workspace roots recorded for ``profile``; ``None`` without a record."""
    try:
        data = json.loads(record_path(profile).read_text())
        roots = data["roots"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(roots, list) or not all(isinstance(r, str) for r in roots):
        return None
    return roots


def write_record(profile: str, roots: Iterable[str | Path]) -> Path:
    """Write the record, owner-only. Paths only; never credentials."""
    path = record_path(profile)
    mount_storage.ensure_private_dirs(path.parent)
    payload = json.dumps(
        {"profile": profile, "roots": list(dict.fromkeys(str(r) for r in roots))},
        indent=2,
    )
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(payload + "\n")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


# --- Reading the user's Colima ---------------------------------------------


def profile_names(run: Runner = subprocess.run) -> list[str]:
    """Names of every Colima profile in any state (``colima list --json``)."""
    try:
        proc = run(
            ["colima", "list", "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProfileError(f"Could not list Colima profiles: {exc}")
    if proc.returncode != 0:
        raise ProfileError(
            "Could not list Colima profiles: "
            f"{(proc.stderr or proc.stdout).strip() or 'colima list failed'}"
        )
    names: list[str] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        for entry in item if isinstance(item, list) else [item]:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                names.append(entry["name"])
    return names


def start_help(run: Runner = subprocess.run) -> str:
    try:
        proc = run(
            ["colima", "start", "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProfileError(f"Could not read `colima start --help`: {exc}")
    return proc.stdout + proc.stderr


def running_sandboxes(runtime: container_cli.Runtime, run: Runner) -> list[str]:
    """Running sandbox containers in the profile, without the egress sidecar.

    The sidecar is not counted: the next launch recreates it after a restart.
    """
    try:
        proc = run(
            runtime.argv(
                "ps", "--filter", "name=project-sandbox-", "--format", "{{.Names}}"
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProfileError(f"Could not list running containers: {exc}")
    if proc.returncode != 0:
        raise ProfileError(
            "Could not list running containers in Colima profile "
            f"{runtime.profile!r}: {(proc.stderr or proc.stdout).strip()}. "
            "Pass --force to restart without checking."
        )
    return [
        line.strip()
        for line in proc.stdout.splitlines()
        if line.strip() and line.strip() != egress_sidecar.CONTAINER
    ]


# --- Doctor helpers (pure) ---------------------------------------------------


def home_exports(exports: Sequence[Export], home: Path | None = None) -> list[Export]:
    """Read-write exports that are the home directory or an ancestor of it."""
    home = (home or Path.home()).resolve()
    return [e for e in exports if e.writable and _contains(Path(e.path), home)]


def export_drift(
    roots: Sequence[str],
    exports: Sequence[Export],
    state_dirs: Iterable[Path],
) -> tuple[list[str], list[str]]:
    """``(unexpected, missing)`` exports against roots plus state subtrees."""
    expected = {*roots, *map(str, state_dirs)}
    actual = {e.path for e in exports}
    return sorted(actual - expected), sorted(expected - actual)


# --- Commands ---------------------------------------------------------------


def _confirm(
    commands: Sequence[Sequence[str]],
    *,
    yes: bool,
    is_tty: Callable[[], bool],
    ask: Callable[[str], str],
    extra: str = "",
) -> None:
    print("Command:" + "".join("\n  " + shlex.join(c) for c in commands))
    if extra:
        print(extra)
    if yes:
        return
    if not is_tty():
        raise ProfileError("Not a terminal: pass --yes to confirm without a prompt.")
    if ask("Run this? [y/N] ").strip().lower() not in {"y", "yes"}:
        raise ProfileError("Cancelled; nothing was changed.")


def _run_attached(run: Runner, argv: Sequence[str], what: str) -> None:
    try:
        proc = run(list(argv), check=False)
    except OSError as exc:
        raise ProfileError(f"Could not run `{shlex.join(argv[:2])}`: {exc}")
    if proc.returncode != 0:
        raise ProfileError(
            f"`{shlex.join(argv[:2])}` failed ({what}); exit status {proc.returncode}"
        )


def init(
    args: argparse.Namespace,
    *,
    run: Runner = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
    is_tty: Callable[[], bool] = lambda: sys.stdin.isatty(),
    ask: Callable[[str], str] = input,
    state_dirs: Sequence[Path] | None = None,
) -> int:
    profile = args.colima_profile
    roots = list(dict.fromkeys(validate_root(r) for r in args.workspace_root))
    dirs = list(
        state_dirs if state_dirs is not None else colima_mounts.required_state_dirs()
    )
    check_state_dirs(dirs)
    size = Size(args.cpu, args.memory, args.disk)
    argv = build_start_argv(profile, init_mounts(roots, dirs), size)
    if args.dry_run:
        print(
            "Dry run: nothing was created or started.\nCommand:\n  " + shlex.join(argv)
        )
        return 0
    if which("colima") is None:
        raise ProfileError("colima is not on PATH")
    if profile in profile_names(run):
        raise ProfileError(
            f"Colima profile {profile!r} already exists. `colima init` never "
            "modifies an existing profile; to change its exports use "
            f"`project-sandbox colima add-root PATH --colima-profile {profile}`."
        )
    check_start_flags(start_help(run))
    _confirm([argv], yes=args.yes, is_tty=is_tty, ask=ask)
    create_state_dirs(dirs)
    _run_attached(run, argv, "profile not created")
    write_record(profile, roots)
    print(
        f"Created Colima profile {profile!r}. Use it with: "
        f"project-sandbox --runtime colima --colima-profile {profile} ..."
    )
    return 0


def add_root(
    args: argparse.Namespace,
    *,
    run: Runner = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
    is_tty: Callable[[], bool] = lambda: sys.stdin.isatty(),
    ask: Callable[[str], str] = input,
) -> int:
    profile = args.colima_profile
    runtime = container_cli.colima_runtime(profile)
    if which("colima") is None:
        raise ProfileError("colima is not on PATH")
    exports = colima_mounts.exported_mounts(runtime, run=run, which=which)
    if exports is None:
        raise ProfileError(
            f"Could not read the current exports of Colima profile {profile!r} "
            f"(is it running? try `colima start --profile {profile}`); refusing to "
            "restart with the new path alone, which would drop every other export."
        )
    root = validate_root(args.path)
    if any(e.path == str(root) and e.writable for e in exports):
        print(
            f"{root} is already exported by Colima profile {profile!r}; nothing changed."
        )
        return 0
    check_start_flags(start_help(run))
    # A read-only export of the same path is replaced: the sandbox needs to
    # write its workspace, and a second --mount for one path is ambiguous.
    mounts = [*(e for e in exports if e.path != str(root)), Export(str(root), True)]
    start = build_restart_argv(profile, mounts)
    stop = ["colima", "stop", "--profile", profile]
    running = [] if args.force else running_sandboxes(runtime, run)
    if running:
        raise ProfileError(
            f"{len(running)} sandbox container(s) are running in profile "
            f"{profile!r} and a restart ends them: {', '.join(running)}. "
            "Finish them, or pass --force to restart anyway."
        )
    if args.dry_run:
        print(
            "Dry run: nothing was restarted.\nCommands:\n  "
            + shlex.join(stop)
            + "\n  "
            + shlex.join(start)
        )
        return 0
    note = "This stops and restarts the VM; running containers are lost."
    _confirm([stop, start], yes=args.yes, is_tty=is_tty, ask=ask, extra=note)
    _run_attached(run, stop, "profile not stopped")
    _run_attached(run, start, "profile may be stopped")
    # A hand-made profile has no record; do not invent one that would make
    # doctor report every other export as drift.
    record = read_record(profile)
    if record is not None:
        write_record(profile, [*record, str(root)])
    print(f"Colima profile {profile!r} now also exports {root}.")
    return 0


def _profile_name(value: str) -> str:
    if not container_cli._COLIMA_PROFILE_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(f"Invalid Colima profile name: {value!r}")
    return value


def _positive(kind: type[int | float]):
    def parse(value: str):
        try:
            number = kind(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not a number: {value!r}")
        if number <= 0:
            raise argparse.ArgumentTypeError("must be greater than zero")
        return number

    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="project-sandbox colima",
        description="Create and maintain a Colima profile that exports only "
        "the workspace roots and the sandbox's own state directories.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--colima-profile",
            type=_profile_name,
            default=container_cli.COLIMA_DEFAULT_PROFILE,
            help="Colima profile (default: %(default)s)",
        )
        p.add_argument("--yes", action="store_true", help="skip the confirmation")
        p.add_argument(
            "--dry-run", action="store_true", help="print the command and run nothing"
        )

    p_init = sub.add_parser("init", help="create the dedicated profile")
    p_init.add_argument(
        "--workspace-root",
        action="append",
        required=True,
        metavar="PATH",
        help="directory to export read-write (repeatable)",
    )
    p_init.add_argument("--cpu", type=_positive(int), default=DEFAULT_CPU)
    p_init.add_argument(
        "--memory", type=_positive(float), default=DEFAULT_MEMORY_GIB, help="GiB"
    )
    p_init.add_argument("--disk", type=_positive(int), default=None, help="GiB")
    common(p_init)

    p_add = sub.add_parser("add-root", help="export one more workspace root")
    p_add.add_argument("path", metavar="PATH")
    p_add.add_argument(
        "--force",
        action="store_true",
        help="restart even while sandbox containers are running",
    )
    common(p_add)
    return parser


def main(argv: Sequence[str], **kwargs) -> int:
    args = build_parser().parse_args(list(argv))
    if args.command == "init":
        return init(args, **kwargs)
    return add_root(args, **kwargs)
