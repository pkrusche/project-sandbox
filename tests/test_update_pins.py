import importlib.util
import io
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "update-pins.py"

sys.path.insert(0, str(ROOT / "src"))

from project_sandbox import config_agents


def _load_module():
    spec = importlib.util.spec_from_file_location("update_pins", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclass resolution works under
    # `from __future__ import annotations`.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


update_pins = _load_module()


class PypiProgressTests(TestCase):
    def test_latest_version_reports_package_before_request(self) -> None:
        output = io.StringIO()

        def fake_request_json(url: str) -> object:
            self.assertEqual(
                output.getvalue(), "Checking PyPI for example-package...\n"
            )
            return {
                "info": {"version": "2.0.0"},
                "releases": {
                    "2.0.0": [{"upload_time_iso_8601": "2020-01-01T00:00:00Z"}]
                },
            }

        with (
            patch.object(update_pins, "request_json", side_effect=fake_request_json),
            redirect_stdout(output),
        ):
            version = update_pins.latest_pypi_version("example-package")

        self.assertEqual(version, "2.0.0")


class ReleaseAgeTests(TestCase):
    def test_release_younger_than_minimum_age_is_skipped(self) -> None:
        release = update_pins.UpstreamRelease(
            "2.0.0", datetime.now(UTC) - timedelta(hours=23)
        )
        with redirect_stdout(io.StringIO()):
            eligible = update_pins.release_is_old_enough(
                release, timedelta(hours=24), source="test"
            )
        self.assertFalse(eligible)

    def test_release_at_least_minimum_age_is_eligible(self) -> None:
        release = update_pins.UpstreamRelease(
            "2.0.0", datetime.now(UTC) - timedelta(hours=25)
        )
        self.assertTrue(
            update_pins.release_is_old_enough(
                release, timedelta(hours=24), source="test"
            )
        )

    def test_selects_latest_release_that_is_old_enough(self) -> None:
        releases = [
            update_pins.UpstreamRelease("3.0.0", datetime.now(UTC) - timedelta(days=2)),
            update_pins.UpstreamRelease("2.0.0", datetime.now(UTC) - timedelta(days=3)),
            update_pins.UpstreamRelease(
                "5.0.0", datetime.now(UTC) - timedelta(hours=2)
            ),
            update_pins.UpstreamRelease(
                "4.0.0-alpha.1", datetime.now(UTC) - timedelta(hours=1)
            ),
        ]
        output = io.StringIO()
        with redirect_stdout(output):
            selected = update_pins.latest_old_enough_release(
                releases, timedelta(hours=24), source="test"
            )
        self.assertIsNotNone(selected)
        self.assertEqual(selected.version, "3.0.0")
        self.assertIn("5.0.0 is newer", output.getvalue())
        self.assertNotIn("alpha", output.getvalue())

    def test_prerelease_and_dev_versions_are_not_stable(self) -> None:
        self.assertIsNone(update_pins.version_key("1.2.3-alpha.1"))
        self.assertIsNone(update_pins.version_key("1.2.3-dev-202609161748"))
        self.assertEqual(update_pins.version_key("1.2.3"), (0, (1, 2, 3), 0))
        self.assertGreater(
            update_pins.comparable_version_key("1.2.3-alpha.1"),
            update_pins.comparable_version_key("1.2.2"),
        )

    def test_returns_no_release_when_all_releases_are_too_new(self) -> None:
        releases = [
            update_pins.UpstreamRelease("3.0.0", datetime.now(UTC) - timedelta(hours=2))
        ]
        output = io.StringIO()
        with redirect_stdout(output):
            selected = update_pins.latest_old_enough_release(
                releases, timedelta(hours=24), source="test"
            )
        self.assertIsNone(selected)
        self.assertIn("Skipping test", output.getvalue())

    def test_does_not_move_a_pin_backward_to_an_eligible_release(self) -> None:
        current = update_pins.UpstreamRelease(
            "3.0.0", datetime.now(UTC) - timedelta(hours=2)
        )
        target = update_pins.UpstreamRelease(
            "2.0.0", datetime.now(UTC) - timedelta(days=2)
        )
        with redirect_stdout(io.StringIO()):
            can_move = update_pins.pin_can_move_forward(
                current.version, target, source="test"
            )
        self.assertFalse(can_move)


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


class NpmPinUpdateTests(TestCase):
    def test_collect_npm_updates_can_update_pi_pin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "Dockerfile.j2"
            config_agents_path = Path(tmp) / "config_agents.py"
            template.write_text(
                update_pins.DOCKERFILE_TEMPLATE.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            config_agents_path.write_text(
                update_pins.CONFIG_AGENTS.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            current_versions = {
                match.group("package"): match.group("version")
                for match in update_pins.NPM_PIN_RE.finditer(
                    template.read_text(encoding="utf-8")
                )
            }
            package = "@earendil-works/pi-coding-agent"
            latest = "999.0.0"

            def fake_latest_npm_releases(
                name: str,
            ) -> list[update_pins.UpstreamRelease]:
                version = latest if name == package else current_versions[name]
                return [
                    update_pins.UpstreamRelease(
                        version,
                        datetime.now(UTC) - timedelta(days=2),
                    )
                ]

            with (
                patch.object(update_pins, "DOCKERFILE_TEMPLATE", template),
                patch.object(update_pins, "CONFIG_AGENTS", config_agents_path),
                patch.object(
                    update_pins,
                    "latest_npm_releases",
                    side_effect=fake_latest_npm_releases,
                ),
            ):
                updates = update_pins.collect_npm_updates()

                self.assertEqual(len(updates), 1)
                self.assertEqual(updates[0].label, f"npm {package}")
                self.assertEqual(updates[0].current, current_versions[package])
                self.assertEqual(updates[0].latest, latest)

                updates[0].apply()

            self.assertIn(
                f"npm install -g {package}@{latest}",
                template.read_text(encoding="utf-8"),
            )
            self.assertIn(
                f'_PI_NPM_VERSION_PIN = "{latest}"',
                config_agents_path.read_text(encoding="utf-8"),
            )

    def test_config_agents_pi_pin_matches_dockerfile_template(self) -> None:
        # Regression: config_agents._PI_NPM_VERSION_PIN is a second, manually
        # maintained copy of the pi-coding-agent npm pin (used to populate
        # settings.json's lastChangelogVersion). Keep this assertion as a
        # repository-level guard in addition to testing the updater callback.
        package = update_pins.PI_NPM_PACKAGE
        text = update_pins.DOCKERFILE_TEMPLATE.read_text(encoding="utf-8")
        match = next(
            m
            for m in update_pins.NPM_PIN_RE.finditer(text)
            if m.group("package") == package
        )
        self.assertEqual(match.group("version"), config_agents._PI_NPM_VERSION_PIN)
