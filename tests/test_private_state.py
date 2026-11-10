"""Private host state: PrivateDir, roots, locks, session staging and reaping."""

import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import paths, staging
from project_sandbox.paths import PrivateDir, UnsafePathError


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


class PrivateDirTests(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(os.path.realpath(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_creates_missing_components_private(self) -> None:
        with PrivateDir.open(self.base / "a" / "b", anchor=self.base) as d:
            self.assertEqual(d.path, self.base / "a" / "b")
        self.assertEqual(_mode(self.base / "a"), 0o700)
        self.assertEqual(_mode(self.base / "a" / "b"), 0o700)

    def test_refuses_symlinked_component(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        (self.base / "a").symlink_to(outside)
        with self.assertRaises(UnsafePathError) as ctx:
            PrivateDir.open(self.base / "a" / "b", anchor=self.base)
        self.assertIn(str(self.base / "a"), str(ctx.exception))
        self.assertEqual(list(outside.iterdir()), [])

    def test_symlinked_leaf_is_replaced_and_target_untouched(self) -> None:
        victim = self.base / "victim"
        victim.write_text("keep", encoding="utf-8")
        with PrivateDir.open(self.base / "d", anchor=self.base) as d:
            (d.path / "f").symlink_to(victim)
            d.write("f", "new")
        self.assertEqual(victim.read_text(encoding="utf-8"), "keep")
        self.assertFalse((self.base / "d" / "f").is_symlink())
        self.assertEqual((self.base / "d" / "f").read_text(encoding="utf-8"), "new")

    def test_link_swapped_in_before_rename_does_not_redirect(self) -> None:
        victim = self.base / "victim"
        victim.write_text("keep", encoding="utf-8")

        def swap(dir_fd, tmp, name):
            os.symlink(victim, name, dir_fd=dir_fd)

        with PrivateDir.open(self.base / "d", anchor=self.base) as d:
            with patch.object(paths, "_before_replace", swap):
                d.write("f", "new")
        self.assertEqual(victim.read_text(encoding="utf-8"), "keep")
        self.assertEqual((self.base / "d" / "f").read_text(encoding="utf-8"), "new")

    def test_directory_swapped_for_link_after_open_does_not_redirect(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        with PrivateDir.open(self.base / "d", anchor=self.base) as d:
            os.rename(self.base / "d", self.base / "moved")
            (self.base / "d").symlink_to(outside)
            d.write("f", "new")
        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue((self.base / "moved" / "f").is_file())

    def test_secret_is_0600_before_any_byte_is_written(self) -> None:
        seen: list[int] = []
        real = paths._write_all

        def spy(fd, data):
            seen.append(stat.S_IMODE(os.fstat(fd).st_mode))
            real(fd, data)

        old_umask = os.umask(0)
        try:
            with PrivateDir.open(self.base / "d", anchor=self.base) as d:
                with patch.object(paths, "_write_all", spy):
                    d.write_secret("key", "s3cret")
        finally:
            os.umask(old_umask)
        self.assertEqual(seen, [0o600])
        self.assertEqual(_mode(self.base / "d" / "key"), 0o600)

    def test_group_writable_directory_is_refused(self) -> None:
        (self.base / "d").mkdir()
        os.chmod(self.base / "d", 0o770)
        with self.assertRaises(UnsafePathError) as ctx:
            PrivateDir.open(self.base / "d", anchor=self.base)
        self.assertIn("writable by group or others", str(ctx.exception))

    def test_foreign_owned_directory_is_refused(self) -> None:
        (self.base / "d").mkdir(mode=0o700)
        real = os.fstat

        def foreign(fd):
            st = real(fd)
            return os.stat_result(
                (
                    st.st_mode,
                    st.st_ino,
                    st.st_dev,
                    st.st_nlink,
                    st.st_uid + 1,
                    st.st_gid,
                    st.st_size,
                    st.st_atime,
                    st.st_mtime,
                    st.st_ctime,
                )
            )

        with patch.object(paths, "_fstat", foreign):
            with self.assertRaises(UnsafePathError) as ctx:
                PrivateDir.open(self.base / "d", anchor=self.base)
        self.assertIn("owned by uid", str(ctx.exception))

    def test_non_private_dir_accepts_group_write_but_not_links(self) -> None:
        project = self.base / "project"
        (project / ".devcontainer").mkdir(parents=True)
        os.chmod(project / ".devcontainer", 0o775)
        with paths.open_in_project(project, ".devcontainer") as d:
            d.write(".gitignore", "*\n")
        (project / "other").symlink_to(self.base)
        with self.assertRaises(UnsafePathError):
            paths.open_in_project(project, "other")

    def test_remove_and_unlink_never_follow(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "keep").write_text("x", encoding="utf-8")
        with PrivateDir.open(self.base / "d", anchor=self.base) as d:
            (d.path / "link").symlink_to(outside)
            (d.path / "tree" / "sub").mkdir(parents=True)
            (d.path / "tree" / "sub" / "link").symlink_to(outside)
            self.assertTrue(d.unlink("link"))
            self.assertTrue(d.remove("tree"))
            self.assertEqual(d.listdir(), [])
        self.assertTrue((outside / "keep").is_file())

    def test_read_bytes_skips_links(self) -> None:
        (self.base / "secret").write_text("x", encoding="utf-8")
        with PrivateDir.open(self.base / "d", anchor=self.base) as d:
            (d.path / "link").symlink_to(self.base / "secret")
            d.write("real", "y")
            self.assertIsNone(d.read_bytes("link"))
            self.assertEqual(d.read_bytes("real"), b"y")

    def test_rejects_path_separators_in_names(self) -> None:
        with PrivateDir.open(self.base / "d", anchor=self.base) as d:
            for name in ("../x", "a/b", "..", ""):
                with self.assertRaises(ValueError):
                    d.write(name, "x")


class RootTests(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(os.path.realpath(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _private(self, name: str) -> Path:
        path = self.base / name
        path.mkdir(mode=0o700)
        return path

    def test_override_wins(self) -> None:
        env = {
            "PROJECT_SANDBOX_RUNTIME_DIR": str(self.base / "rt"),
            "XDG_RUNTIME_DIR": str(self._private("xdg")),
            "TMPDIR": str(self._private("tmp")),
        }
        self.assertEqual(paths.runtime_root_path(env), self.base / "rt")

    def test_xdg_runtime_dir_then_tmpdir(self) -> None:
        xdg, tmp = self._private("xdg"), self._private("tmp")
        self.assertEqual(
            paths.runtime_root_path({"XDG_RUNTIME_DIR": str(xdg), "TMPDIR": str(tmp)}),
            xdg / "project-sandbox",
        )
        self.assertEqual(
            paths.runtime_root_path({"TMPDIR": str(tmp)}), tmp / "project-sandbox"
        )

    def test_non_private_tmpdir_falls_back_to_tmp(self) -> None:
        shared = self.base / "shared"
        shared.mkdir()
        os.chmod(shared, 0o1777)
        self.assertEqual(
            paths.runtime_root_path({"TMPDIR": str(shared)}),
            Path("/tmp") / f"project-sandbox-{os.geteuid()}",
        )
        self.assertEqual(
            paths.runtime_root_path({}),
            Path("/tmp") / f"project-sandbox-{os.geteuid()}",
        )

    def test_relative_values(self) -> None:
        with self.assertRaises(UnsafePathError) as ctx:
            paths.runtime_root_path({"PROJECT_SANDBOX_RUNTIME_DIR": "rel/dir"})
        self.assertIn("PROJECT_SANDBOX_RUNTIME_DIR", str(ctx.exception))
        # A relative XDG directory is ignored, per the XDG base directory spec.
        self.assertEqual(
            paths.runtime_root_path({"XDG_RUNTIME_DIR": "rel"}),
            Path("/tmp") / f"project-sandbox-{os.geteuid()}",
        )
        self.assertEqual(
            paths.state_home({"XDG_STATE_HOME": "rel"}),
            Path.home() / ".local" / "state",
        )

    def test_non_private_override_is_refused(self) -> None:
        shared = self.base / "rt"
        shared.mkdir()
        os.chmod(shared, 0o777)
        with self.assertRaises(UnsafePathError) as ctx:
            paths.open_runtime_root({"PROJECT_SANDBOX_RUNTIME_DIR": str(shared)})
        self.assertIn(str(shared), str(ctx.exception))

    def test_override_is_created_private(self) -> None:
        env = {"PROJECT_SANDBOX_RUNTIME_DIR": str(self.base / "rt")}
        with paths.open_runtime_root(env) as root:
            self.assertEqual(root.path, self.base / "rt")
        self.assertEqual(_mode(self.base / "rt"), 0o700)

    def test_symlinked_state_root_is_refused(self) -> None:
        state = self.base / "state"
        state.mkdir()
        (state / "project-sandbox").symlink_to(self._private("elsewhere"))
        with self.assertRaises(UnsafePathError):
            paths.open_state_root({"XDG_STATE_HOME": str(state)})

    def test_project_key(self) -> None:
        project = self.base / "My Project"
        project.mkdir()
        key = paths.project_key(project)
        self.assertRegex(key, r"^My-Project-[0-9a-f]{16}$")
        link = self.base / "link"
        link.symlink_to(project)
        self.assertEqual(paths.project_key(link), key)
        self.assertNotEqual(paths.project_key(self.base), key)

    def test_mount_source_validation(self) -> None:
        root = self._private("root")
        inside = root / "x"
        inside.mkdir()
        paths.validate_mount_source(inside, (root,))
        with self.assertRaises(UnsafePathError):
            paths.validate_mount_source(self.base, (root,))
        (root / "link").symlink_to(inside)
        with self.assertRaises(UnsafePathError):
            paths.validate_mount_source(root / "link", (root,))
        with self.assertRaises(UnsafePathError):
            paths.validate_mount_source(Path("relative"), (root,))


class SessionStagingTests(TestCase):
    def test_layout_and_cleanup_of_own_directory_only(self) -> None:
        one = staging.SessionStaging.create("20260101T000000-aaaaaaaaaaaa")
        two = staging.SessionStaging.create("20260101T000000-bbbbbbbbbbbb")
        try:
            self.assertNotEqual(one.path, two.path)
            self.assertEqual(
                sorted(p.name for p in one.path.iterdir()),
                ["build", "config", "empty", "lease", "prompt", "secrets"],
            )
            for name in staging.SUBDIRS:
                self.assertEqual(_mode(one.path / name), 0o700)
            one.secrets.write_secret("token", "x")
            one.close()
            self.assertFalse(one.path.exists())
            self.assertTrue(two.path.is_dir())
        finally:
            one.close()
            two.close()
        self.assertFalse(two.path.exists())

    def test_staging_path_matches_and_dry_run_creates_nothing(self) -> None:
        sid = "20260101T000000-cccccccccccc"
        expected = staging.staging_path(sid)
        self.assertFalse(expected.parent.parent.exists())
        with staging.SessionStaging.create(sid) as session:
            self.assertEqual(session.path, Path(os.path.realpath(expected)))

    def test_reaper_removes_killed_session_and_keeps_live_one(self) -> None:
        src = str(Path(__file__).resolve().parents[1] / "src")
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import sys, time; sys.path.insert(0, sys.argv[1]);"
                    "from project_sandbox import staging;"
                    "s = staging.SessionStaging.create(sys.argv[2]);"
                    "print(s.path, flush=True); time.sleep(60)"
                ),
                src,
                "20260101T000000-dddddddddddd",
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            killed_path = Path(child.stdout.readline().strip())
            self.assertTrue(killed_path.is_dir())
            with staging.SessionStaging.create("20260101T000000-eeeeeeeeeeee") as live:
                self.assertEqual(staging.reap(), [])  # child still holds its lease
                child.send_signal(signal.SIGKILL)
                child.wait(timeout=10)
                self.assertEqual(staging.reap(), ["20260101T000000-dddddddddddd"])
                self.assertFalse(killed_path.exists())
                self.assertTrue(live.path.is_dir())
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            child.stdout.close()

    def test_reaper_removes_directory_without_lease(self) -> None:
        with staging.SessionStaging.create("20260101T000000-ffffffffffff") as s:
            pass
        with paths.open_runtime_root() as root, root.subdir("staging") as st:
            st.subdir("20260101T000000-000000000000").close()
        self.assertEqual(staging.reap(), ["20260101T000000-000000000000"])
        self.assertFalse(s.path.exists())

    def test_reap_without_runtime_root_creates_nothing(self) -> None:
        root = paths.runtime_root_path()
        self.assertEqual(staging.reap(), [])
        self.assertFalse(root.exists())

    def test_invalid_session_id(self) -> None:
        with self.assertRaises(ValueError):
            staging.SessionStaging.create("../x")
        time.sleep(0)
