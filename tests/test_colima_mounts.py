"""Colima mount-visibility preflight and the Colima-safe VCS mask directory."""

import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import colima_mounts, observability, vcs_access
from project_sandbox.container_cli import colima_runtime


class _Result(subprocess.CompletedProcess):
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        super().__init__([], returncode, stdout, stderr)


def _which(found: bool = True):
    return lambda _name: "/opt/local/bin/colima" if found else None


class MountSourceParsingTests(TestCase):
    def test_bind_sources_are_collected_in_order(self) -> None:
        argv = [
            "docker",
            "run",
            "--mount",
            "type=bind,source=/a/one,target=/x",
            "--env",
            "A=1",
            "--mount",
            "type=bind,src=/b/two,target=/y,readonly",
            "--mount",
            "type=volume,source=named,target=/z",
            "--mount",
            "type=tmpfs,target=/t",
        ]
        self.assertEqual(
            colima_mounts.mount_sources(argv), [Path("/a/one"), Path("/b/two")]
        )

    def test_trailing_mount_flag_without_value_is_ignored(self) -> None:
        self.assertEqual(colima_mounts.mount_sources(["docker", "run", "--mount"]), [])


class ProbePathTests(TestCase):
    def test_existing_source_is_probed_itself(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(colima_mounts.probe_path(Path(tmp)), Path(tmp))

    def test_staging_not_yet_created_probes_its_nearest_existing_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            future = Path(tmp) / "session-inputs" / "abc" / "claude"
            self.assertEqual(colima_mounts.probe_path(future), Path(tmp))

    def test_filesystem_root_is_never_probed(self) -> None:
        self.assertIsNone(colima_mounts.probe_path(Path("/")))


class MissingInVmTests(TestCase):
    runtime = colima_runtime("work")

    def test_one_read_only_ssh_call_covers_all_paths(self) -> None:
        calls: list[list[str]] = []

        def run(argv, **_kwargs):
            calls.append(list(argv))
            return _Result(0, "")

        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            missing = colima_mounts.missing_in_vm(
                self.runtime,
                [Path(a), Path(a) / "child", Path(b)],
                run=run,
                which=_which(),
            )
        self.assertEqual(missing, [])
        self.assertEqual(len(calls), 1)
        argv = calls[0]
        self.assertEqual(argv[:5], ["colima", "ssh", "--profile", "work", "--"])
        # The script and each path are separate argv elements, never one
        # pre-quoted string, and duplicates collapse to one probe.
        self.assertEqual(argv[5:7], ["sh", "-c"])
        self.assertEqual(argv[8], "sh")
        self.assertEqual(argv[9:], [a, b])

    def test_vm_reported_paths_are_returned(self) -> None:
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            missing = colima_mounts.missing_in_vm(
                self.runtime,
                [Path(a), Path(b)],
                run=lambda *_a, **_k: _Result(0, b + "\n"),
                which=_which(),
            )
        self.assertEqual(missing, [Path(b)])

    def test_nothing_to_probe_makes_no_call(self) -> None:
        def run(*_args, **_kwargs):
            raise AssertionError("colima must not be called")

        self.assertEqual(
            colima_mounts.missing_in_vm(self.runtime, [], run=run, which=_which()), []
        )

    def test_missing_colima_binary_means_not_checked(self) -> None:
        with tempfile.TemporaryDirectory() as a:
            self.assertIsNone(
                colima_mounts.missing_in_vm(
                    self.runtime, [Path(a)], which=_which(False)
                )
            )

    def test_ssh_failure_is_reported_not_treated_as_visible(self) -> None:
        with tempfile.TemporaryDirectory() as a, self.assertRaises(SystemExit) as ctx:
            colima_mounts.missing_in_vm(
                self.runtime,
                [Path(a)],
                run=lambda *_a, **_k: _Result(1, "", "instance not running"),
                which=_which(),
            )
        self.assertIn("instance not running", str(ctx.exception))


class CheckTests(TestCase):
    runtime = colima_runtime("work")

    def test_unexported_paths_fail_naming_path_and_mount(self) -> None:
        with tempfile.TemporaryDirectory() as a, self.assertRaises(SystemExit) as ctx:
            colima_mounts.check(
                self.runtime,
                [Path(a)],
                run=lambda *_a, **_k: _Result(0, a + "\n"),
                which=_which(),
            )
        message = str(ctx.exception)
        self.assertIn(a, message)
        self.assertIn("colima stop --profile work", message)
        self.assertIn(f"colima start --profile work --mount {a}:w", message)

    def test_visible_paths_pass_silently(self) -> None:
        warned: list[str] = []
        with tempfile.TemporaryDirectory() as a:
            colima_mounts.check(
                self.runtime,
                [Path(a)],
                run=lambda *_a, **_k: _Result(0, ""),
                which=_which(),
                warn=warned.append,
            )
        self.assertEqual(warned, [])

    def test_missing_colima_warns_and_continues(self) -> None:
        warned: list[str] = []
        with tempfile.TemporaryDirectory() as a:
            colima_mounts.check(
                self.runtime, [Path(a)], which=_which(False), warn=warned.append
            )
        self.assertEqual(len(warned), 1)
        self.assertIn("not checked", warned[0])

    def test_script_requires_a_host_backed_mount(self) -> None:
        # The VM has plain directories named like macOS ones (/private/tmp), so
        # existence alone is not visibility.
        script = colima_mounts._SCRIPT
        self.assertIn("findmnt", script)
        for fstype in ("virtiofs", "9p", "fuse.sshfs"):
            self.assertIn(fstype, script)


class VcsMaskDirTests(TestCase):
    def test_default_is_the_system_temporary_directory(self) -> None:
        path = vcs_access.new_mask_dir()
        try:
            self.assertEqual(path.parent, Path(tempfile.gettempdir()))
            self.assertTrue((path / "directory").is_dir())
            self.assertTrue((path / "file").is_file())
        finally:
            for child in path.iterdir():
                child.rmdir() if child.is_dir() else child.unlink()
            path.rmdir()

    def test_colima_places_it_under_the_given_state_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "vcs-masks"
            path = vcs_access.new_mask_dir(parent)
            self.assertEqual(path.parent, parent)
            self.assertEqual(parent.stat().st_mode & 0o777, 0o700)
            self.assertTrue((path / "directory").is_dir())


FINDMNT = (
    "/Users/me/src rw,relatime\n"
    "/Users/me/.local/state/project-sandbox/mounts rw,relatime\n"
    "/Users/me/ro\\x20data ro,relatime\n"
)


def _runner(findmnt: str = FINDMNT, *, ssh_missing: str = ""):
    """Answers the visibility probe and the findmnt export listing differently."""
    calls: list[list[str]] = []

    def run(argv, **_kwargs):
        calls.append(list(argv))
        if "findmnt" in argv:
            return _Result(0, findmnt)
        return _Result(0, ssh_missing)

    return run, calls


class ExportedMountsTests(TestCase):
    runtime = colima_runtime("work")

    def test_exports_are_parsed_with_their_access_and_escaped_spaces(self) -> None:
        run, calls = _runner()
        exports = colima_mounts.exported_mounts(self.runtime, run=run, which=_which())
        self.assertEqual(
            exports,
            [
                colima_mounts.Export("/Users/me/src", True),
                colima_mounts.Export(
                    "/Users/me/.local/state/project-sandbox/mounts", True
                ),
                colima_mounts.Export("/Users/me/ro data", False),
            ],
        )
        self.assertEqual(
            calls[0][:6], ["colima", "ssh", "--profile", "work", "--", "findmnt"]
        )

    def test_unreadable_exports_are_none_never_an_empty_list(self) -> None:
        # An empty list would read as "the VM exports nothing" and the hint would
        # then drop every mount; unknown must stay unknown.
        self.assertIsNone(
            colima_mounts.exported_mounts(self.runtime, which=_which(False))
        )
        self.assertIsNone(
            colima_mounts.exported_mounts(
                self.runtime,
                run=lambda *_a, **_k: _Result(1, "", "down"),
                which=_which(),
            )
        )

        def boom(*_a, **_k):
            raise OSError("no colima")

        self.assertIsNone(
            colima_mounts.exported_mounts(self.runtime, run=boom, which=_which())
        )


class MountFlagsTests(TestCase):
    E = colima_mounts.Export

    def test_existing_exports_are_kept_and_the_missing_path_is_added(self) -> None:
        flags = colima_mounts.mount_flags(
            [self.E("/a", True), self.E("/ro", False)], [Path("/new")]
        )
        self.assertEqual(flags, "--mount /a:w --mount /ro --mount /new:w")

    def test_read_only_exports_stay_read_only(self) -> None:
        self.assertEqual(
            colima_mounts.mount_flags([self.E("/ro", False)], []), "--mount /ro"
        )

    def test_a_missing_path_that_is_already_exported_appears_once(self) -> None:
        flags = colima_mounts.mount_flags([self.E("/a", False)], [Path("/a")])
        self.assertEqual(flags, "--mount /a:w")

    def test_paths_with_spaces_are_quoted(self) -> None:
        flags = colima_mounts.mount_flags([self.E("/a b", True)], [Path("/c d")])
        self.assertEqual(flags, "--mount '/a b':w --mount '/c d':w")


class SafeHintTests(TestCase):
    runtime = colima_runtime("work")

    def test_the_hint_keeps_every_current_export_and_says_start_replaces(self) -> None:
        with tempfile.TemporaryDirectory() as new:
            run, _ = _runner(ssh_missing=new + "\n")
            with self.assertRaises(SystemExit) as ctx:
                colima_mounts.check(self.runtime, [Path(new)], run=run, which=_which())
        message = str(ctx.exception)
        self.assertIn("REPLACES", message)
        self.assertIn("currently exports: /Users/me/src (read-write)", message)
        command = next(
            l for l in message.splitlines() if l.startswith("  colima start --profile")
        )
        for kept in ("--mount /Users/me/src:w", "--mount '/Users/me/ro data'"):
            self.assertIn(kept, command)
        self.assertIn(f"--mount {new}:w", command)
        self.assertNotIn("only your home directory", message)

    def test_unknown_exports_tell_the_user_to_list_them_not_to_guess(self) -> None:
        with tempfile.TemporaryDirectory() as new:

            def run(argv, **_kwargs):
                if "findmnt" in argv:
                    return _Result(1, "", "unreachable")
                return _Result(0, new + "\n")

            with self.assertRaises(SystemExit) as ctx:
                colima_mounts.check(self.runtime, [Path(new)], run=run, which=_which())
        message = str(ctx.exception)
        self.assertIn("REPLACES", message)
        self.assertIn("could not be read", message)
        self.assertIn("<current exports>", message)

    def test_exports_are_only_listed_when_something_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as new:
            run, calls = _runner(ssh_missing="")
            colima_mounts.check(self.runtime, [Path(new)], run=run, which=_which())
        self.assertEqual(len(calls), 1)
        self.assertNotIn("findmnt", calls[0])


class RequiredStateDirsTests(TestCase):
    def test_exactly_the_bind_mounted_subtrees_are_required(self) -> None:
        import os
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp}):
                dirs = colima_mounts.required_state_dirs()
        root = Path(tmp) / "project-sandbox"
        self.assertEqual(
            sorted(d.name for d in dirs),
            ["egress", "mounts", "session-inputs", "vcs-masks"],
        )
        # conftest patches mount_storage.storage_root() to a pytest directory, and
        # session-inputs is derived from it; the other two follow XDG_STATE_HOME.
        for d in (d for d in dirs if d.name in ("egress", "vcs-masks")):
            self.assertEqual(d.parent.resolve(), root.resolve())

    def test_host_only_directories_are_never_required(self) -> None:
        import os
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp}):
                names = {d.name for d in colima_mounts.required_state_dirs()}
                records = observability.state_dir().name
        self.assertEqual(records, "sessions")
        for host_only in ("sessions", "locks", "projects", "local-forward"):
            self.assertNotIn(host_only, names)

    def test_the_colima_mask_parent_is_not_inside_the_session_records(self) -> None:
        import os
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp}):
                parent = vcs_access.colima_mask_parent()
                records = observability.state_dir()
        self.assertNotIn(records, parent.parents)
        self.assertEqual(parent.name, "vcs-masks")
