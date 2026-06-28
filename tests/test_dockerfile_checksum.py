import tempfile
import unittest
from pathlib import Path

from project_sandbox import dockerfile_checksum


class DockerfileChecksumTests(unittest.TestCase):
    def _project(self, tmp: str) -> tuple[Path, Path]:
        project = Path(tmp)
        context_dir = project / ".project-sandbox"
        context_dir.mkdir()
        return project, context_dir

    def test_no_recorded_baseline_yields_no_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, context_dir = self._project(tmp)
            dockerfile = project / "Dockerfile"
            dockerfile.write_text("FROM debian\n", encoding="utf-8")
            # Nothing recorded yet: a first encounter is never flagged.
            self.assertEqual(
                dockerfile_checksum.changed_warnings(context_dir, [dockerfile]), []
            )

    def test_unchanged_dockerfile_yields_no_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, context_dir = self._project(tmp)
            dockerfile = project / "Dockerfile"
            dockerfile.write_text("FROM debian\n", encoding="utf-8")
            dockerfile_checksum.record(context_dir, [dockerfile])
            self.assertEqual(
                dockerfile_checksum.changed_warnings(context_dir, [dockerfile]), []
            )

    def test_changed_dockerfile_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, context_dir = self._project(tmp)
            dockerfile = project / "Dockerfile"
            dockerfile.write_text("FROM debian\n", encoding="utf-8")
            dockerfile_checksum.record(context_dir, [dockerfile])

            dockerfile.write_text("FROM debian\nRUN echo pwned\n", encoding="utf-8")
            warnings = dockerfile_checksum.changed_warnings(context_dir, [dockerfile])
            self.assertEqual(len(warnings), 1)
            self.assertIn(str(dockerfile), warnings[0])
            self.assertIn("changed since it was last built", warnings[0])

    def test_record_persists_under_project_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, context_dir = self._project(tmp)
            dockerfile = project / "Dockerfile"
            dockerfile.write_text("FROM debian\n", encoding="utf-8")
            dockerfile_checksum.record(context_dir, [dockerfile])

            state = dockerfile_checksum.state_path(context_dir)
            self.assertEqual(state.parent, context_dir)
            self.assertTrue(state.is_file())

    def test_corrupt_state_degrades_to_no_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, context_dir = self._project(tmp)
            dockerfile = project / "Dockerfile"
            dockerfile.write_text("FROM debian\n", encoding="utf-8")
            dockerfile_checksum.state_path(context_dir).write_text(
                "{not json", encoding="utf-8"
            )
            self.assertEqual(
                dockerfile_checksum.changed_warnings(context_dir, [dockerfile]), []
            )

    def test_missing_dockerfile_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, context_dir = self._project(tmp)
            absent = context_dir.parent / "Dockerfile"
            # No file on disk: neither recording nor checking raises.
            dockerfile_checksum.record(context_dir, [absent])
            self.assertEqual(
                dockerfile_checksum.changed_warnings(context_dir, [absent]), []
            )


if __name__ == "__main__":
    unittest.main()
