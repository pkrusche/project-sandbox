import importlib.util
import sys
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "update-pins.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("update_pins", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclass resolution works under
    # `from __future__ import annotations`.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


update_pins = _load_module()


class UvImagePinTests(TestCase):
    def test_regex_matches_contiguous_form(self) -> None:
        text = (
            "COPY --from=ghcr.io/astral-sh/uv:0.11.23@sha256:"
            + "a" * 64
            + " /uv /usr/local/bin/uv"
        )
        match = update_pins.UV_IMAGE_RE.search(text)
        self.assertIsNotNone(match)
        self.assertEqual(match.group("version"), "0.11.23")
        self.assertEqual(match.group("digest"), "a" * 64)
        self.assertIsNone(match.group("sep"))

    def test_regex_matches_split_string_literal_form(self) -> None:
        # dockerfile.py splits the tag and digest across adjacent Python string
        # literals; the pin must still be recognised there.
        text = (
            '        "FROM ghcr.io/astral-sh/uv:0.11.23"\n'
            '        "@sha256:' + "b" * 64 + '"\n'
            '        " AS uv-bin",'
        )
        match = update_pins.UV_IMAGE_RE.search(text)
        self.assertIsNotNone(match)
        self.assertEqual(match.group("version"), "0.11.23")
        self.assertEqual(match.group("digest"), "b" * 64)
        self.assertIsNotNone(match.group("sep"))

    def test_substitution_preserves_split_literal_layout(self) -> None:
        text = (
            '        "FROM ghcr.io/astral-sh/uv:0.11.23"\n'
            '        "@sha256:' + "b" * 64 + '"\n'
            '        " AS uv-bin",'
        )
        new_version, new_digest = "0.99.0", "c" * 64

        def repl(match):
            sep = match.group("sep") or ""
            return f"ghcr.io/astral-sh/uv:{new_version}{sep}@sha256:{new_digest}"

        updated = update_pins.UV_IMAGE_RE.sub(repl, text)
        # The two-literal layout (version on one line, digest on the next) is
        # preserved so the helper remains readable and diffs stay minimal.
        self.assertIn(f'"FROM ghcr.io/astral-sh/uv:{new_version}"', updated)
        self.assertIn(f'"@sha256:{new_digest}"', updated)
        self.assertIn('" AS uv-bin"', updated)

    def test_pin_is_found_in_both_dockerfile_and_helper(self) -> None:
        # Regression: the uv pin lives in both the committed Dockerfile and the
        # dockerfile.py helper that generates it. Both must be matched so they
        # stay in sync; previously the helper's split form was silently skipped.
        for path in (update_pins.DOCKERFILE, update_pins.DOCKERFILE_HELPER):
            with self.subTest(path=path.name):
                matches = list(update_pins.UV_IMAGE_RE.finditer(path.read_text()))
                self.assertEqual(len(matches), 1, f"no uv pin matched in {path}")

    def test_dockerfile_and_helper_pins_agree(self) -> None:
        dockerfile_match = update_pins.UV_IMAGE_RE.search(
            update_pins.DOCKERFILE.read_text()
        )
        helper_match = update_pins.UV_IMAGE_RE.search(
            update_pins.DOCKERFILE_HELPER.read_text()
        )
        self.assertEqual(
            (dockerfile_match.group("version"), dockerfile_match.group("digest")),
            (helper_match.group("version"), helper_match.group("digest")),
        )
