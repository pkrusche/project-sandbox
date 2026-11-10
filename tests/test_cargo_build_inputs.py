import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase

from project_sandbox import build_inputs, cli, dockerfile


class CargoBuildInputTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.context = self.project / ".project-sandbox"

    def workspace(self, members):
        (self.project / "Cargo.toml").write_text(
            "[workspace]\nmembers = " + json.dumps(members) + "\n"
        )

    def member(self, name):
        path = self.project / name
        path.mkdir(parents=True, exist_ok=True)
        (path / "Cargo.toml").write_text('[package]\nname = "fixture"\n')
        return path

    def render(self, members):
        return dockerfile.render_rust_cargo_dockerfile(
            self.context, None, True, True, workspace_members=members
        ).read_text()

    def test_literal_names_survive_copy_and_shell_without_execution(self):
        names = [
            "has spaces",
            "single'quote",
            'double"quote',
            "cash$HOME",
            "crate$(touch PWNED)",
            "crate`touch PWNED`",
            "crate;touch PWNED",
            "-leading",
            "unicode-λ",
        ]
        for name in names:
            self.member(name)
        self.workspace(names)
        _, detected, _ = cli._detect_cargo_workspace(self.project)
        self.assertEqual(detected, sorted(names))
        self.context.mkdir()
        text = self.render(detected)
        copies = [
            json.loads(line[5:])
            for line in text.splitlines()
            if line.startswith("COPY [")
        ][1:-1]
        # Docker's word expansion consumes quoting inside JSON strings. Probe
        # the same literals with a shell, with HOME set to expose accidental
        # variable expansion, rather than merely comparing quote characters.
        for name, pair in zip(sorted(names), copies, strict=True):
            for token, suffix in zip(pair, ("/Cargo.toml", "/"), strict=True):
                result = subprocess.run(
                    ["sh", "-c", "printf '%s' " + token],
                    cwd=self.project,
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout
                self.assertEqual(result, name + suffix)
        self.assertFalse((self.project / "PWNED").exists())

        # Execute the actual generated mkdir/touch/fetch/rm chain. A fake cargo
        # checks every literal stub before removal; nothing runs a real build.
        check = self.root / "check.py"
        check.write_text(
            "import json, os\nfrom pathlib import Path\n"
            "for name in json.loads(os.environ['EXPECTED_MEMBERS']):\n"
            "    assert (Path(name) / 'src/lib.rs').is_file(), name\n"
            "assert not Path('PWNED').exists()\n"
        )
        flattened = text.replace("\\\n", "")
        command = next(
            line[4:]
            for line in flattened.splitlines()
            if line.startswith("RUN mkdir -p --")
        )
        subprocess.run(
            ["sh", "-ec", 'cargo() { "$TEST_PYTHON" "$TEST_CHECK"; }; ' + command],
            cwd=self.project,
            check=True,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "EXPECTED_MEMBERS": json.dumps(names),
                "TEST_PYTHON": sys.executable,
                "TEST_CHECK": str(check),
            },
        )
        self.assertFalse((self.project / "PWNED").exists())
        for name in names:
            self.assertFalse((self.project / name / "src").exists())
            self.assertTrue((self.project / name / "Cargo.toml").is_file())

    def test_copy_sources_escape_literal_wildcards(self):
        self.context.mkdir()
        text = self.render(["crate[1]", "star*", "question?"])
        copies = [
            json.loads(line[5:])
            for line in text.splitlines()
            if line.startswith("COPY [")
        ][1:-1]
        decoded = [(shlex.split(src)[0], shlex.split(dst)[0]) for src, dst in copies]
        self.assertEqual(
            decoded,
            [
                ("crate[[]1]/Cargo.toml", "crate[1]/"),
                ("question[?]/Cargo.toml", "question?/"),
                ("star[*]/Cargo.toml", "star*/"),
            ],
        )

    def test_rejects_unsafe_patterns_before_traversal_or_rendering(self):
        for name in (
            "../outside",
            "safe/../../outside",
            "/absolute",
            "bad\nRUN evil",
            "bad\rname",
            "back\\slash",
            "bad\x00name",
            "bad\x7fname",
            "bad\x85name",
            "bad\u2028name",
            "bad\u2029name",
            "",
            12,
        ):
            with self.subTest(name=name):
                self.workspace([name])
                with self.assertRaises(SystemExit):
                    cli._detect_cargo_workspace(self.project)
                with self.assertRaises(ValueError):
                    self.render([name])
                self.assertFalse(self.context.exists())

    def test_member_and_manifest_symlinks_cannot_escape_project(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "Cargo.toml").write_text('[package]\nname = "outside"\n')
        for use_manifest in (False, True):
            with self.subTest(use_manifest=use_manifest):
                name = "manifest-link" if use_manifest else "directory-link"
                member = self.project / name
                if use_manifest:
                    member.mkdir()
                    (member / "Cargo.toml").symlink_to(outside / "Cargo.toml")
                else:
                    member.symlink_to(outside, target_is_directory=True)
                self.workspace([name])
                with self.assertRaisesRegex(SystemExit, "escapes the project"):
                    cli._detect_cargo_workspace(self.project)

    def test_glob_results_are_validated_and_deduplicated(self):
        self.member("crates/normal")
        self.workspace(["crates/*", "crates/normal"])
        self.assertEqual(
            cli._detect_cargo_workspace(self.project)[1], ["crates/normal"]
        )
        self.member("crates/control\nname")
        with self.assertRaisesRegex(SystemExit, "control characters"):
            cli._detect_cargo_workspace(self.project)

    def test_internal_symlink_preserves_lexical_member_path(self):
        member = self.member("actual")
        (self.project / "alias").symlink_to(member, target_is_directory=True)
        self.workspace(["alias"])
        self.assertEqual(cli._detect_cargo_workspace(self.project)[1], ["alias"])

    def test_invalid_images_and_versions_fail_dry_run_without_writes(self):
        for flag in ("--rust", "--python", "base_image"):
            for value in (
                "tag\nRUN touch PWNED",
                "tag AS stage",
                "$BASE",
                "--platform=evil",
                "",
                "tag;evil",
            ):
                if flag == "base_image" and not value:
                    continue
                with self.subTest(flag=flag, value=value):
                    args = argparse.Namespace(
                        docker_context=None,
                        dockerfile=None,
                        base_image=value if flag == "base_image" else None,
                        rust_cargo=flag == "--rust",
                        rust_version=value if flag == "--rust" else None,
                        python_uv=flag == "--python",
                        python_version=value if flag == "--python" else None,
                        node_npm=False,
                    )
                    with self.assertRaises(SystemExit):
                        cli._resolve_build_source(
                            args,
                            project=self.project,
                            context_dir=self.context,
                            write_generated=False,
                        )
                    self.assertFalse(self.context.exists())

    def test_direct_renderers_validate_from_inputs_before_writing(self):
        for render in (
            lambda: dockerfile.render(self.context, base_image="debian\nRUN evil"),
            lambda: dockerfile.render_python_uv_dockerfile(
                self.context, "3.12\nRUN evil", False, False
            ),
            lambda: dockerfile.render_rust_cargo_dockerfile(
                self.context, "1.87\nRUN evil", False, False
            ),
        ):
            with self.assertRaises(ValueError):
                render()
            self.assertFalse(self.context.exists())

    def test_supported_literal_image_and_version_tokens(self):
        for image in (
            "debian:bookworm-slim",
            "registry.example:5000/team/image:tag",
            "ubuntu@sha256:" + "a" * 64,
        ):
            build_inputs.validate_image(image)
        for version in ("1.87", "3.12.1", "nightly-2026-10-01"):
            build_inputs.validate_version(version, option="version")

    def test_workspace_root_member_is_not_stubbed_as_a_child(self):
        self.member("child")
        self.workspace([".", "./", "child"])
        self.assertEqual(
            cli._detect_cargo_workspace(self.project), (True, ["child"], False)
        )

    def test_malformed_workspace_shapes_fail_with_guidance(self):
        for manifest in (
            "workspace = []",
            '[workspace]\nmembers = "child"',
            "[workspace]\nmembers = [3]",
            '[workspace]\nexclude = "child"',
        ):
            with self.subTest(manifest=manifest):
                (self.project / "Cargo.toml").write_text(manifest)
                with self.assertRaisesRegex(SystemExit, "--rust-cargo"):
                    cli._detect_cargo_workspace(self.project)
