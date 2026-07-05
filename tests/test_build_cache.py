import sys
import tempfile
from pathlib import Path
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_sandbox import build_cache


def _seed_context(context_dir: Path) -> None:
    (context_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (context_dir / "entrypoint.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (context_dir / "init-firewall.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (context_dir / "project-sandbox-devcontainer-init").write_text(
        "#!/bin/sh\n", encoding="utf-8"
    )


class BuildCacheTests(TestCase):
    def test_fingerprint_is_stable_for_unchanged_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            _seed_context(context)
            extra = {"image_tag": "img:latest", "base_image": "python:3.12-slim"}
            first = build_cache.compute_fingerprint(context, extra=extra)
            second = build_cache.compute_fingerprint(context, extra=extra)
            self.assertEqual(first, second)

    def test_fingerprint_changes_when_a_build_input_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            _seed_context(context)
            extra = {"image_tag": "img:latest", "base_image": "python:3.12-slim"}
            before = build_cache.compute_fingerprint(context, extra=extra)
            (context / "entrypoint.sh").write_text(
                "#!/bin/sh\nexit 0\n", encoding="utf-8"
            )
            after = build_cache.compute_fingerprint(context, extra=extra)
            self.assertNotEqual(before, after)

    def test_fingerprint_changes_when_base_image_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            _seed_context(context)
            base = build_cache.compute_fingerprint(
                context, extra={"image_tag": "img:latest", "base_image": "a"}
            )
            other = build_cache.compute_fingerprint(
                context, extra={"image_tag": "img:latest", "base_image": "b"}
            )
            self.assertNotEqual(base, other)

    def test_state_round_trip_and_cache_validity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            _seed_context(context)
            self.assertIsNone(build_cache.read_state(context))
            build_cache.write_state(context, image_tag="img:latest", fingerprint="abc")
            self.assertTrue(
                build_cache.is_cache_valid(
                    context, image_tag="img:latest", fingerprint="abc"
                )
            )

    def test_cache_invalid_on_tag_or_fingerprint_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            build_cache.write_state(context, image_tag="img:latest", fingerprint="abc")
            self.assertFalse(
                build_cache.is_cache_valid(
                    context, image_tag="other:latest", fingerprint="abc"
                )
            )
            self.assertFalse(
                build_cache.is_cache_valid(
                    context, image_tag="img:latest", fingerprint="zzz"
                )
            )

    def test_read_state_returns_none_on_corrupt_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            build_cache.state_path(context).write_text("{not json", encoding="utf-8")
            self.assertIsNone(build_cache.read_state(context))
