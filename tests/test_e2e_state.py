"""run-e2e-tests.sh --incremental and its state key helper scripts/e2e-state.py."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run-e2e-tests.sh"

_spec = importlib.util.spec_from_file_location(
    "e2e_state", ROOT / "scripts" / "e2e-state.py"
)
assert _spec and _spec.loader
e2e_state = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(e2e_state)


def _env_without_override() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("PROJECT_SANDBOX_E2E_SOURCE_FINGERPRINT", None)
    return env


class GitFingerprintTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git not installed")
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }
        for argv in (
            ["git", "init", "-q"],
            ["git", "config", "commit.gpgsign", "false"],
        ):
            subprocess.run(argv, cwd=self.repo, check=True, env=env)
        (self.repo / ".gitignore").write_text("ignored/\n")
        (self.repo / "a.txt").write_text("one\n")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True, env=env)
        subprocess.run(
            ["git", "commit", "-q", "-m", "init"], cwd=self.repo, check=True, env=env
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_tracks_working_tree_edits_but_not_ignored_files(self) -> None:
        clean = e2e_state.git_fingerprint(self.repo)
        self.assertIsNotNone(clean)
        self.assertEqual(clean, e2e_state.git_fingerprint(self.repo))

        (self.repo / "ignored").mkdir()
        (self.repo / "ignored" / "x").write_text("noise\n")
        self.assertEqual(clean, e2e_state.git_fingerprint(self.repo))

        (self.repo / "a.txt").write_text("two\n")
        edited = e2e_state.git_fingerprint(self.repo)
        self.assertNotEqual(clean, edited)

        (self.repo / "new.txt").write_text("untracked\n")
        self.assertNotEqual(edited, e2e_state.git_fingerprint(self.repo))

        (self.repo / "new.txt").unlink()
        (self.repo / "a.txt").write_text("one\n")
        self.assertEqual(clean, e2e_state.git_fingerprint(self.repo))

    def test_leaves_the_real_index_untouched(self) -> None:
        (self.repo / "new.txt").write_text("untracked\n")
        e2e_state.git_fingerprint(self.repo)
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertIn("?? new.txt", status)


class JjFingerprintTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("jj") is None:
            self.skipTest("jj not installed")
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "repo"
        env = {**os.environ, "JJ_USER": "t", "JJ_EMAIL": "t@example.invalid"}
        subprocess.run(
            ["jj", "git", "init", "--quiet", str(self.repo)],
            check=True,
            env=env,
            capture_output=True,
        )
        (self.repo / ".gitignore").write_text("/.e2e-state/\n")
        (self.repo / "a.txt").write_text("one\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_tracks_working_copy_but_not_the_state_dir(self) -> None:
        before = e2e_state.jj_fingerprint(self.repo)
        self.assertIsNotNone(before)
        assert before is not None
        self.assertTrue(before.startswith("jj-"))

        (self.repo / ".e2e-state").mkdir()
        (self.repo / ".e2e-state" / "smoke.pass").write_text("passed\n")
        self.assertEqual(before, e2e_state.jj_fingerprint(self.repo))

        (self.repo / "a.txt").write_text("two\n")
        self.assertNotEqual(before, e2e_state.jj_fingerprint(self.repo))

    def test_describing_the_change_keeps_the_fingerprint(self) -> None:
        before = e2e_state.jj_fingerprint(self.repo)
        subprocess.run(
            ["jj", "describe", "-m", "renamed"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        self.assertEqual(before, e2e_state.jj_fingerprint(self.repo))


class StateKeyTests(unittest.TestCase):
    def _key(
        self, *args: str, fingerprint: str = "src1"
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["python3", "-I", str(ROOT / "scripts" / "e2e-state.py"), *args],
            env={**os.environ, "PROJECT_SANDBOX_E2E_SOURCE_FINGERPRINT": fingerprint},
            capture_output=True,
            text=True,
            check=False,
        )

    def test_key_separates_source_runtime_and_settings(self) -> None:
        base = self._key("--root", ".", "--runtime", "docker", "BASE_IMAGE=a")
        self.assertEqual(base.returncode, 0, base.stderr)
        source, runtime_cfg = base.stdout.strip().split("/")
        self.assertEqual(source, "src1")
        self.assertTrue(runtime_cfg.startswith("docker-"))
        for args, fingerprint in (
            (("--runtime", "podman", "BASE_IMAGE=a"), "src1"),
            (("--runtime", "docker", "BASE_IMAGE=b"), "src1"),
            (("--runtime", "docker", "BASE_IMAGE=a"), "src2"),
        ):
            with self.subTest(args=args, fingerprint=fingerprint):
                other = self._key("--root", ".", *args, fingerprint=fingerprint)
                self.assertNotEqual(base.stdout, other.stdout)

    def test_rejects_unsafe_override_and_runtime(self) -> None:
        self.assertNotEqual(
            self._key(
                "--root", ".", "--runtime", "docker", fingerprint="../x"
            ).returncode,
            0,
        )
        self.assertNotEqual(
            self._key("--root", ".", "--runtime", "../docker").returncode, 0
        )

    def test_fails_outside_a_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [
                    "python3",
                    "-I",
                    str(ROOT / "scripts" / "e2e-state.py"),
                    "--root",
                    tmp,
                    "--runtime",
                    "docker",
                ],
                env=_env_without_override(),
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("neither a jj workspace nor a git checkout", proc.stderr)


class RunnerIncrementalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _plan(self, *args: str, fingerprint: str = "src1") -> str:
        proc = subprocess.run(
            [str(RUNNER), "--plan", "--runtime", "chroot", "--only", "smoke", *args],
            env={
                **os.environ,
                "PROJECT_SANDBOX_E2E_STATE_DIR": str(self.state),
                "PROJECT_SANDBOX_E2E_SOURCE_FINGERPRINT": fingerprint,
            },
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout

    def _mark_passed(self) -> None:
        out = self._plan("--incremental")
        line = next(l for l in out.splitlines() if l.startswith("Incremental: results"))
        state_dir = Path(line.rsplit(" in ", 1)[1])
        self.assertTrue(state_dir.is_relative_to(self.state))
        state_dir.mkdir(parents=True)
        (state_dir / "smoke.pass").write_text("passed\n")

    def test_suite_runs_until_it_has_passed_on_this_source(self) -> None:
        self.assertRegex(self._plan("--incremental"), r"RUN\s+smoke")
        self._mark_passed()
        out = self._plan("--incremental")
        self.assertIn("smoke                      PASS (cached)", out)
        self.assertIn("PASS smoke  (incremental: passed earlier on this source)", out)
        self.assertIn("Incremental: 1 suite(s) already passed", out)

    def test_other_source_or_configuration_reruns(self) -> None:
        self._mark_passed()
        self.assertRegex(
            self._plan("--incremental", fingerprint="src2"), r"RUN\s+smoke"
        )
        self.assertRegex(
            self._plan("--incremental", "--base-image", "other:1"), r"RUN\s+smoke"
        )

    def test_selection_flags_do_not_change_the_key(self) -> None:
        self._mark_passed()
        self.assertIn("PASS (cached)", self._plan("--incremental", "--keep"))

    def test_without_incremental_state_is_ignored(self) -> None:
        self._mark_passed()
        out = self._plan()
        self.assertRegex(out, r"RUN\s+smoke")
        self.assertNotIn("Incremental", out)

    def test_plan_never_writes_state(self) -> None:
        self._plan("--incremental")
        self.assertEqual(list(self.state.iterdir()), [])


class StateDirTests(unittest.TestCase):
    def test_state_dir_is_gitignored(self) -> None:
        self.assertIn("/.e2e-state/", (ROOT / ".gitignore").read_text().splitlines())


if __name__ == "__main__":
    unittest.main()
