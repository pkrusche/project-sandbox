"""``project-sandbox colima init`` / ``add-root``: the pure core and the commands.

Nothing here needs Colima or Docker: the runner is a fake that records argv.
"""

import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import cli, colima_mounts, colima_profile
from project_sandbox.colima_mounts import Export
from project_sandbox.colima_profile import ProfileError, Size

HELP = """\
      --activate        set as active context (default true)
  -V, --mount strings  directories to mount
      --mount-type string  volume driver
      --ssh-config      generate SSH config (default true)
  -t, --vm-type string  virtual machine type
"""


def _result(rc: int = 0, out: str = "", err: str = ""):
    return subprocess.CompletedProcess([], rc, out, err)


class FakeColima:
    """Records every argv; answers the read-only probes from its fields."""

    def __init__(
        self,
        *,
        profiles: str = "",
        help_text: str = HELP,
        findmnt: str | None = "",
        containers: str = "",
        start_rc: int = 0,
    ) -> None:
        self.profiles = profiles
        self.help_text = help_text
        self.findmnt = findmnt
        self.containers = containers
        self.start_rc = start_rc
        self.calls: list[list[str]] = []

    def __call__(self, argv, **_kwargs):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:3] == ["colima", "list", "--json"]:
            return _result(out=self.profiles)
        if argv[:2] == ["colima", "start"] and "--help" in argv:
            return _result(out=self.help_text)
        if argv[:2] == ["colima", "ssh"]:
            if self.findmnt is None:
                return _result(1, err="VM not running")
            return _result(out=self.findmnt)
        if argv[:1] == ["docker"] and "ps" in argv:
            return _result(out=self.containers)
        if argv[:2] == ["colima", "start"]:
            return _result(self.start_rc)
        return _result()

    @property
    def mutating(self) -> list[list[str]]:
        return [
            c
            for c in self.calls
            if c[:2] in (["colima", "start"], ["colima", "stop"]) and "--help" not in c
        ]


class _Env(TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()
        self.state_home = self.base / "state"
        self.state_home.mkdir()
        self.work = self.base / "work"
        self.work.mkdir()
        patcher = patch.dict(
            os.environ, {"HOME": str(self.home), "XDG_STATE_HOME": str(self.state_home)}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def state_dirs(self) -> list[Path]:
        return colima_mounts.required_state_dirs()


class ValidateRootTests(_Env):
    def refused(self, path: Path | str, *fragments: str) -> None:
        with self.assertRaises(ProfileError) as ctx:
            colima_profile.validate_root(path)
        message = str(ctx.exception)
        for fragment in fragments:
            self.assertIn(fragment, message)

    def test_a_plain_directory_is_accepted_resolved(self) -> None:
        self.assertEqual(colima_profile.validate_root(self.work), self.work)

    def test_a_symlink_is_exported_as_its_target(self) -> None:
        target = self.base / "Volumes-work-src"
        target.mkdir()
        link = self.base / "link"
        link.symlink_to(target)
        self.assertEqual(colima_profile.validate_root(link), target)

    def test_refusals(self) -> None:
        (self.home / ".ssh").mkdir()
        (self.home / ".config/gcloud").mkdir(parents=True)
        (self.home / ".ssh/inside").mkdir()
        file = self.base / "file"
        file.write_text("x")
        state_tree = self.state_home / "project-sandbox/egress"
        state_tree.mkdir(parents=True)
        table = [
            ("relative", "work", "absolute"),
            ("missing", self.base / "nope", "not an existing directory"),
            ("file", file, "not an existing directory"),
            ("home", self.home, "home directory"),
            ("home ancestor", self.base, "home directory"),
            ("filesystem root", "/", "home directory"),
            ("system dir", "/etc", "system directory"),
            ("ssh", self.home / ".ssh", "credential location"),
            ("inside ssh", self.home / ".ssh/inside", "credential location"),
            ("contains gcloud", self.home / ".config", "credential location"),
            ("state root", self.state_home / "project-sandbox", "state directory"),
            ("inside state", state_tree, "state directory"),
            ("contains state", self.state_home, "state directory"),
        ]
        for label, path, fragment in table:
            with self.subTest(label):
                self.refused(path, fragment)

    def test_a_path_mount_cannot_express_is_refused(self) -> None:
        odd = self.base / "a:b"
        odd.mkdir()
        self.refused(odd, "cannot express")


class StartArgvTests(_Env):
    def argv(self, roots, size: Size | None = None, profile: str = "project-sandbox"):
        dirs = self.state_dirs()
        return colima_profile.build_start_argv(
            profile, colima_profile.init_mounts(roots, dirs), size or Size()
        )

    def mounts(self, argv: list[str]) -> list[str]:
        return [argv[i + 1] for i, t in enumerate(argv) if t == "--mount"]

    def test_exports_are_exactly_the_roots_and_state_dirs(self) -> None:
        argv = self.argv([self.work])
        expected = [f"{p}:w" for p in (self.work, *self.state_dirs())]
        self.assertEqual(self.mounts(argv), expected)
        self.assertNotIn(str(self.home), " ".join(argv).replace(str(self.base), ""))
        self.assertFalse(any(m.startswith(f"{self.home}:") for m in self.mounts(argv)))

    def test_protective_flags_and_engine_are_always_present(self) -> None:
        argv = self.argv([self.work], Size(cpu=2, memory=4, disk=50))
        for flag in ("--activate=false", "--ssh-config=false"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[:4], ["colima", "start", "--profile", "project-sandbox"])
        for pair in (
            ("--runtime", "docker"),
            ("--vm-type", "vz"),
            ("--mount-type", "virtiofs"),
        ):
            self.assertEqual(argv[argv.index(pair[0]) + 1], pair[1])

    def test_default_and_overridden_size(self) -> None:
        argv = self.argv([self.work])
        self.assertEqual(argv[argv.index("--cpu") + 1], "4")
        self.assertEqual(argv[argv.index("--memory") + 1], "8")
        self.assertNotIn("--disk", argv)
        argv = self.argv([self.work], Size(2, 4, 40))
        self.assertEqual(argv[argv.index("--cpu") + 1], "2")
        self.assertEqual(argv[argv.index("--memory") + 1], "4")
        self.assertEqual(argv[argv.index("--disk") + 1], "40")

    def test_paths_with_spaces_stay_one_argument_and_quote_for_display(self) -> None:
        spaced = self.base / "my work"
        spaced.mkdir()
        argv = self.argv([spaced])
        self.assertIn(f"{spaced}:w", argv)
        import shlex

        self.assertEqual(shlex.split(shlex.join(argv)), argv)

    def test_restart_argv_keeps_modes_and_protective_flags(self) -> None:
        argv = colima_profile.build_restart_argv(
            "p", [Export("/a", True), Export("/b", False)]
        )
        self.assertEqual(self.mounts(argv), ["/a:w", "/b"])
        self.assertIn("--activate=false", argv)
        self.assertIn("--ssh-config=false", argv)
        self.assertNotIn("--cpu", argv)


class FlagCheckTests(TestCase):
    def test_complete_help_passes(self) -> None:
        colima_profile.check_start_flags(HELP)

    def test_each_missing_flag_is_named(self) -> None:
        for flag in colima_profile.REQUIRED_START_FLAGS:
            with self.subTest(flag):
                text = "\n".join(
                    line for line in HELP.splitlines() if f"{flag} " not in line + " "
                )
                with self.assertRaises(ProfileError) as ctx:
                    colima_profile.check_start_flags(text)
                self.assertIn(flag, str(ctx.exception))

    def test_a_longer_flag_does_not_satisfy_a_shorter_one(self) -> None:
        text = HELP.replace("--mount strings", "--mounts strings")
        with self.assertRaises(ProfileError) as ctx:
            colima_profile.check_start_flags(text.replace("--mount-type", "--mt"))
        self.assertIn("--mount", str(ctx.exception))


class StateDirTests(_Env):
    def test_a_symlinked_state_home_is_refused_naming_the_resolved_dir(self) -> None:
        link = self.base / "statelink"
        link.symlink_to(self.state_home)
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(link)}):
            with self.assertRaises(ProfileError) as ctx:
                colima_profile.check_state_dirs(self.state_dirs())
        self.assertIn(str(self.state_home), str(ctx.exception))
        self.assertIn("XDG_STATE_HOME", str(ctx.exception))

    def test_plain_state_dirs_pass(self) -> None:
        colima_profile.check_state_dirs(self.state_dirs())

    def test_directories_are_created_owner_only(self) -> None:
        dirs = self.state_dirs()
        self.assertEqual(len(dirs), 4)
        colima_profile.create_state_dirs(dirs)
        for directory in dirs:
            with self.subTest(directory.name):
                self.assertTrue(directory.is_dir())
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)


class RecordTests(_Env):
    def test_round_trip_and_permissions(self) -> None:
        path = colima_profile.write_record("p", [self.work])
        self.assertEqual(colima_profile.read_record("p"), [str(self.work)])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(list(path.parent.glob(".*.tmp")), [])

    def test_missing_or_corrupt_record_is_none(self) -> None:
        self.assertIsNone(colima_profile.read_record("none"))
        path = colima_profile.write_record("bad", [self.work])
        path.write_text("{not json")
        self.assertIsNone(colima_profile.read_record("bad"))

    def test_rewrite_replaces_and_dedupes(self) -> None:
        colima_profile.write_record("p", [self.work])
        colima_profile.write_record("p", [self.work, "/x", "/x"])
        self.assertEqual(colima_profile.read_record("p"), [str(self.work), "/x"])

    def test_the_record_is_never_under_an_exported_path(self) -> None:
        path = colima_profile.record_path("p")
        for directory in self.state_dirs():
            self.assertNotIn(directory, [path, *path.parents])
        self.assertNotIn(path.parent, self.state_dirs())


class _CommandEnv(_Env):
    def run_main(self, argv, fake: FakeColima, *, tty: bool = True, answer: str = "y"):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = colima_profile.main(
                argv,
                run=fake,
                which=lambda _n: "/opt/local/bin/colima",
                is_tty=lambda: tty,
                ask=lambda _p: answer,
            )
        return code, out.getvalue()

    def init_argv(self, *extra: str) -> list[str]:
        return ["init", "--workspace-root", str(self.work), *extra]


class ParserTests(_CommandEnv):
    def test_help_lists_the_options(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            colima_profile.main(["init", "--help"])
        for option in (
            "--workspace-root",
            "--colima-profile",
            "--cpu",
            "--memory",
            "--disk",
            "--yes",
            "--dry-run",
        ):
            self.assertIn(option, out.getvalue())

    def test_missing_workspace_root_exits_before_anything_runs(self) -> None:
        fake = FakeColima()
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            colima_profile.main(["init"], run=fake)
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertIn("--workspace-root", err.getvalue())
        self.assertEqual(fake.calls, [])

    def test_cli_dispatches_colima_and_leaves_the_positional_cli_alone(self) -> None:
        with patch.object(colima_profile, "main", return_value=7) as main:
            self.assertEqual(cli._main(["colima", "init", "--yes"]), 7)
        main.assert_called_once_with(["init", "--yes"])
        args = cli.build_parser().parse_args(["--runtime", "docker", "proj"])
        self.assertEqual((args.runtime, args.project), ("docker", "proj"))

    def test_invalid_profile_name_is_rejected(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            colima_profile.main(self.init_argv("--colima-profile", "../x"))
        self.assertIn("Invalid Colima profile name", err.getvalue())


class InitTests(_CommandEnv):
    def test_success_runs_start_with_the_exports_and_writes_the_record(self) -> None:
        fake = FakeColima()
        code, out = self.run_main(self.init_argv(), fake)
        self.assertEqual(code, 0)
        (start,) = fake.mutating
        self.assertEqual(start[:2], ["colima", "start"])
        self.assertIn(f"{self.work}:w", start)
        self.assertIn("--activate=false", start)
        self.assertEqual(
            colima_profile.read_record("project-sandbox"), [str(self.work)]
        )
        for directory in self.state_dirs():
            self.assertTrue(directory.is_dir())
        self.assertIn("colima start", out)

    def test_an_existing_profile_is_refused_in_any_state(self) -> None:
        for status in ("Running", "Stopped", "Broken"):
            with self.subTest(status):
                fake = FakeColima(
                    profiles=json.dumps({"name": "project-sandbox", "status": status})
                )
                with self.assertRaises(ProfileError) as ctx:
                    self.run_main(self.init_argv(), fake)
                self.assertIn("add-root", str(ctx.exception))
                self.assertEqual(fake.mutating, [])
                self.assertFalse(colima_profile.record_path("project-sandbox").exists())

    def test_other_profiles_do_not_block_init(self) -> None:
        fake = FakeColima(profiles=json.dumps({"name": "default", "status": "Broken"}))
        self.assertEqual(self.run_main(self.init_argv(), fake)[0], 0)

    def test_dry_run_runs_nothing_and_creates_nothing(self) -> None:
        fake = FakeColima()
        code, out = self.run_main(self.init_argv("--dry-run"), fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.calls, [])
        self.assertFalse((self.state_home / "project-sandbox").exists())
        self.assertIn("--activate=false", out)

    def test_declined_confirmation_runs_nothing_and_fails(self) -> None:
        fake = FakeColima()
        with self.assertRaises(ProfileError) as ctx:
            self.run_main(self.init_argv(), fake, answer="n")
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertEqual(fake.mutating, [])
        self.assertFalse((self.state_home / "project-sandbox").exists())

    def test_no_tty_without_yes_is_an_error_and_yes_skips_the_prompt(self) -> None:
        fake = FakeColima()
        with self.assertRaises(ProfileError) as ctx:
            self.run_main(self.init_argv(), fake, tty=False)
        self.assertIn("--yes", str(ctx.exception))
        self.assertEqual(fake.mutating, [])
        self.assertEqual(self.run_main(self.init_argv("--yes"), fake, tty=False)[0], 0)

    def test_a_failed_start_writes_no_record(self) -> None:
        fake = FakeColima(start_rc=1)
        with self.assertRaises(ProfileError):
            self.run_main(self.init_argv(), fake)
        self.assertIsNone(colima_profile.read_record("project-sandbox"))

    def test_a_missing_flag_stops_before_starting(self) -> None:
        fake = FakeColima(help_text=HELP.replace("--activate", "--act"))
        with self.assertRaises(ProfileError) as ctx:
            self.run_main(self.init_argv(), fake)
        self.assertIn("--activate", str(ctx.exception))
        self.assertEqual(fake.mutating, [])

    def test_a_bad_root_exits_before_anything_runs(self) -> None:
        fake = FakeColima()
        with self.assertRaises(ProfileError):
            self.run_main(["init", "--workspace-root", str(self.home)], fake)
        self.assertEqual(fake.calls, [])

    def test_a_symlinked_state_home_exits_before_anything_runs(self) -> None:
        link = self.base / "statelink"
        link.symlink_to(self.state_home)
        fake = FakeColima()
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(link)}):
            with self.assertRaises(ProfileError):
                self.run_main(self.init_argv(), fake)
        self.assertEqual(fake.calls, [])

    def test_size_options_reach_the_command(self) -> None:
        fake = FakeColima()
        self.run_main(
            self.init_argv("--cpu", "2", "--memory", "4", "--disk", "40", "--yes"), fake
        )
        (start,) = fake.mutating
        self.assertEqual(start[start.index("--cpu") + 1], "2")
        self.assertEqual(start[start.index("--memory") + 1], "4")
        self.assertEqual(start[start.index("--disk") + 1], "40")


def _findmnt(*pairs: tuple[str, str]) -> str:
    return "".join(f"{path} {mode},relatime\n" for path, mode in pairs)


class AddRootTests(_CommandEnv):
    def setUp(self) -> None:
        super().setUp()
        self.new = self.base / "new"
        self.new.mkdir()
        self.exports = _findmnt(
            (str(self.work), "rw"), ("/ro", "ro"), ("/with\\x20space", "rw")
        )

    def add(self, fake: FakeColima, *extra: str, **kwargs):
        return self.run_main(["add-root", str(self.new), *extra], fake, **kwargs)

    def test_every_current_export_and_mode_is_kept_and_the_new_root_is_never_alone(
        self,
    ) -> None:
        colima_profile.write_record("project-sandbox", [self.work])
        fake = FakeColima(findmnt=self.exports)
        code, _ = self.add(fake, "--yes")
        self.assertEqual(code, 0)
        stop, start = fake.mutating
        self.assertEqual(stop, ["colima", "stop", "--profile", "project-sandbox"])
        mounts = [start[i + 1] for i, t in enumerate(start) if t == "--mount"]
        self.assertEqual(
            mounts, [f"{self.work}:w", "/ro", "/with space:w", f"{self.new}:w"]
        )
        self.assertIn("--activate=false", start)
        self.assertIn("--ssh-config=false", start)
        self.assertEqual(
            colima_profile.read_record("project-sandbox"),
            [str(self.work), str(self.new)],
        )

    def test_unreadable_exports_stop_before_restarting(self) -> None:
        fake = FakeColima(findmnt=None)
        with self.assertRaises(ProfileError) as ctx:
            self.add(fake, "--yes")
        self.assertIn("exports", str(ctx.exception))
        self.assertEqual(fake.mutating, [])

    def test_already_exported_changes_nothing(self) -> None:
        fake = FakeColima(findmnt=_findmnt((str(self.new), "rw")))
        code, out = self.add(fake, "--yes")
        self.assertEqual(code, 0)
        self.assertIn("already exported", out)
        self.assertEqual(fake.mutating, [])

    def test_an_invalid_root_is_refused_before_restarting(self) -> None:
        fake = FakeColima(findmnt=self.exports)
        with self.assertRaises(ProfileError):
            self.run_main(["add-root", str(self.home), "--yes"], fake)
        self.assertEqual(fake.mutating, [])

    def test_running_sandbox_is_refused_naming_it_and_force(self) -> None:
        fake = FakeColima(findmnt=self.exports, containers="project-sandbox-abc\n")
        with self.assertRaises(ProfileError) as ctx:
            self.add(fake, "--yes")
        message = str(ctx.exception)
        self.assertIn("project-sandbox-abc", message)
        self.assertIn("--force", message)
        self.assertEqual(fake.mutating, [])

    def test_the_sidecar_alone_is_not_counted(self) -> None:
        fake = FakeColima(findmnt=self.exports, containers="project-sandbox-egress\n")
        self.assertEqual(self.add(fake, "--yes")[0], 0)
        self.assertEqual(len(fake.mutating), 2)

    def test_force_restarts_without_asking_docker(self) -> None:
        fake = FakeColima(findmnt=self.exports, containers="project-sandbox-abc\n")
        self.assertEqual(self.add(fake, "--yes", "--force")[0], 0)
        self.assertEqual(len(fake.mutating), 2)
        self.assertFalse(any("ps" in c for c in fake.calls if c[0] == "docker"))

    def test_a_profile_without_a_record_does_not_get_one(self) -> None:
        fake = FakeColima(findmnt=self.exports)
        self.add(fake, "--yes")
        self.assertIsNone(colima_profile.read_record("project-sandbox"))

    def test_the_ps_probe_targets_the_profile_context(self) -> None:
        fake = FakeColima(findmnt=self.exports)
        self.add(fake, "--yes")
        (ps,) = [c for c in fake.calls if c[0] == "docker"]
        self.assertEqual(ps[:3], ["docker", "--context", "colima-project-sandbox"])
        self.assertIn("name=project-sandbox-", ps)

    def test_dry_run_restarts_nothing(self) -> None:
        fake = FakeColima(findmnt=self.exports)
        code, out = self.add(fake, "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(fake.mutating, [])
        self.assertIn("colima stop", out)


class DoctorHelperTests(_Env):
    def test_home_exports_only_count_when_writable_and_covering_home(self) -> None:
        home = str(self.home)
        parent = str(self.home.parent)
        exports = [
            Export(home, True),
            Export(parent, True),
            Export(home, False),
            Export(str(self.work), True),
        ]
        found = colima_profile.home_exports(exports)
        self.assertEqual([e.path for e in found], [home, parent])

    def test_drift(self) -> None:
        dirs = self.state_dirs()
        roots = [str(self.work)]
        exact = [Export(p, True) for p in (*roots, *map(str, dirs))]
        self.assertEqual(colima_profile.export_drift(roots, exact, dirs), ([], []))
        extra = [*exact, Export("/Users/me/Documents", True)]
        self.assertEqual(
            colima_profile.export_drift(roots, extra, dirs),
            (["/Users/me/Documents"], []),
        )
        short = exact[1:]
        self.assertEqual(
            colima_profile.export_drift(roots, short, dirs), ([], [str(self.work)])
        )


class LaunchMessageTests(_Env):
    def message(self) -> str:
        from project_sandbox.container_cli import colima_runtime

        runtime = colima_runtime("project-sandbox")
        return colima_mounts.not_visible_message(
            runtime, [Path("/x/y")], [Export("/a", True)]
        )

    def test_a_managed_profile_is_told_to_use_add_root(self) -> None:
        colima_profile.write_record("project-sandbox", [self.work])
        message = self.message()
        self.assertIn("project-sandbox colima add-root /x/y", message)
        self.assertNotIn("colima start --profile", message)

    def test_a_hand_made_profile_keeps_the_full_list_advice(self) -> None:
        message = self.message()
        self.assertIn("REPLACES", message)
        self.assertIn("--mount /a:w", message)


class DocsTests(TestCase):
    def test_no_document_suggests_a_bare_colima_start_mount(self) -> None:
        docs = Path(__file__).resolve().parents[1] / "docs"
        for name in ("runtimes.md", "doctor.md"):
            text = (docs / name).read_text()
            for line in text.splitlines():
                with self.subTest(name=name, line=line):
                    self.assertNotRegex(
                        line, r"colima start (--profile \S+ )?--mount \S+\s*$"
                    )
