"""CLI behavior of the VCS session modes (vcs-session-modes change)."""

import argparse
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import cli, host_locks, vcs_mode
from project_sandbox import jj_workspace as jj_workspace_mod
from project_sandbox.git_identity import GitIdentity

JJ = shutil.which("jj")
SESSION_ID = "20261110T090000-0123456789ab"
MOUNT_RE = re.compile(r"type=bind,source=([^,'\"\s]+),target=([^,'\"\s]+)(,readonly)?")


def _agent_paths(home: Path) -> dict[str, Path]:
    return {
        "claude": home / ".claude",
        "codex": home / ".codex",
        "opencode": home / ".config" / "opencode",
        "pi": home / ".pi" / "agent",
    }


def _jj(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["jj", "-R", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        cwd=repo,
    ).stdout


def _make_jj_repo(path: Path, *, colocate: bool = True) -> None:
    path.mkdir(parents=True, exist_ok=True)
    init = ["jj", "git", "init", str(path)]
    if colocate:
        init.insert(3, "--colocate")
    subprocess.run(init, check=True, capture_output=True)
    _jj(path, "config", "set", "--repo", "user.name", "Test")
    _jj(path, "config", "set", "--repo", "user.email", "t@test.com")
    (path / "a.txt").write_text("init\n", encoding="utf-8")
    (path / "src").mkdir()
    (path / "src" / "main.py").write_text("print(1)\n", encoding="utf-8")
    (path / ".gitignore").write_text(
        "data/\nout/\n.project-sandbox/\n", encoding="utf-8"
    )
    _jj(path, "commit", "-m", "init")


def _make_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "README.md").write_text("init\n", encoding="utf-8")


def _snapshot(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


class _CliHarness(TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name).resolve()
        self.state = self.root / "state"
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state)})
        env.start()
        self.addCleanup(env.stop)
        session_id = patch.object(
            cli.observability, "new_session_id", return_value=SESSION_ID
        )
        session_id.start()
        self.addCleanup(session_id.stop)

    def tearDown(self) -> None:
        self._td.cleanup()

    def _common_patches(self, stack: contextlib.ExitStack, project: Path) -> None:
        paths = _agent_paths(self.root / "home")
        paths["claude"].mkdir(parents=True, exist_ok=True)
        stack.enter_context(
            patch.object(cli, "read_identity", return_value=GitIdentity("A", "a@b.c"))
        )
        stack.enter_context(
            patch.object(cli.config_agents, "_agent_host_paths", return_value=paths)
        )

    def dry_run(self, project: Path, *extra: str, runtime: str = "docker") -> str:
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            self._common_patches(stack, project)
            stack.enter_context(contextlib.redirect_stdout(out))
            rc = cli.main(
                [
                    "--dry-run",
                    "--no-build",
                    "--no-firewall",
                    "--runtime",
                    runtime,
                    "--agent",
                    "bash",
                    *extra,
                    str(project),
                    "python:3.12-slim",
                ]
            )
        self.assertEqual(rc, 0)
        return out.getvalue()

    def launch(
        self,
        project: Path,
        *extra: str,
        agent=None,
        exit_code: int = 0,
        build_rc: int = 0,
        run_error: BaseException | None = None,
        headless: bool = False,
    ):
        """Run a session with a mocked runtime; ``agent(workspace)`` stands in
        for what the agent does inside the container."""
        out = io.StringIO()
        seen: dict = {}

        def fake_run(cmd, *args, **kwargs):
            seen["cmd"] = cmd
            seen["stdout_at_start"] = out.getvalue()
            mounts = {
                target: source for source, target, _ in MOUNT_RE.findall(" ".join(cmd))
            }
            seen["mounts"] = mounts
            if run_error is not None:
                raise run_error
            if agent is not None:
                agent(Path(mounts["/workspace"]))
            log_path = kwargs.get("log_path")
            if log_path is not None:
                Path(log_path).write_text("done\n", encoding="utf-8")
            return exit_code

        argv = ["--no-firewall", "--runtime", "docker", "--agent", "bash", *extra]
        if headless:
            argv += ["--prompt-text", "do it"]
        with contextlib.ExitStack() as stack:
            self._common_patches(stack, project)
            stack.enter_context(patch.object(cli.config_agents, "sync_credentials"))
            stack.enter_context(
                patch.object(
                    cli.container_cli,
                    "select_runtime",
                    return_value=cli.container_cli.DOCKER,
                )
            )
            stack.enter_context(
                patch.object(cli.container_cli, "ensure_system_started", return_value=0)
            )
            stack.enter_context(
                patch.object(cli.container_cli, "image_exists", return_value=False)
            )
            stack.enter_context(
                patch.object(cli.container_cli, "build_image", return_value=build_rc)
            )
            stack.enter_context(
                patch.object(cli.container_cli, "run", side_effect=fake_run)
            )
            stack.enter_context(patch.object(cli.session, "run", side_effect=fake_run))
            stack.enter_context(contextlib.redirect_stdout(out))
            rc = cli.main([*argv, str(project), "python:3.12-slim"])
        seen["stdout"] = out.getvalue()
        return rc, seen


class ModeTableDryRunTests(_CliHarness):
    """Design Decision 4, one test per cell, through --dry-run."""

    def _jj_project(self) -> Path:
        project = self.root / "jjp"
        project.mkdir()
        (project / ".jj").mkdir()
        (project / ".git").mkdir()
        return project

    def _assert_nothing_created(self, project: Path, before: list[str]) -> None:
        self.assertEqual(_snapshot(project), before)
        self.assertFalse((project.parent / f"{project.name}-workspaces").exists())

    def test_jj_project_defaults_to_isolated_with_generated_bookmark(self) -> None:
        project = self._jj_project()
        before = _snapshot(project)

        out = self.dry_run(project)

        ws = jj_workspace_mod.plan(project, f"sandbox/{SESSION_ID}")
        self.assertIn("Would use VCS mode: isolated", out)
        self.assertIn(f"Would use bookmark: sandbox/{SESSION_ID} (generated)", out)
        self.assertIn("Would start from:", out)
        self.assertIn(f"Would use session directory: {ws.path}", out)
        self._assert_nothing_created(project, before)

    def test_jj_project_explicit_isolated(self) -> None:
        project = self._jj_project()
        out = self.dry_run(project, "--vcs", "isolated", "--branch", "feat")
        self.assertIn("Would use VCS mode: isolated", out)
        self.assertIn("Would use bookmark: feat\n", out)

    def test_jj_project_shared_mounts_in_place_and_warns(self) -> None:
        project = self._jj_project()
        out = self.dry_run(project, "--vcs", "shared")
        self.assertIn("Would use VCS mode: shared", out)
        self.assertIn(vcs_mode.SHARED_WARNING, out)
        self.assertIn(f"source={project},target=/workspace", out)
        self.assertNotIn("/workspace/.jj", out)

    def test_git_only_default_fails_with_both_hints(self) -> None:
        project = self.root / "gitp"
        _make_git_repo(project)
        before = _snapshot(project)
        for extra in ([], ["--dry-run"]):
            with self.subTest(extra=extra):
                with self.assertRaises(SystemExit) as raised:
                    cli.main(
                        [*extra, "--agent", "bash", str(project), "python:3.12-slim"]
                    )
                msg = str(raised.exception)
                self.assertIn("jj git init --colocate", msg)
                self.assertIn("--vcs shared", msg)
                self._assert_nothing_created(project, before)

    def test_git_only_isolated_and_branch_fail_with_the_jj_hint(self) -> None:
        project = self.root / "gitp"
        _make_git_repo(project)
        before = _snapshot(project)
        for flags in (["--vcs", "isolated"], ["--branch", "feat"]):
            for extra in ([], ["--dry-run"]):
                with self.subTest(flags=flags, extra=extra):
                    with self.assertRaises(SystemExit) as raised:
                        cli.main(
                            [
                                *extra,
                                "--agent",
                                "bash",
                                *flags,
                                str(project),
                                "python:3.12-slim",
                            ]
                        )
                    msg = str(raised.exception)
                    self.assertIn("jj git init --colocate", msg)
                    self.assertNotIn("--vcs shared", msg)
                    self._assert_nothing_created(project, before)

    def test_git_only_shared_mounts_in_place(self) -> None:
        project = self.root / "gitp"
        _make_git_repo(project)
        out = self.dry_run(project, "--vcs", "shared")
        self.assertIn("Would use VCS mode: shared", out)
        self.assertIn(f"source={project},target=/workspace", out)

    def test_directory_without_repository_mounts_in_place(self) -> None:
        project = self.root / "plain"
        project.mkdir()
        out = self.dry_run(project)
        self.assertNotIn("VCS mode", out)
        self.assertIn(f"source={project},target=/workspace", out)

    def test_directory_without_repository_rejects_vcs_and_branch(self) -> None:
        project = self.root / "plain"
        project.mkdir()
        before = _snapshot(project)
        for flags in (["--vcs", "isolated"], ["--vcs", "shared"], ["--branch", "feat"]):
            for extra in ([], ["--dry-run"]):
                with self.subTest(flags=flags, extra=extra):
                    with self.assertRaises(SystemExit) as raised:
                        cli.main(
                            [
                                *extra,
                                "--agent",
                                "bash",
                                *flags,
                                str(project),
                                "python:3.12-slim",
                            ]
                        )
                    self.assertIn("neither .jj nor .git", str(raised.exception))
                    self._assert_nothing_created(project, before)

    def test_argument_level_rejections(self) -> None:
        project = self._jj_project()
        cases = [
            (
                ["--agent", "bash", "--vcs", "shared", "--branch", "x"],
                "can't be combined",
            ),
            (["--vcs", "shared"], "--vcs requires --agent"),
            (["--share", "data"], "--share requires --agent"),
            (["--build-only", "--vcs", "isolated"], "--build-only cannot be combined"),
            (
                ["--agent", "bash", "--keep-workspace", "--vcs", "shared"],
                "isolated session",
            ),
            (["--agent", "bash", "--branch-start-at", "@"], "requires --branch"),
        ]
        for flags, message in cases:
            with self.subTest(flags=flags):
                with self.assertRaises(SystemExit) as raised:
                    cli.main([*flags, str(project), "python:3.12-slim"])
                self.assertIn(message, str(raised.exception))

    def test_keep_workspace_without_repository_is_rejected(self) -> None:
        project = self.root / "plain"
        project.mkdir()
        with self.assertRaises(SystemExit) as raised:
            cli.main(
                [
                    "--dry-run",
                    "--agent",
                    "bash",
                    "--keep-workspace",
                    str(project),
                    "x:1",
                ]
            )
        self.assertIn("isolated session", str(raised.exception))


class ModeTableLaunchTests(_CliHarness):
    """The same cells without --dry-run: the real launch picks the same mode."""

    def _mode_of(self, project: Path, *flags: str) -> str:
        class Stop(Exception):
            pass

        seen = {}

        def fake_setup(args, project, plan, runtime, stack):
            seen["plan"] = plan
            raise Stop

        with (
            patch.object(cli, "_setup_vcs", side_effect=fake_setup),
            self.assertRaises(Stop),
        ):
            self.launch(project, *flags)
        return seen["plan"].mode

    def test_selected_modes(self) -> None:
        jjp = self.root / "jjp"
        (jjp / ".jj").mkdir(parents=True)
        gitp = self.root / "gitp"
        _make_git_repo(gitp)
        plain = self.root / "plain"
        plain.mkdir()
        cases = [
            (jjp, [], vcs_mode.ISOLATED),
            (jjp, ["--vcs", "isolated"], vcs_mode.ISOLATED),
            (jjp, ["--vcs", "shared"], vcs_mode.SHARED),
            (gitp, ["--vcs", "shared"], vcs_mode.SHARED),
            (plain, [], vcs_mode.NONE),
        ]
        for project, flags, expected in cases:
            with self.subTest(project=project.name, flags=flags):
                self.assertEqual(self._mode_of(project, *flags), expected)


class IsolatedMountTests(_CliHarness):
    """Task 3.2: an isolated session mounts W and the two private directories."""

    def _mounts(self, out: str) -> list[tuple[str, str, bool]]:
        return [(s, t, bool(ro)) for s, t, ro in MOUNT_RE.findall(out)]

    def test_no_mount_source_lies_in_the_main_repository_metadata(self) -> None:
        for colocated in (True, False):
            for runtime in ("docker", "podman", "apple-container"):
                with self.subTest(colocated=colocated, runtime=runtime):
                    project = self.root / f"p-{colocated}-{runtime}"
                    (project / ".jj").mkdir(parents=True)
                    if colocated:
                        (project / ".git").mkdir()
                    out = self.dry_run(project, "--branch", "feat", runtime=runtime)
                    ws = jj_workspace_mod.plan(project, "feat")
                    mounts = self._mounts(out)
                    by_target = {t: s for s, t, _ in mounts}
                    self.assertEqual(by_target["/workspace"], str(ws.path))
                    private = Path(by_target["/workspace/.jj"]).parent
                    self.assertEqual(private.parent, ws.path.parent / ".session-vcs")
                    self.assertEqual(by_target["/workspace/.jj"], str(private / "jj"))
                    self.assertEqual(by_target["/workspace/.git"], str(private / "git"))
                    for source, _target, _ro in mounts:
                        for meta in (
                            project / ".jj",
                            project / ".git",
                            ws.path / ".jj",
                        ):
                            path = Path(source)
                            self.assertFalse(
                                path == meta or meta in path.parents,
                                f"{source} exposes {meta}",
                            )
                    targets = [t for _, t, _ in mounts]
                    self.assertLess(
                        targets.index("/workspace"), targets.index("/workspace/.jj")
                    )

    def test_conflicting_extra_mounts_are_refused(self) -> None:
        project = self.root / "p"
        (project / ".jj").mkdir(parents=True)
        (project / ".git" / "hooks").mkdir(parents=True)
        ws = jj_workspace_mod.plan(project, "feat")
        cases = [
            (
                f"type=bind,source={project / '.jj'},target=/x",
                "version-control metadata",
            ),
            (
                f"type=bind,source={project / '.git' / 'hooks'},target=/x",
                "version-control metadata",
            ),
            (
                f"type=bind,source={ws.private.parent},target=/x",
                "version-control metadata",
            ),
            (
                f"type=bind,source={self.root},target=/workspace/.git/hooks",
                "/workspace/.git",
            ),
            (f"type=bind,source={self.root},target=/workspace/.jj", "/workspace/.jj"),
        ]
        for mount, message in cases:
            with self.subTest(mount=mount):
                with self.assertRaises(SystemExit) as raised:
                    self.dry_run(project, "--branch", "feat", "--mount", mount)
                self.assertIn(message, str(raised.exception))

    def test_shared_mode_has_no_private_mounts(self) -> None:
        project = self.root / "p"
        (project / ".jj").mkdir(parents=True)
        out = self.dry_run(project, "--vcs", "shared")
        targets = [t for _, t, _ in self._mounts(out)]
        self.assertNotIn("/workspace/.jj", targets)
        self.assertNotIn("/workspace/.git", targets)


class ShareTests(_CliHarness):
    """Design Decision 7 and the two sharing requirements."""

    def _project(self) -> Path:
        project = self.root / "p"
        (project / ".jj").mkdir(parents=True)
        (project / "data").mkdir()
        (project / "out").mkdir()
        (project / "settings.ini").write_text("x\n", encoding="utf-8")
        return project

    def test_read_only_and_writable_shares(self) -> None:
        project = self._project()
        out = self.dry_run(project, "--share", "data", "--share", "out:rw")
        mounts = {t: (s, ro) for s, t, ro in MOUNT_RE.findall(out)}
        self.assertEqual(
            mounts["/workspace/data"], (str(project / "data"), ",readonly")
        )
        self.assertEqual(mounts["/workspace/out"], (str(project / "out"), ""))
        self.assertIn("Writable shares", out)
        self.assertIn(": out", out)
        targets = [t for _, t, _ in MOUNT_RE.findall(out)]
        self.assertLess(
            targets.index("/workspace/.git"), targets.index("/workspace/data")
        )
        self.assertLess(
            targets.index("/workspace/out"),
            targets.index("/workspace/.project-sandbox"),
        )

    def test_unsafe_paths_are_refused(self) -> None:
        project = self._project()
        outside = self.root / "outside"
        outside.mkdir()
        (project / "escape").symlink_to(outside)
        cases = [
            (".git", ".git can't be shared"),
            (".jj/repo", ".jj can't be shared"),
            (".devcontainer", ".devcontainer can't be shared"),
            (".claude", ".claude/settings.json can't be shared"),
            ("escape", "resolves outside the project"),
            ("settings.ini", "only directories can be shared"),
            ("../x", "must not contain '..'"),
            (str(project / "data"), "relative to the project"),
            ("missing", "does not exist"),
            ("data,readonly", "comma"),
        ]
        for path, message in cases:
            with self.subTest(path=path):
                with self.assertRaises(SystemExit) as raised:
                    self.dry_run(project, "--share", path)
                self.assertIn(message, str(raised.exception))

    def test_shared_mode_accepts_share_without_effect(self) -> None:
        project = self._project()
        out = self.dry_run(project, "--vcs", "shared", "--share", "data")
        self.assertIn("--share has no effect in shared mode.", out)
        self.assertNotIn("/workspace/data", out)

    def test_directory_without_repository_accepts_share_without_effect(self) -> None:
        project = self.root / "plain"
        (project / "data").mkdir(parents=True)
        out = self.dry_run(project, "--share", "data")
        self.assertIn("--share has no effect without a repository.", out)


@unittest.skipUnless(JJ, "jj not installed")
class IsolatedSessionLaunchTests(_CliHarness):
    """Whole launches with real jj and a mocked runtime."""

    def setUp(self) -> None:
        super().setUp()
        if not jj_workspace_mod._supports_no_colocate():
            self.skipTest("requires jj >= 0.46")
        self.project = self.root / "repo"
        _make_jj_repo(self.project)

    def test_result_is_one_commit_on_the_bookmark(self) -> None:
        def agent(workspace: Path) -> None:
            (workspace / "a.txt").write_text("agent\n", encoding="utf-8")
            (workspace / "new.txt").write_text("new\n", encoding="utf-8")
            (workspace / "src" / "main.py").unlink()

        rc, seen = self.launch(self.project, "--branch", "feat", agent=agent)

        self.assertEqual(rc, 0)
        self.assertEqual(
            sorted(_jj(self.project, "diff", "--summary", "-r", "feat").splitlines()),
            ["A new.txt", "D src/main.py", "M a.txt"],
        )
        ws = jj_workspace_mod.plan(self.project, "feat")
        self.assertFalse(ws.path.exists())
        self.assertFalse(ws.private.exists())
        self.assertIn("Session result: bookmark 'feat'", seen["stdout"])

    def test_generated_bookmark_is_printed_and_used(self) -> None:
        def agent(workspace: Path) -> None:
            (workspace / "a.txt").write_text("agent\n", encoding="utf-8")

        rc, seen = self.launch(self.project, agent=agent)

        bookmark = f"sandbox/{SESSION_ID}"
        self.assertEqual(rc, 0)
        self.assertIn(f"Bookmark: {bookmark} (generated)", seen["stdout_at_start"])
        self.assertIn(f"Session result: bookmark '{bookmark}'", seen["stdout"])
        self.assertEqual(
            _jj(self.project, "file", "show", "-r", bookmark, "root:a.txt"), "agent\n"
        )

    def test_shares_add_nothing_to_the_result_commit(self) -> None:
        (self.project / "data").mkdir()
        (self.project / "data" / "in.csv").write_text("1\n", encoding="utf-8")
        (self.project / "out").mkdir()

        def agent(workspace: Path) -> None:
            # The real shares are separate mounts; the agent's writes through
            # the writable one land in the project, not in W.
            (self.project / "out" / "report.txt").write_text("r\n", encoding="utf-8")
            (workspace / "a.txt").write_text("agent\n", encoding="utf-8")

        rc, seen = self.launch(
            self.project,
            "--branch",
            "feat",
            "--share",
            "data",
            "--share",
            "out:rw",
            agent=agent,
        )

        self.assertEqual(rc, 0)
        self.assertEqual(seen["mounts"]["/workspace/data"], str(self.project / "data"))
        self.assertEqual(
            _jj(self.project, "diff", "--summary", "-r", "feat").splitlines(),
            ["M a.txt"],
        )
        self.assertTrue((self.project / "out" / "report.txt").is_file())
        self.assertIn("Writable shares", seen["stdout_at_start"])

    def test_tracked_share_fails_and_leaves_no_session_directory(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            self.launch(self.project, "--branch", "feat", "--share", "src")
        self.assertIn("already contains src", str(raised.exception))
        ws = jj_workspace_mod.plan(self.project, "feat")
        self.assertFalse(ws.path.exists())
        self.assertFalse(ws.private.exists())
        self.assertFalse(jj_workspace_mod._bookmark_exists(self.project, "feat"))

    def test_failed_session_keeps_directory_and_prints_its_path(self) -> None:
        def agent(workspace: Path) -> None:
            (workspace / "a.txt").write_text("partial\n", encoding="utf-8")

        rc, seen = self.launch(
            self.project, "--branch", "feat", agent=agent, exit_code=3
        )

        ws = jj_workspace_mod.plan(self.project, "feat")
        self.assertEqual(rc, 3)
        self.assertTrue(ws.path.is_dir())
        self.assertIn(str(ws.path), seen["stdout"])
        self.assertEqual(
            _jj(self.project, "file", "show", "-r", "feat", "root:a.txt"), "partial\n"
        )

    def test_launcher_contract_for_project_sandbox_tui(self) -> None:
        """The summary fields and session record the TUI reads (task 5.1)."""
        log = self.root / "run.log"
        summary = self.root / "run.json"

        def agent(workspace: Path) -> None:
            (workspace / "a.txt").write_text("agent\n", encoding="utf-8")

        rc, _ = self.launch(
            self.project,
            "--branch=feat",
            f"--log={log}",
            f"--json-summary={summary}",
            agent=agent,
            headless=True,
        )

        self.assertEqual(rc, 0)
        payload = json.loads(summary.read_text(encoding="utf-8"))
        ws = jj_workspace_mod.plan(self.project, "feat")
        self.assertEqual(payload["session_id"], SESSION_ID)
        self.assertEqual(payload["exit_code"], 0)
        self.assertEqual(payload["bookmark"], "feat")
        self.assertEqual(payload["workspace_path"], str(ws.path))
        self.assertEqual(
            payload["jj_change_id"],
            _jj(self.project, "log", "--no-graph", "-r", "feat", "-T", "change_id"),
        )
        record = json.loads(
            (
                self.state / "project-sandbox" / "sessions" / f"{SESSION_ID}.json"
            ).read_text()
        )
        self.assertEqual(record["pid"], os.getpid())
        self.assertEqual(record["exit_code"], 0)
        self.assertEqual(record["workspace_path"], str(ws.path))


class LeaseTests(_CliHarness):
    """Task 4.1: one isolated session per bookmark, one shared per repository."""

    def _args(self, **overrides) -> argparse.Namespace:
        values = {
            "branch_start_at": None,
            "worktree_dir": None,
            "keep_workspace": False,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def _isolated(self, project: Path) -> vcs_mode.VcsPlan:
        ws = jj_workspace_mod.plan(project, "feat")
        return vcs_mode.VcsPlan(mode=vcs_mode.ISOLATED, workspace=ws.path, ws=ws)

    def _write_record(self, workspace: Path, **fields) -> None:
        sessions = self.state / "project-sandbox" / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        record = {
            "session_id": "OTHER",
            "workspace_path": str(workspace),
            "status": "running",
            "pid": os.getpid(),
            "container_name": "project-sandbox-other",
            "runtime": "docker",
        }
        record.update(fields)
        (sessions / f"{record['session_id']}.json").write_text(json.dumps(record))

    def _setup(self, project: Path, plan, stack) -> object:
        with (
            patch.object(
                cli.jj_workspace_mod, "setup", side_effect=lambda **kw: plan.ws
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return cli._setup_vcs(
                self._args(), project, plan, cli.container_cli.DOCKER, stack
            )

    def test_second_session_on_one_bookmark_is_refused(self) -> None:
        project = self.root / "p"
        project.mkdir()
        plan = self._isolated(project)
        self._write_record(plan.ws.path)
        with (
            host_locks.path_lock("workspace-lease", plan.ws.path, blocking=False),
            contextlib.ExitStack() as stack,
            self.assertRaises(SystemExit) as raised,
        ):
            self._setup(project, plan, stack)
        self.assertIn("bookmark 'feat'", str(raised.exception))
        self.assertIn("OTHER", str(raised.exception))

    def test_second_shared_session_is_refused(self) -> None:
        project = self.root / "p"
        project.mkdir()
        plan = vcs_mode.VcsPlan(mode=vcs_mode.SHARED, workspace=project)
        self._write_record(project)
        with (
            host_locks.path_lock("shared-vcs", project, blocking=False),
            contextlib.ExitStack() as stack,
            self.assertRaises(SystemExit) as raised,
        ):
            self._setup(project, plan, stack)
        self.assertIn("another shared session", str(raised.exception))
        self.assertIn("OTHER", str(raised.exception))

    def test_isolated_session_runs_beside_a_shared_one(self) -> None:
        project = self.root / "p"
        project.mkdir()
        plan = self._isolated(project)
        with (
            host_locks.path_lock("shared-vcs", project, blocking=False),
            contextlib.ExitStack() as stack,
        ):
            self.assertIs(self._setup(project, plan, stack), plan.ws)

    def test_killed_cli_with_live_container_is_refused(self) -> None:
        project = self.root / "p"
        project.mkdir()
        plan = self._isolated(project)
        self._write_record(plan.ws.path, pid=2**22 + 12345)
        with (
            patch.object(
                cli.container_cli, "container_exists", return_value=True
            ) as alive,
            contextlib.ExitStack() as stack,
            self.assertRaises(SystemExit) as raised,
        ):
            self._setup(project, plan, stack)
        self.assertIn("still runs", str(raised.exception))
        self.assertEqual(alive.call_args.args[1], "project-sandbox-other")
        # The refusal released the lease again.
        with host_locks.path_lock("workspace-lease", plan.ws.path, blocking=False):
            pass

    def test_finished_records_and_dead_containers_do_not_block(self) -> None:
        project = self.root / "p"
        project.mkdir()
        plan = self._isolated(project)
        self._write_record(plan.ws.path, status="completed")
        self._write_record(plan.ws.path, session_id="GONE", pid=2**22 + 1)
        with (
            patch.object(cli.container_cli, "container_exists", return_value=False),
            contextlib.ExitStack() as stack,
        ):
            self.assertIs(self._setup(project, plan, stack), plan.ws)

    def test_lease_is_released_on_every_finalize_path(self) -> None:
        project = self.root / "p"
        (project / ".jj").mkdir(parents=True)
        ws = jj_workspace_mod.plan(project, "feat")
        outcomes = [
            {"exit_code": 0},
            {"exit_code": 1},
            {"build_rc": 1},
            {"run_error": KeyboardInterrupt()},
        ]
        for outcome in outcomes:
            with self.subTest(**{k: repr(v) for k, v in outcome.items()}):
                with (
                    patch.object(cli.jj_workspace_mod, "setup", return_value=ws),
                    patch.object(cli.jj_workspace_mod, "finalize"),
                    patch.object(cli.jj_workspace_mod, "remove"),
                    contextlib.suppress(KeyboardInterrupt),
                ):
                    self.launch(project, "--branch", "feat", **outcome)
                with host_locks.path_lock("workspace-lease", ws.path, blocking=False):
                    pass


class SharedWarningTests(_CliHarness):
    """Task 4.2: every shared launch warns before the container starts."""

    def test_warning_precedes_container_start(self) -> None:
        project = self.root / "p"
        (project / ".jj").mkdir(parents=True)
        rc, seen = self.launch(project, "--vcs", "shared")
        self.assertEqual(rc, 0)
        self.assertIn(vcs_mode.SHARED_WARNING, seen["stdout_at_start"])
        self.assertEqual(seen["mounts"]["/workspace"], str(project))

    def test_isolated_launch_does_not_warn(self) -> None:
        project = self.root / "p"
        (project / ".jj").mkdir(parents=True)
        ws = jj_workspace_mod.plan(project, "feat")
        with (
            patch.object(cli.jj_workspace_mod, "setup", return_value=ws),
            patch.object(cli.jj_workspace_mod, "finalize"),
        ):
            _, seen = self.launch(project, "--branch", "feat")
        self.assertNotIn(vcs_mode.SHARED_WARNING, seen["stdout"])


if __name__ == "__main__":
    unittest.main()


class NewDirectoryWaitTests(TestCase):
    """Fresh mount sources wait out the macOS VM metadata cache (e2e regression:
    a bookmark reused right after its directories were removed failed with
    "bind source path does not exist" on Docker Desktop)."""

    def _ws(self, created_at):
        ws = jj_workspace_mod.plan(Path("/tmp/fake/repo"), "b")
        ws.created_at = created_at
        return ws

    def test_waits_for_a_container_runtime_on_macos(self) -> None:
        with (
            patch.object(cli.sys, "platform", "darwin"),
            patch.object(
                cli.jj_workspace_mod, "wait_for_new_directories", return_value=1.25
            ) as wait,
        ):
            waited = cli._wait_for_new_directories(
                self._ws(100.0), cli.container_cli.DOCKER
            )
        self.assertEqual(waited, 1.25)
        wait.assert_called_once()

    def test_no_wait_on_linux_for_chroot_or_in_place_sessions(self) -> None:
        cases = [
            ("linux", self._ws(100.0), cli.container_cli.DOCKER),
            ("darwin", self._ws(100.0), cli.container_cli.CHROOT),
            ("darwin", None, cli.container_cli.DOCKER),
        ]
        for platform, ws, runtime in cases:
            with self.subTest(
                platform=platform, runtime=runtime.name, ws=ws is not None
            ):
                with (
                    patch.object(cli.sys, "platform", platform),
                    patch.object(
                        cli.jj_workspace_mod, "wait_for_new_directories"
                    ) as wait,
                ):
                    self.assertEqual(cli._wait_for_new_directories(ws, runtime), 0.0)
                wait.assert_not_called()

    def test_wait_covers_only_the_rest_of_the_window(self) -> None:
        with (
            patch.object(jj_workspace_mod.time, "monotonic", return_value=101.0),
            patch.object(jj_workspace_mod.time, "sleep") as sleep,
        ):
            self.assertEqual(
                jj_workspace_mod.wait_for_new_directories(self._ws(None)), 0.0
            )
            self.assertEqual(
                jj_workspace_mod.wait_for_new_directories(self._ws(99.0)), 0.0
            )
            waited = jj_workspace_mod.wait_for_new_directories(self._ws(100.0))
        self.assertAlmostEqual(waited, jj_workspace_mod.METADATA_CACHE_WINDOW - 1.0)
        sleep.assert_called_once_with(waited)
