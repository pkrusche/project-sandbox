import contextlib
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import jj_workspace as jj_workspace_mod

JJ = shutil.which("jj")


def _find_docker() -> str | None:
    for cmd in ("docker", "podman"):
        path = shutil.which(cmd)
        if path is None:
            continue
        try:
            if (
                subprocess.run(
                    [path, "info"], capture_output=True, timeout=5
                ).returncode
                == 0
            ):
                return path
        except (subprocess.TimeoutExpired, OSError):
            continue
    return None


DOCKER = _find_docker()
DOCKER_IMAGE = "ubuntu:22.04"


def _make_jj_repo(path: Path) -> None:
    """Init a jj git-backed repo with one committed change and an empty @ on top."""
    subprocess.run(["jj", "git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["jj", "-R", str(path), "config", "set", "--repo", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["jj", "-R", str(path), "config", "set", "--repo", "user.email", "t@test.com"],
        check=True,
        capture_output=True,
    )
    (path / "a.txt").write_text("init\n", encoding="utf-8")
    subprocess.run(
        ["jj", "-R", str(path), "describe", "-m", "init"],
        check=True,
        capture_output=True,
    )
    subprocess.run(["jj", "-R", str(path), "new"], check=True, capture_output=True)


class PathForCollisionTests(unittest.TestCase):
    """Unit tests for path_for() disambiguation — no jj binary required."""

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

    def test_repo_store_mount_resolves_relative_workspace_pointer_in_container(
        self,
    ) -> None:
        repo = Path("/tmp/root/repo")
        ws = Path("/tmp/root/repo-workspaces/feat")

        source, target = jj_workspace_mod.repo_store_mount(repo, ws)

        self.assertEqual(source, (repo / ".jj" / "repo").resolve())
        self.assertEqual(target, "/repo/.jj/repo")

    def test_git_backend_mount_returns_none_without_git_target(self) -> None:
        # No real repo on disk, so the store/git_target file is absent.
        repo = Path("/tmp/root/repo")
        ws = Path("/tmp/root/repo-workspaces/feat")

        self.assertIsNone(jj_workspace_mod.git_backend_mount(repo, ws))


class ListWorkspacesTests(unittest.TestCase):
    """_list_workspaces() must trust a successful templated call outright,
    never re-classify its output by scanning for ':' — a character that can
    legitimately appear in a workspace path."""

    def test_colon_in_workspace_path_is_not_misparsed(self) -> None:
        repo = Path("/tmp/fake/repo")
        # Simulates `jj workspace list --template 'root ++ "\n"'` succeeding
        # with one workspace path that happens to contain a colon.
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

        self.assertEqual(
            paths,
            ["nmptpmps 11fc3403", "zupokots 1b71df22"],
        )


@unittest.skipUnless(JJ, "jj not installed")
class JjWorkspaceSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _make_jj_repo(self.repo)

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_setup_creates_workspace_and_bookmark(self) -> None:
        ws = jj_workspace_mod.setup(self.repo, "feat/hello")

        self.assertEqual(ws.bookmark, "feat/hello")
        self.assertTrue(ws.path.is_dir())
        # Workspace directory should have jj state
        self.assertTrue((ws.path / ".jj").exists())

    def test_git_backend_mount_points_at_main_repo_git_dir(self) -> None:
        ws = jj_workspace_mod.setup(self.repo, "feat/hello")

        mount = jj_workspace_mod.git_backend_mount(self.repo, ws.path)
        self.assertIsNotNone(mount)
        source, target = mount

        # Source is the real git backend the store points at, and it exists.
        self.assertEqual(source, (self.repo / ".git").resolve())
        self.assertTrue(source.exists())
        # Container target mirrors the store mount so jj's git_target resolves:
        # store at /repo/.jj/repo, backend at /repo/.git.
        _, store_target = jj_workspace_mod.repo_store_mount(self.repo, ws.path)
        self.assertEqual(target, "/repo/.git")
        self.assertEqual(store_target, "/repo/.jj/repo")

    def test_setup_default_workspace_dir(self) -> None:
        ws = jj_workspace_mod.setup(self.repo, "feat/hello")

        expected_root = self.repo.resolve().parent / f"{self.repo.name}-workspaces"
        import hashlib

        suffix = hashlib.sha256(b"feat/hello").hexdigest()[:6]
        self.assertEqual(ws.path.resolve(), expected_root / f"feat-hello-{suffix}")

    def test_setup_custom_workspace_dir(self) -> None:
        custom = self.root / "custom-ws"
        ws = jj_workspace_mod.setup(self.repo, "feat/x", workspace_dir=custom)

        self.assertTrue(ws.path.is_dir())
        self.assertEqual(ws.path.parent, custom)

    def test_setup_idempotent_reuse(self) -> None:
        ws1 = jj_workspace_mod.setup(self.repo, "feat/x")
        ws2 = jj_workspace_mod.setup(self.repo, "feat/x")

        self.assertEqual(ws1.path, ws2.path)

    def test_setup_bookmark_visible_in_log(self) -> None:
        ws = jj_workspace_mod.setup(self.repo, "my-feature")

        log_out = subprocess.run(
            ["jj", "-R", str(ws.path), "log", "--no-pager"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertIn("my-feature", log_out)

    def test_setup_non_empty_current_revision_starts_from_current_tree(self) -> None:
        (self.repo / "current.txt").write_text("current\n", encoding="utf-8")

        ws = jj_workspace_mod.setup(self.repo, "feat/current")

        self.assertTrue((ws.path / "current.txt").is_file())

    def test_setup_stale_unregistered_directory_raises(self) -> None:
        ws_path = jj_workspace_mod.path_for(self.repo, "feat/stale")
        ws_path.mkdir(parents=True)
        (ws_path / "stray.txt").write_text("leftover\n", encoding="utf-8")

        with self.assertRaises(SystemExit) as raised:
            jj_workspace_mod.setup(self.repo, "feat/stale")

        msg = str(raised.exception)
        self.assertIn(str(ws_path), msg)
        self.assertIn("not registered", msg)
        self.assertTrue(ws_path.is_dir())  # must not be auto-deleted

    def test_setup_reuses_existing_bookmark_starting_at_bookmark(self) -> None:
        """Retry after teardown leaves the bookmark but no workspace; the reused
        workspace must start at the bookmark's commit, not the default @."""
        ws = jj_workspace_mod.setup(self.repo, "feat/reuse")
        ws_name = ws.path.name
        # Put work on the bookmark, then simulate teardown: advance the bookmark
        # to @, forget the workspace, and remove the directory.
        (ws.path / "reuse.txt").write_text("reuse\n", encoding="utf-8")
        subprocess.run(
            ["jj", "-R", str(ws.path), "describe", "-m", "reuse work"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "jj",
                "-R",
                str(ws.path),
                "bookmark",
                "set",
                "--allow-backwards",
                "feat/reuse",
                "-r",
                "@",
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["jj", "-R", str(self.repo), "workspace", "forget", ws_name],
            check=True,
            capture_output=True,
        )
        shutil.rmtree(ws.path)

        ws2 = jj_workspace_mod.setup(self.repo, "feat/reuse")

        self.assertEqual(ws2.bookmark, "feat/reuse")
        self.assertTrue(ws2.path.is_dir())
        # The bookmark's work must be reachable in the reused workspace (its @ is a
        # descendant of the bookmark), not lost by starting from the default @.
        self.assertTrue((ws2.path / "reuse.txt").is_file())

    def test_setup_start_at_on_existing_bookmark_raises(self) -> None:
        jj_workspace_mod.setup(self.repo, "feat/exists")
        subprocess.run(
            [
                "jj",
                "-R",
                str(self.repo),
                "workspace",
                "forget",
                jj_workspace_mod.path_for(self.repo, "feat/exists").name,
            ],
            check=True,
            capture_output=True,
        )
        shutil.rmtree(jj_workspace_mod.path_for(self.repo, "feat/exists"))

        with self.assertRaises(SystemExit) as raised:
            jj_workspace_mod.setup(self.repo, "feat/exists", start_at="@")

        msg = str(raised.exception)
        self.assertIn("feat/exists", msg)
        self.assertIn("already exists", msg)

    def test_remove_cleans_up_workspace_and_bookmark(self) -> None:
        ws = jj_workspace_mod.setup(self.repo, "feat/cleanup")

        jj_workspace_mod.remove(self.repo, ws)

        self.assertFalse(ws.path.exists())
        existing = jj_workspace_mod._list_workspaces(self.repo)
        self.assertFalse(any(ws.path.name in e for e in existing))
        bookmark_out = subprocess.run(
            ["jj", "-R", str(self.repo), "bookmark", "list"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertNotIn("feat/cleanup", bookmark_out)

    def test_remove_is_idempotent(self) -> None:
        ws = jj_workspace_mod.setup(self.repo, "feat/idempotent-remove")
        jj_workspace_mod.remove(self.repo, ws)
        jj_workspace_mod.remove(self.repo, ws)  # should not raise

    def test_remove_preserves_reused_bookmark_after_build_failure(self) -> None:
        """A build failure (agent never ran) on a *reused* bookmark must not
        delete that bookmark: the prior session's work is only reachable through
        it. Only the freshly-created empty workspace should be dropped."""
        # First session: put real work on the bookmark, then simulate a
        # no-keep teardown (bookmark survives, workspace forgotten + removed).
        ws1 = jj_workspace_mod.setup(self.repo, "feat/reuse-fail")
        (ws1.path / "work.txt").write_text("work\n", encoding="utf-8")
        subprocess.run(
            ["jj", "-R", str(ws1.path), "describe", "-m", "real work"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "jj",
                "-R",
                str(ws1.path),
                "bookmark",
                "set",
                "--allow-backwards",
                "feat/reuse-fail",
                "-r",
                "@",
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["jj", "-R", str(self.repo), "workspace", "forget", ws1.path.name],
            check=True,
            capture_output=True,
        )
        shutil.rmtree(ws1.path)

        # Second session reuses the bookmark, but the build fails before the
        # agent runs, so cli calls remove() on the new workspace.
        ws2 = jj_workspace_mod.setup(self.repo, "feat/reuse-fail")
        self.assertFalse(ws2.created_bookmark)
        self.assertTrue(ws2.created_workspace)

        jj_workspace_mod.remove(self.repo, ws2)

        # The empty new workspace is gone, but the bookmark and its work remain.
        self.assertFalse(ws2.path.exists())
        bookmark_out = subprocess.run(
            ["jj", "-R", str(self.repo), "bookmark", "list"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertIn("feat/reuse-fail", bookmark_out)
        self.assertTrue(jj_workspace_mod._bookmark_exists(self.repo, "feat/reuse-fail"))


@unittest.skipUnless(JJ, "jj not installed")
class JjWorkspaceTeardownTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _make_jj_repo(self.repo)
        self.ws = jj_workspace_mod.setup(self.repo, "feat/work")
        # Add a file and describe the change in the workspace
        (self.ws.path / "work.txt").write_text("work\n", encoding="utf-8")
        subprocess.run(
            ["jj", "-R", str(self.ws.path), "describe", "-m", "agent work"],
            check=True,
            capture_output=True,
        )

    def tearDown(self) -> None:
        self._td.cleanup()

    def _finalize(
        self, ws=None, *, keep_workspace=False, session_failed=False, message="msg"
    ):
        jj_workspace_mod.finalize(
            self.repo,
            ws or self.ws,
            keep_workspace=keep_workspace,
            session_failed=session_failed,
            message=message,
        )

    def _bookmark_file(self, bookmark: str, path: str) -> str:
        return subprocess.run(
            ["jj", "-R", str(self.repo), "file", "show", "-r", bookmark, path],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    def test_finalize_removes_workspace_and_advances_bookmark(self) -> None:
        self._finalize()

        # Workspace gone; the bookmark holds the work but it is NOT rebased onto
        # the default workspace's @ (no integration into main).
        self.assertFalse(self.ws.path.exists())
        self.assertEqual(self._bookmark_file("feat/work", "root:work.txt"), "work\n")

    def test_finalize_snapshots_when_agent_never_ran_jj(self) -> None:
        """The agent may edit files without ever running a jj command, leaving
        the working copy un-snapshotted (its @ shows empty from outside). finalize
        must force the snapshot itself (via ``jj status``) so the edits land on the
        bookmark and survive workspace removal — otherwise work is silently lost.
        """
        ws = jj_workspace_mod.setup(self.repo, "feat/no-jj")
        # Edit files directly on disk. Do NOT run any jj command in the workspace,
        # so nothing snapshots @ before finalize does.
        (ws.path / "silent.txt").write_text("unsnapshotted work\n", encoding="utf-8")
        # Sanity check: from outside, @ is still empty (jj snapshots lazily).
        empty_before = subprocess.run(
            [
                "jj",
                "-R",
                str(self.repo),
                "log",
                "-r",
                "feat/no-jj",
                "--no-graph",
                "--template",
                "empty",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(empty_before, "true")

        self._finalize(ws)

        self.assertFalse(ws.path.exists())
        self.assertEqual(
            self._bookmark_file("feat/no-jj", "root:silent.txt"),
            "unsnapshotted work\n",
        )

    def _bookmark_is_empty(self, bookmark: str) -> bool:
        return (
            subprocess.run(
                [
                    "jj",
                    "-R",
                    str(self.repo),
                    "log",
                    "-r",
                    bookmark,
                    "--no-graph",
                    "--template",
                    "empty",
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            == "true"
        )

    def test_finalize_empty_session_does_not_leave_empty_bookmark_tip(self) -> None:
        """An empty session (agent changed nothing) must not advance the bookmark
        onto an empty commit; it lands on @-'s non-empty revision instead."""
        ws = jj_workspace_mod.setup(self.repo, "feat/nothing")
        # No edits and no jj commands in the workspace: @ stays empty.
        self._finalize(ws, message="feat/nothing — 2026-07-01T10:00")

        self.assertFalse(ws.path.exists())
        self.assertFalse(
            self._bookmark_is_empty("feat/nothing"),
            "bookmark must not point at an empty commit",
        )
        # The base commit it lands on must not be relabelled with the session
        # message (we never describe when @ is empty).
        desc = subprocess.run(
            [
                "jj",
                "-R",
                str(self.repo),
                "log",
                "-r",
                "feat/nothing",
                "--no-graph",
                "--template",
                "description",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertNotIn("feat/nothing — 2026-07-01T10:00", desc)

    def test_finalize_captures_committed_work_under_empty_at(self) -> None:
        """If the agent commits work and leaves an empty @ on top, finalize must
        advance the bookmark to @- so the committed work is not stranded."""
        ws = jj_workspace_mod.setup(self.repo, "feat/committed")
        (ws.path / "committed.txt").write_text("committed\n", encoding="utf-8")
        subprocess.run(
            ["jj", "-R", str(ws.path), "commit", "-m", "agent commit"],
            check=True,
            capture_output=True,
        )
        # @ is now a fresh empty commit on top of the agent's committed work.
        self.assertTrue(jj_workspace_mod._is_empty(ws.path, "@"))

        self._finalize(ws, message="feat/committed — 2026-07-01T10:00")

        self.assertFalse(ws.path.exists())
        self.assertEqual(
            self._bookmark_file("feat/committed", "root:committed.txt"), "committed\n"
        )
        self.assertFalse(self._bookmark_is_empty("feat/committed"))

    def test_finalize_capture_failure_leaves_workspace_without_raising(self) -> None:
        # finalize runs from main()'s finally block: a failed jj command must be
        # reported, not raised (which would mask the session exit code).
        out = io.StringIO()

        def failing_jj(repo, args, capture=False):
            if "bookmark" in args:
                raise subprocess.CalledProcessError(1, "jj bookmark set")
            return ""

        with (
            patch.object(jj_workspace_mod, "_jj", side_effect=failing_jj),
            contextlib.redirect_stdout(out),
        ):
            self._finalize()  # must not raise

        self.assertTrue(self.ws.path.is_dir(), "workspace kept so work is not lost")
        self.assertIn("could not capture", out.getvalue())

    def test_finalize_keep_workspace_leaves_workspace(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self._finalize(keep_workspace=True)

        self.assertTrue(self.ws.path.is_dir())
        self.assertIn("kept", out.getvalue())
        self.assertEqual(self._bookmark_file("feat/work", "root:work.txt"), "work\n")

    def test_finalize_session_failed_leaves_workspace(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self._finalize(session_failed=True)

        self.assertTrue(self.ws.path.is_dir())
        self.assertIn("failed", out.getvalue())
        # Work is still captured on the bookmark for inspection.
        self.assertEqual(self._bookmark_file("feat/work", "root:work.txt"), "work\n")

    def test_finalize_snapshots_plain_file_edits(self) -> None:
        ws = jj_workspace_mod.setup(self.repo, "feat/plain-edits")
        (ws.path / "plain.txt").write_text("plain\n", encoding="utf-8")

        self._finalize(ws)

        self.assertFalse(ws.path.exists())
        self.assertEqual(
            self._bookmark_file("feat/plain-edits", "root:plain.txt"), "plain\n"
        )

    def test_finalize_describes_empty_description_with_message(self) -> None:
        # A workspace whose @ has no description gets described with the message.
        ws = jj_workspace_mod.setup(self.repo, "feat/undescribed")
        (ws.path / "undescribed.txt").write_text("x\n", encoding="utf-8")

        self._finalize(ws, message="feat/undescribed — 2026-07-01T09:10")

        log_out = subprocess.run(
            ["jj", "-R", str(self.repo), "log", "--no-pager", "-r", "feat/undescribed"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertIn("feat/undescribed — 2026-07-01T09:10", log_out)


@unittest.skipUnless(JJ and DOCKER, "jj and docker/podman both required")
class JjWorkspaceDockerEndToEndTests(unittest.TestCase):
    """Container writes a file; host describes+verifies revision; teardown cleans up."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _make_jj_repo(self.repo)

    def tearDown(self) -> None:
        self._td.cleanup()

    def _docker(self, ws_path: Path, bash_cmd: str) -> None:
        jj_dir = str((self.repo / ".jj").resolve())
        subprocess.run(
            [
                DOCKER,
                "run",
                "--rm",
                "--mount",
                f"type=bind,source={ws_path},target=/workspace",
                "--mount",
                f"type=bind,source={jj_dir},target={jj_dir}",
                "--workdir",
                "/workspace",
                DOCKER_IMAGE,
                "bash",
                "-c",
                bash_cmd,
            ],
            check=True,
        )

    def test_container_adds_file_tree_and_revision_visible_on_host(self) -> None:
        # 1. Create workspace with bookmark
        ws = jj_workspace_mod.setup(self.repo, "feat/e2e")

        # 2. Container writes file via bash (jj auto-tracks working-copy changes)
        self._docker(ws.path, "echo 'hello from container' > agent_output.txt")

        # 3. Host labels the auto-tracked change
        subprocess.run(
            ["jj", "-R", str(ws.path), "describe", "-m", "agent: add agent_output"],
            check=True,
            capture_output=True,
        )

        # 4. Show tree — file present on host
        self.assertTrue((ws.path / "agent_output.txt").exists())
        tree_out = subprocess.run(
            ["find", str(ws.path), "-not", "-path", f"{ws.path}/.jj*", "-type", "f"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertIn("agent_output.txt", tree_out)

        # 5. Show revision — bookmark and message appear in jj log
        log_out = subprocess.run(
            ["jj", "-R", str(ws.path), "log", "--no-pager"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertIn("agent: add agent_output", log_out)
        self.assertIn("feat/e2e", log_out)

    def test_container_change_survives_finalize(self) -> None:
        ws = jj_workspace_mod.setup(self.repo, "feat/e2e-finalize")
        self._docker(ws.path, "echo 'hello from container' > agent_output.txt")
        subprocess.run(
            ["jj", "-R", str(ws.path), "describe", "-m", "agent: add file"],
            check=True,
            capture_output=True,
        )

        jj_workspace_mod.finalize(
            self.repo, ws, keep_workspace=False, session_failed=False, message="msg"
        )

        self.assertFalse(ws.path.exists())  # workspace removed
        # The change is captured on the bookmark.
        file_out = subprocess.run(
            [
                "jj",
                "-R",
                str(self.repo),
                "file",
                "show",
                "-r",
                "feat/e2e-finalize",
                "root:agent_output.txt",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertEqual(file_out, "hello from container\n")
