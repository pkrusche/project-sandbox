import contextlib
import functools
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import jj_workspace as jj_workspace_mod

JJ = shutil.which("jj")


def _skip_unless_lazy(check, reason: str):
    """Like unittest.skipUnless, but ``check`` runs when the test does, so
    importing this module never spawns a subprocess (collection must not)."""

    def decorator(func):
        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            if not check():
                self.skipTest(reason)
            return func(self, *args, **kwargs)

        return wrapper

    return decorator


def _find_docker() -> str | None:
    for cmd in ("docker", "podman"):
        path = shutil.which(cmd)
        if path is None:
            continue
        try:
            if (
                subprocess.run(
                    [path, "info"], capture_output=True, timeout=5, check=False
                ).returncode
                == 0
            ):
                return path
        except (subprocess.TimeoutExpired, OSError):
            continue
    return None


DOCKER_IMAGE = "ubuntu:22.04"


def _jj(repo: Path, *args: str) -> str:
    # From inside ``repo``, so jj prints paths relative to its root.
    return subprocess.run(
        ["jj", "-R", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        cwd=repo if repo.is_dir() else None,
    ).stdout


def _make_jj_repo(path: Path) -> None:
    """Init a colocated jj repo with one committed change and an empty @ on top."""
    subprocess.run(
        ["jj", "git", "init", "--colocate", str(path)], check=True, capture_output=True
    )
    _jj(path, "config", "set", "--repo", "user.name", "Test")
    _jj(path, "config", "set", "--repo", "user.email", "t@test.com")
    (path / "a.txt").write_text("init\n", encoding="utf-8")
    (path / ".gitignore").write_text("data/\n", encoding="utf-8")
    _jj(path, "describe", "-m", "init")
    _jj(path, "new")


@contextlib.contextmanager
def _as_agent(ws: jj_workspace_mod.JjWorkspace) -> Iterator[Path]:
    """Swap the private metadata into W, as the container's mounts do."""
    parked = ws.private.parent / "host-jj.parked"
    os.rename(ws.path / ".jj", parked)
    os.rmdir(ws.path / ".git")
    os.rename(ws.private_jj, ws.path / ".jj")
    os.rename(ws.private_git, ws.path / ".git")
    try:
        yield ws.path
    finally:
        os.rename(ws.path / ".jj", ws.private_jj)
        os.rename(ws.path / ".git", ws.private_git)
        os.rename(parked, ws.path / ".jj")
        (ws.path / ".git").mkdir()


class PathTests(unittest.TestCase):
    """Unit tests for the session layout — no jj binary required."""

    def _fake_repo(self) -> Path:
        return Path("/tmp/fake/repo")

    def test_slash_and_dash_bookmark_get_different_paths(self) -> None:
        p_slash = jj_workspace_mod.path_for(self._fake_repo(), "feat/x")
        p_dash = jj_workspace_mod.path_for(self._fake_repo(), "feat-x")
        self.assertNotEqual(p_slash, p_dash)

    def test_plain_bookmark_has_no_suffix(self) -> None:
        p = jj_workspace_mod.path_for(self._fake_repo(), "main")
        self.assertEqual(p.name, "main")

    def test_plain_dash_bookmark_has_no_suffix(self) -> None:
        p = jj_workspace_mod.path_for(self._fake_repo(), "my-feature")
        self.assertEqual(p.name, "my-feature")

    def test_slash_bookmark_ends_with_six_char_hex_suffix(self) -> None:
        p = jj_workspace_mod.path_for(self._fake_repo(), "feat/x")
        self.assertRegex(p.name, r"^feat-x-[0-9a-f]{6}$")

    def test_slash_bookmark_suffix_is_deterministic(self) -> None:
        p1 = jj_workspace_mod.path_for(self._fake_repo(), "feat/x")
        p2 = jj_workspace_mod.path_for(self._fake_repo(), "feat/x")
        self.assertEqual(p1, p2)

    def test_default_root_uses_workspaces_suffix(self) -> None:
        repo = Path("/tmp/fake/myrepo")
        p = jj_workspace_mod.path_for(repo, "main")
        self.assertEqual(p.parent.name, "myrepo-workspaces")

    def test_custom_workspace_dir(self) -> None:
        custom = Path("/tmp/custom-ws")
        p = jj_workspace_mod.path_for(self._fake_repo(), "main", workspace_dir=custom)
        self.assertEqual(p.parent, custom)

    def test_private_repository_lies_beside_the_session_directory(self) -> None:
        ws = jj_workspace_mod.plan(self._fake_repo(), "feat/x")
        self.assertEqual(ws.private.parent, ws.path.parent / ".session-vcs")
        self.assertRegex(ws.private.name, rf"^{ws.path.name}\.[0-9a-f]{{12}}$")
        self.assertNotIn(ws.path, ws.private.parents)
        self.assertNotIn(self._fake_repo().resolve(), ws.private.parents)

    def test_each_new_private_repository_gets_a_fresh_name(self) -> None:
        """Docker Desktop can't bind a removed-and-recreated directory for a
        while, so a reused bookmark must never reuse the old private path."""
        first = jj_workspace_mod.plan(self._fake_repo(), "feat/x").private
        second = jj_workspace_mod.plan(self._fake_repo(), "feat/x").private
        self.assertNotEqual(first, second)

    def test_plan_finds_an_existing_private_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            ws_path = jj_workspace_mod.path_for(repo, "v1")
            root = ws_path.parent / ".session-vcs"
            (root / "v1.0123456789ab").mkdir(parents=True)
            (root / "v1.2.0123456789ab").mkdir()  # another bookmark's
            (root / ".v1.0123456789ab.host-jj").mkdir()  # parked, not a match
            self.assertEqual(
                jj_workspace_mod.plan(repo, "v1").private, root / "v1.0123456789ab"
            )
            (root / "v1.ba9876543210").mkdir()
            with self.assertRaises(SystemExit) as raised:
                jj_workspace_mod.plan(repo, "v1")
            self.assertIn("several private repositories", str(raised.exception))

    def test_metadata_mounts_cover_only_the_private_repository(self) -> None:
        ws = jj_workspace_mod.plan(self._fake_repo(), "feat/x")
        self.assertEqual(
            jj_workspace_mod.metadata_mounts(ws),
            [
                (ws.private / "jj", "/workspace/.jj"),
                (ws.private / "git", "/workspace/.git"),
            ],
        )

    def test_refused_paths_parses_jj_size_warning(self) -> None:
        stderr = (
            "Warning: Refused to snapshot some files:\n"
            "  big file.bin: 1.9MiB (2000000 bytes); the maximum size allowed is "
            "1.0MiB (1048576 bytes)\n"
            "  sub/b: 1.4MiB (1500000 bytes); the maximum size allowed is 1.0MiB "
            "(1048576 bytes)\n"
            "Hint: This is to prevent large files from being added by accident.\n"
            "  * Add the file(s) to `.gitignore`\n"
        )
        self.assertEqual(
            jj_workspace_mod.refused_paths(stderr), ["big file.bin", "sub/b"]
        )
        self.assertEqual(jj_workspace_mod.refused_paths("Working copy clean\n"), [])


class SetupLockTests(unittest.TestCase):
    def test_setup_holds_repository_lock_around_setup_decisions(self) -> None:
        repo = Path("/tmp/fake/repo")
        lock_active = False

        @contextlib.contextmanager
        def fake_lock(locked_repo):
            nonlocal lock_active
            self.assertEqual(locked_repo, repo.resolve())
            lock_active = True
            try:
                yield
            finally:
                lock_active = False

        def checked(*_args, **_kwargs):
            self.assertTrue(lock_active)
            return False

        with (
            patch.object(jj_workspace_mod, "_workspace_lock", side_effect=fake_lock),
            patch.object(jj_workspace_mod, "_bookmark_exists", side_effect=checked),
            patch.object(jj_workspace_mod, "_create", side_effect=checked) as create,
        ):
            ws = jj_workspace_mod.setup(repo, "feature")

        create.assert_called_once()
        self.assertEqual(ws.bookmark, "feature")
        self.assertFalse(lock_active)

    def test_remove_holds_same_repository_lock_as_setup(self) -> None:
        repo = Path("/tmp/fake/repo")
        ws = jj_workspace_mod.plan(repo, "feature")
        ws.created_bookmark = True
        ws.created_workspace = True
        lock_active = False

        @contextlib.contextmanager
        def fake_lock(locked_repo):
            nonlocal lock_active
            self.assertEqual(locked_repo, repo)
            lock_active = True
            try:
                yield
            finally:
                lock_active = False

        def fake_jj(*_args, **_kwargs):
            self.assertTrue(lock_active)
            return ""

        with (
            patch.object(jj_workspace_mod, "_workspace_lock", side_effect=fake_lock),
            patch.object(jj_workspace_mod, "_jj", side_effect=fake_jj),
            patch.object(jj_workspace_mod.shutil, "rmtree") as rmtree,
        ):
            jj_workspace_mod.remove(repo, ws)

        removed = [call.args[0] for call in rmtree.call_args_list]
        self.assertIn(ws.path, removed)
        self.assertIn(ws.private, removed)
        self.assertFalse(lock_active)


class ListWorkspacesTests(unittest.TestCase):
    """_list_workspaces() must trust a successful templated call outright,
    never re-classify its output by scanning for ':' — a character that can
    legitimately appear in a workspace path."""

    def test_colon_in_workspace_path_is_not_misparsed(self) -> None:
        repo = Path("/tmp/fake/repo")
        templated_out = "/tmp/fake/repo\n/tmp/fake/repo-workspaces/other:place\n"

        def fake_jj(_repo, args, capture=False):
            self.assertIn("--template", args)
            return templated_out

        with patch.object(jj_workspace_mod, "_jj", side_effect=fake_jj):
            paths = jj_workspace_mod._list_workspaces(repo)

        self.assertEqual(
            paths,
            ["/tmp/fake/repo", "/tmp/fake/repo-workspaces/other:place"],
        )

    def test_falls_back_to_human_format_only_when_template_call_fails(self) -> None:
        repo = Path("/tmp/fake/repo")
        human_out = (
            "default: nmptpmps 11fc3403 (empty) (no description set)\n"
            '"other:place": zupokots 1b71df22 (empty) (no description set)\n'
        )

        def fake_jj(_repo, args, capture=False):
            if "--template" in args:
                raise subprocess.CalledProcessError(1, "jj workspace list")
            return human_out

        with patch.object(jj_workspace_mod, "_jj", side_effect=fake_jj):
            paths = jj_workspace_mod._list_workspaces(repo)

        self.assertEqual(paths, ["nmptpmps 11fc3403", "zupokots 1b71df22"])


class _RepoTestCase(unittest.TestCase):
    def setUp(self) -> None:
        if not jj_workspace_mod._supports_no_colocate():
            self.skipTest("requires jj >= 0.46")
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _make_jj_repo(self.repo)

    def tearDown(self) -> None:
        self._td.cleanup()

    def _setup(self, bookmark: str, **kwargs) -> jj_workspace_mod.JjWorkspace:
        with contextlib.redirect_stdout(io.StringIO()):
            return jj_workspace_mod.setup(self.repo, bookmark, **kwargs)

    def _finalize(
        self, ws, *, keep_workspace=False, session_failed=False, message="msg"
    ):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            jj_workspace_mod.finalize(
                self.repo,
                ws,
                keep_workspace=keep_workspace,
                session_failed=session_failed,
                message=message,
            )
        return out.getvalue()

    def _bookmark_file(self, bookmark: str, path: str) -> str:
        return _jj(self.repo, "file", "show", "-r", bookmark, f"root:{path}")

    def _bookmark_is_empty(self, bookmark: str) -> bool:
        return (
            _jj(self.repo, "log", "-r", bookmark, "--no-graph", "-T", "empty").strip()
            == "true"
        )

    def _assert_nothing_left(self, ws) -> None:
        self.assertFalse(os.path.lexists(ws.path))
        self.assertFalse(os.path.lexists(ws.private))
        self.assertNotIn(str(ws.path), jj_workspace_mod._list_workspaces(self.repo))


@unittest.skipUnless(JJ, "jj not installed")
class IsolatedSetupTests(_RepoTestCase):
    def test_setup_creates_workspace_bookmark_and_private_repository(self) -> None:
        ws = self._setup("feat/hello")

        self.assertEqual(ws.bookmark, "feat/hello")
        self.assertTrue((ws.path / ".jj").is_dir())
        self.assertTrue(ws.private_jj.is_dir())
        self.assertTrue(ws.private_git.is_dir())
        self.assertTrue(jj_workspace_mod._bookmark_exists(self.repo, "feat/hello"))
        self.assertTrue(ws.created_bookmark and ws.created_workspace)

    def test_session_directory_has_only_an_empty_git_placeholder(self) -> None:
        ws = self._setup("feat/no-colocate")

        self.assertTrue((ws.path / ".git").is_dir())
        self.assertEqual(list((ws.path / ".git").iterdir()), [])
        self.assertFalse((self.repo / ".git" / "worktrees").exists())

    def test_host_workspace_is_clean_after_setup(self) -> None:
        (self.repo / "wip.txt").write_text("wip\n", encoding="utf-8")
        ws = self._setup("feat/clean")

        self.assertEqual(_jj(ws.path, "diff", "--summary"), "")
        self.assertTrue((ws.path / "wip.txt").is_file())  # started from main's @

    def test_private_repository_has_one_base_commit_and_a_clean_tree(self) -> None:
        ws = self._setup("feat/private")

        with _as_agent(ws) as workspace:
            descriptions = _jj(
                workspace,
                "log",
                "--no-graph",
                "-r",
                "::@ ~ root()",
                "-T",
                'description.first_line() ++ "\\n"',
            ).split("\n")
            self.assertEqual([d for d in descriptions if d], ["session base"])
            self.assertEqual(_jj(workspace, "diff", "--summary"), "")
            git_status = subprocess.run(
                ["git", "-C", str(workspace), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertEqual(git_status, "")
            # Self-contained: its store is a directory, not a pointer file
            # into the main repository, and its git backend is the private .git.
            self.assertTrue((workspace / ".jj" / "repo").is_dir())
            git_target = (
                workspace / ".jj" / "repo" / "store" / "git_target"
            ).read_text()
            self.assertFalse(os.path.isabs(git_target.strip()))
            self.assertEqual(
                (workspace / ".jj" / "repo" / "store" / git_target.strip()).resolve(),
                (workspace / ".git").resolve(),
            )

    def test_checkout_contains_no_ignored_files(self) -> None:
        (self.repo / "data").mkdir()
        (self.repo / "data" / "x.csv").write_text("1\n", encoding="utf-8")
        ws = self._setup("feat/ignored")
        self.assertFalse((ws.path / "data").exists())

    def test_failure_at_each_step_leaves_nothing_behind(self) -> None:
        real_jj = jj_workspace_mod._jj
        real_bare = jj_workspace_mod._jj_bare

        def failing(step):
            def fake_jj(repo, args, capture=False):
                if step in " ".join(args):
                    raise subprocess.CalledProcessError(1, step)
                return real_jj(repo, args, capture=capture)

            def fake_bare(args):
                if step in " ".join(args):
                    raise subprocess.CalledProcessError(1, step)
                return real_bare(args)

            return fake_jj, fake_bare

        for step in ("workspace add", "bookmark create", "git init", "session base"):
            with self.subTest(step=step):
                bookmark = f"feat/fail-{step.replace(' ', '-')}"
                fake_jj, fake_bare = failing(step)
                with (
                    patch.object(jj_workspace_mod, "_jj", side_effect=fake_jj),
                    patch.object(jj_workspace_mod, "_jj_bare", side_effect=fake_bare),
                    self.assertRaises(subprocess.CalledProcessError),
                ):
                    self._setup(bookmark)
                ws = jj_workspace_mod.plan(self.repo, bookmark)
                self._assert_nothing_left(ws)
                self.assertFalse(jj_workspace_mod._bookmark_exists(self.repo, bookmark))
                self.assertFalse(os.path.lexists(jj_workspace_mod._parked_host_jj(ws)))
        # The main checkout is untouched by the failed attempts.
        self.assertEqual(_jj(self.repo, "diff", "--summary"), "")

    def test_failed_rename_into_private_directory_leaves_nothing_behind(self) -> None:
        real_rename = os.rename

        def fake_rename(src, dst):
            if str(dst).endswith(os.sep + "jj") and ".session-vcs" in str(dst):
                raise OSError("injected")
            return real_rename(src, dst)

        with (
            patch.object(jj_workspace_mod.os, "rename", side_effect=fake_rename),
            self.assertRaises(OSError),
        ):
            self._setup("feat/fail-rename")
        ws = jj_workspace_mod.plan(self.repo, "feat/fail-rename")
        self._assert_nothing_left(ws)

    def test_setup_default_and_custom_workspace_dir(self) -> None:
        ws = self._setup("feat/hello")
        self.assertEqual(ws.path.parent, self.root / "repo-workspaces")
        custom = self.root / "custom-ws"
        ws2 = self._setup("feat/x", workspace_dir=custom)
        self.assertEqual(ws2.path.parent, custom)
        self.assertEqual(ws2.private.parent, custom / ".session-vcs")

    def test_setup_stale_unregistered_directory_raises(self) -> None:
        ws_path = jj_workspace_mod.path_for(self.repo, "feat/stale")
        ws_path.mkdir(parents=True)
        (ws_path / "stray.txt").write_text("leftover\n", encoding="utf-8")

        with self.assertRaises(SystemExit) as raised:
            self._setup("feat/stale")

        self.assertIn("not registered", str(raised.exception))
        self.assertTrue(ws_path.is_dir())  # must not be auto-deleted

    def test_reused_bookmark_starts_at_the_bookmark(self) -> None:
        ws = self._setup("feat/reuse")
        (ws.path / "reuse.txt").write_text("reuse\n", encoding="utf-8")
        self._finalize(ws)  # records on the bookmark, removes W and P

        ws2 = self._setup("feat/reuse")

        self.assertFalse(ws2.created_bookmark)
        self.assertTrue((ws2.path / "reuse.txt").is_file())

    def test_start_at_on_existing_bookmark_raises(self) -> None:
        ws = self._setup("feat/exists")
        self._finalize(ws)

        with self.assertRaises(SystemExit) as raised:
            self._setup("feat/exists", start_at="@")

        self.assertIn("already exists", str(raised.exception))

    def test_remove_cleans_up_workspace_private_repository_and_bookmark(self) -> None:
        ws = self._setup("feat/cleanup")

        jj_workspace_mod.remove(self.repo, ws)
        jj_workspace_mod.remove(self.repo, ws)  # idempotent

        self._assert_nothing_left(ws)
        self.assertFalse(jj_workspace_mod._bookmark_exists(self.repo, "feat/cleanup"))

    def test_remove_preserves_reused_bookmark_after_build_failure(self) -> None:
        ws1 = self._setup("feat/reuse-fail")
        (ws1.path / "work.txt").write_text("work\n", encoding="utf-8")
        self._finalize(ws1)

        ws2 = self._setup("feat/reuse-fail")
        self.assertFalse(ws2.created_bookmark)
        jj_workspace_mod.remove(self.repo, ws2)

        self._assert_nothing_left(ws2)
        self.assertEqual(self._bookmark_file("feat/reuse-fail", "work.txt"), "work\n")


@unittest.skipUnless(JJ, "jj not installed")
class IsolatedFinalizeTests(_RepoTestCase):
    def test_modified_added_and_deleted_files_land_in_one_commit(self) -> None:
        (self.repo / "gone.txt").write_text("gone\n", encoding="utf-8")
        ws = self._setup("feat/work")
        with _as_agent(ws) as workspace:
            (workspace / "a.txt").write_text("agent\n", encoding="utf-8")
            (workspace / "sub").mkdir()
            (workspace / "sub" / "new.txt").write_text("new\n", encoding="utf-8")
            (workspace / "gone.txt").unlink()
            _jj(workspace, "commit", "-m", "agent commit one")
            (workspace / "sub" / "new.txt").write_text("new 2\n", encoding="utf-8")

        out = self._finalize(ws, message="feat/work — 2026-11-10T10:00")

        self._assert_nothing_left(ws)
        self.assertIn("bookmark 'feat/work'", out)
        summary = _jj(self.repo, "diff", "--summary", "-r", "feat/work")
        self.assertEqual(
            sorted(summary.splitlines()),
            ["A sub/new.txt", "D gone.txt", "M a.txt"],
        )
        self.assertEqual(self._bookmark_file("feat/work", "sub/new.txt"), "new 2\n")
        all_descriptions = _jj(
            self.repo, "log", "--no-graph", "-r", "all()", "-T", "description"
        )
        self.assertNotIn("agent commit one", all_descriptions)
        self.assertNotIn("session base", all_descriptions)
        self.assertIn("feat/work — 2026-11-10T10:00", all_descriptions)

    def test_symlink_is_recorded_as_a_link(self) -> None:
        ws = self._setup("feat/link")
        outside = self.root / "secret.txt"
        outside.write_text("secret\n", encoding="utf-8")
        with _as_agent(ws) as workspace:
            (workspace / "link").symlink_to(outside)

        self._finalize(ws)

        diff = _jj(self.repo, "diff", "--git", "-r", "feat/link", "root:link")
        self.assertIn("new file mode 120000", diff)
        self.assertNotIn("secret", diff.replace(str(outside), ""))

    def test_unchanged_session_creates_no_non_empty_commit(self) -> None:
        start = _jj(self.repo, "log", "--no-graph", "-r", "@-", "-T", "commit_id")
        ws = self._setup("feat/nothing")
        with _as_agent(ws):
            pass

        self._finalize(ws, message="feat/nothing — 2026-11-10T10:00")

        self.assertFalse(self._bookmark_is_empty("feat/nothing"))
        tip = _jj(
            self.repo, "log", "--no-graph", "-r", "feat/nothing", "-T", "commit_id"
        )
        self.assertEqual(tip, start)

    def test_agent_planted_hook_and_config_never_reach_the_main_repository(
        self,
    ) -> None:
        hooks = self.repo / ".git" / "hooks"
        before_config = (self.repo / ".git" / "config").read_bytes()
        before_hooks = sorted(p.name for p in hooks.iterdir()) if hooks.is_dir() else []
        ws = self._setup("feat/hook")
        with _as_agent(ws) as workspace:
            (workspace / ".git" / "hooks").mkdir(exist_ok=True)
            (workspace / ".git" / "hooks" / "pre-commit").write_text(
                "#!/bin/sh\ntouch /tmp/pwned\n", encoding="utf-8"
            )
            with (workspace / ".git" / "config").open("a", encoding="utf-8") as f:
                f.write("[core]\n\tfsmonitor = /tmp/evil\n")

        self._finalize(ws)

        self.assertEqual((self.repo / ".git" / "config").read_bytes(), before_config)
        after_hooks = sorted(p.name for p in hooks.iterdir()) if hooks.is_dir() else []
        self.assertEqual(after_hooks, before_hooks)

    def test_refused_large_file_is_reported_and_directory_kept(self) -> None:
        _jj(self.repo, "config", "set", "--repo", "snapshot.max-new-file-size", "1000")
        ws = self._setup("feat/large")
        with _as_agent(ws) as workspace:
            (workspace / "big.bin").write_bytes(b"\0" * 5000)
            (workspace / "small.txt").write_text("small\n", encoding="utf-8")

        out = self._finalize(ws)

        self.assertIn("big.bin", out)
        self.assertIn("snapshot.max-new-file-size", out)
        self.assertTrue(ws.path.is_dir())
        self.assertTrue(ws.private.is_dir())
        self.assertEqual(self._bookmark_file("feat/large", "small.txt"), "small\n")

    def test_capture_failure_leaves_workspace_without_raising(self) -> None:
        ws = self._setup("feat/fail")

        def failing_jj(repo, args, capture=False):
            if "bookmark" in args:
                raise subprocess.CalledProcessError(1, "jj bookmark set")
            return ""

        with patch.object(jj_workspace_mod, "_jj", side_effect=failing_jj):
            out = self._finalize(ws)  # must not raise

        self.assertTrue(ws.path.is_dir(), "workspace kept so work is not lost")
        self.assertIn("could not capture", out)

    def test_keep_workspace_and_failed_session_keep_both_directories(self) -> None:
        for flags in ({"keep_workspace": True}, {"session_failed": True}):
            with self.subTest(**flags):
                bookmark = "feat/keep" if "keep_workspace" in flags else "feat/failed"
                ws = self._setup(bookmark)
                (ws.path / "work.txt").write_text("work\n", encoding="utf-8")
                self._finalize(ws, **flags)
                self.assertTrue(ws.path.is_dir())
                self.assertTrue(ws.private_jj.is_dir())
                self.assertEqual(self._bookmark_file(bookmark, "work.txt"), "work\n")

    def test_finalize_snapshots_when_nothing_ran_jj(self) -> None:
        ws = self._setup("feat/no-jj")
        (ws.path / "silent.txt").write_text("unsnapshotted\n", encoding="utf-8")

        self._finalize(ws)

        self.assertEqual(
            self._bookmark_file("feat/no-jj", "silent.txt"), "unsnapshotted\n"
        )


@unittest.skipUnless(JJ, "jj not installed")
class IsolatedResumeTests(_RepoTestCase):
    def test_kept_session_is_resumed_with_its_private_repository(self) -> None:
        ws = self._setup("feat/resume")
        with _as_agent(ws) as workspace:
            (workspace / "first.txt").write_text("1\n", encoding="utf-8")
            _jj(workspace, "commit", "-m", "agent first session")
        self._finalize(ws, keep_workspace=True)

        ws2 = self._setup("feat/resume")

        self.assertEqual(ws2.path, ws.path)
        self.assertFalse(ws2.created_workspace or ws2.created_bookmark)
        with _as_agent(ws2) as workspace:
            log = _jj(
                workspace, "log", "--no-graph", "-r", "all()", "-T", "description"
            )
            self.assertIn("agent first session", log)
            self.assertTrue((workspace / "first.txt").is_file())

    def test_session_directory_without_private_repository_is_refused(self) -> None:
        ws = self._setup("feat/no-private")
        self._finalize(ws, keep_workspace=True)
        shutil.rmtree(ws.private)

        with self.assertRaises(SystemExit) as raised:
            self._setup("feat/no-private")

        msg = str(raised.exception)
        self.assertIn("has no private repository", msg)
        self.assertIn("jj workspace forget", msg)
        self.assertTrue(ws.path.is_dir())

    def test_private_repository_without_session_directory_is_refused(self) -> None:
        ws = self._setup("feat/no-ws")
        self._finalize(ws, keep_workspace=True)
        _jj(self.repo, "workspace", "forget", ws.path.name)
        shutil.rmtree(ws.path)

        with self.assertRaises(SystemExit) as raised:
            self._setup("feat/no-ws")

        self.assertIn("without its session directory", str(raised.exception))
        self.assertIn(f"rm -rf {ws.private}", str(raised.exception))

    def test_workspace_kept_by_an_older_version_is_refused(self) -> None:
        ws_path = jj_workspace_mod.path_for(self.repo, "feat/old")
        ws_path.parent.mkdir(parents=True)
        _jj(self.repo, "workspace", "add", "--no-colocate", str(ws_path))
        (ws_path / "work.txt").write_text("work\n", encoding="utf-8")

        with self.assertRaises(SystemExit) as raised:
            self._setup("feat/old")

        msg = str(raised.exception)
        self.assertIn("older project-sandbox version", msg)
        self.assertIn("bookmark set", msg)
        self.assertTrue((ws_path / "work.txt").is_file())

    def test_replaced_placeholder_is_refused(self) -> None:
        ws = self._setup("feat/placeholder")
        self._finalize(ws, keep_workspace=True)
        os.rmdir(ws.path / ".git")
        (ws.path / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")

        with self.assertRaises(SystemExit) as raised:
            self._setup("feat/placeholder")

        self.assertIn("no longer the empty placeholder", str(raised.exception))


@unittest.skipUnless(JJ, "jj not installed")
class IsolatedShareTests(_RepoTestCase):
    def test_share_gets_a_placeholder_and_a_private_exclude_entry(self) -> None:
        ws = self._setup("feat/share", shares=["data", "out/reports"])

        self.assertTrue((ws.path / "data").is_dir())
        self.assertTrue((ws.path / "out" / "reports").is_dir())
        exclude = (ws.private_git / "info" / "exclude").read_text(encoding="utf-8")
        self.assertIn("/data/\n", exclude)
        self.assertIn("/out/reports/\n", exclude)
        # The host doesn't record the empty placeholders.
        self.assertEqual(_jj(ws.path, "diff", "--summary"), "")

    def test_agent_tools_do_not_report_files_in_a_share(self) -> None:
        ws = self._setup("feat/share-status", shares=["out"])
        with _as_agent(ws) as workspace:
            # Stands in for the mounted share's content.
            (workspace / "out" / "report.txt").write_text("r\n", encoding="utf-8")
            self.assertEqual(_jj(workspace, "diff", "--summary"), "")
            git_status = subprocess.run(
                ["git", "-C", str(workspace), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertEqual(git_status, "")
            (workspace / "out" / "report.txt").unlink()

    def test_tracked_path_is_refused_and_nothing_is_left(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            self._setup("feat/share-tracked", shares=["a.txt"])

        self.assertIn("already contains a.txt", str(raised.exception))
        self._assert_nothing_left(
            jj_workspace_mod.plan(self.repo, "feat/share-tracked")
        )
        self.assertFalse(
            jj_workspace_mod._bookmark_exists(self.repo, "feat/share-tracked")
        )

    def test_tracked_symlink_on_the_share_path_is_refused(self) -> None:
        (self.repo / "linked").symlink_to(self.root)
        _jj(self.repo, "commit", "-m", "add link")

        with self.assertRaises(SystemExit) as raised:
            self._setup("feat/share-link", shares=["linked/data"])

        self.assertIn("symbolic link", str(raised.exception))
        self.assertFalse((self.root / "data").exists())

    def test_resumed_session_accepts_its_own_placeholder(self) -> None:
        ws = self._setup("feat/share-resume", shares=["data"])
        self._finalize(ws, keep_workspace=True)

        ws2 = self._setup("feat/share-resume", shares=["data"])

        exclude = (ws2.private_git / "info" / "exclude").read_text(encoding="utf-8")
        self.assertEqual(exclude.count("/data/\n"), 1)


@unittest.skipUnless(
    os.environ.get("PROJECT_SANDBOX_CONTAINER_TESTS") == "1",
    "set PROJECT_SANDBOX_CONTAINER_TESTS=1 to run real-container tests",
)
class JjWorkspaceDockerEndToEndTests(unittest.TestCase):
    """A container edits the isolated session; the host records the result."""

    @classmethod
    def setUpClass(cls) -> None:
        if not JJ:
            raise unittest.SkipTest("jj is required")
        cls.docker = _find_docker()
        if cls.docker is None:
            raise unittest.SkipTest("docker/podman not available")

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _make_jj_repo(self.repo)

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_container_change_is_recorded_on_the_bookmark(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            ws = jj_workspace_mod.setup(self.repo, "feat/e2e")
        assert self.docker is not None
        mounts = ["--mount", f"type=bind,source={ws.path},target=/workspace"]
        for source, target in jj_workspace_mod.metadata_mounts(ws):
            mounts += ["--mount", f"type=bind,source={source},target={target}"]
        subprocess.run(
            [
                self.docker,
                "run",
                "--rm",
                *mounts,
                "--workdir",
                "/workspace",
                DOCKER_IMAGE,
                "bash",
                "-c",
                (
                    "echo 'hello from container' > agent_output.txt; "
                    "test ! -e /workspace/.jj/repo/store/git_target || "
                    "grep -qv '^/' /workspace/.jj/repo/store/git_target"
                ),
            ],
            check=True,
        )

        with contextlib.redirect_stdout(io.StringIO()):
            jj_workspace_mod.finalize(
                self.repo, ws, keep_workspace=False, session_failed=False, message="msg"
            )

        self.assertFalse(ws.path.exists())
        self.assertEqual(
            _jj(self.repo, "file", "show", "-r", "feat/e2e", "root:agent_output.txt"),
            "hello from container\n",
        )
