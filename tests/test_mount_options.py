import csv
import io
from pathlib import Path
from unittest import TestCase

from project_sandbox import container_cli, mount_options


class MountOptionsTests(TestCase):
    def test_csv_paths_round_trip_through_runtime_arguments(self) -> None:
        value = 'type=bind,"src=/tmp/path,with ""quotes""",destination=/data,ro=false'
        mount = container_cli.parse_mount(value)
        # Sources are canonicalized (macOS /tmp -> /private/tmp).
        source = Path('/tmp/path,with "quotes"').resolve()
        self.assertEqual(mount.source, source)
        self.assertEqual(mount.target, "/data")
        self.assertFalse(mount.readonly)
        rendered = container_cli._mount_arg(mount)
        self.assertEqual(
            next(csv.reader(io.StringIO(rendered))),
            [
                "type=bind",
                f"source={source}",
                "target=/data",
            ],
        )
        # bind_source keeps the lexical path; callers resolve before checks.
        self.assertEqual(
            mount_options.bind_source(value), Path('/tmp/path,with "quotes"')
        )

    def test_safe_bind_options_preserve_raw_runtime_arguments_and_expose_source(
        self,
    ) -> None:
        for option in (
            "bind-propagation=rprivate",
            "bind-recursive=readonly",
            "consistency=cached",
            "bind-nonrecursive",
        ):
            with self.subTest(option=option):
                value = f'type=bind,"source=/tmp/a,b",target=/data,readonly,{option}'
                mount = container_cli.parse_mount(value)
                self.assertEqual(container_cli._mount_arg(mount), value)
                self.assertEqual(mount.source, Path("/tmp/a,b").resolve())
                self.assertEqual(mount_options.bind_source(value), Path("/tmp/a,b"))
                self.assertTrue(mount.readonly)

    def test_alias_case_and_boolean_values_are_consistent(self) -> None:
        for boolean, expected in (
            ("true", True),
            ("TRUE", True),
            ("1", True),
            ("false", False),
            ("0", False),
        ):
            with self.subTest(boolean=boolean):
                value = f"TYPE=BIND,SRC=/tmp/src,DESTINATION=/data,RO={boolean}"
                self.assertEqual(container_cli.parse_mount(value).readonly, expected)
                self.assertEqual(mount_options.bind_source(value), Path("/tmp/src"))

    def test_rejects_ambiguous_aliases_and_malformed_csv(self) -> None:
        for value in (
            "type=bind,source=/safe,src=/secret,target=/data",
            "type=bind,type=volume,source=/safe,target=/data",
            "type=bind,source=/safe,target=/data,dst=/other",
            "type=bind,source=/safe,target=/data,ro,readonly=false",
            'type=bind,source="/tmp/a,b",target=/data',
            'type=bind,"source=/tmp/a,b,target=/data',
            'type=bind,"source=/tmp/a"junk,target=/data',
            "type=bind,source=/tmp/a\ntype=volume,target=/data",
            "type=bind,source=/tmp/a,target=/data,",
            "type=bind,source=,target=/data",
            "type=bind,source=/tmp/a,target=/data,ro=maybe",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    mount_options.bind_source(value)
                with self.assertRaises(SystemExit):
                    container_cli.parse_mount(value)

    def test_unmodeled_or_indirect_exposure_fails_closed(self) -> None:
        for value in (
            "type=volume,source=existing-volume,target=/data",
            "type=volume,source=local,target=/data,volume-opt=device=/secret",
            "type=bind,source=/safe,target=/data,volume-opt=device=/secret",
            "type=bind,source=/safe,target=/data,mystery=/secret",
            "type=bind,source=/safe,target=/data,bind-propagation=unknown",
            "source=/safe,target=/data,bind-propagation=rprivate",
            "type=tmpfs,target=/scratch",
            "type=bind,target=/data",
            "type=bind,source=/safe,target=relative,consistency=cached",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                mount_options.bind_source(value)

    def test_shorthand_is_rendered_with_explicit_bind_type(self) -> None:
        mount = container_cli.parse_mount("source=./data,target=/data")
        self.assertEqual(
            mount_options.bind_source("source=./data,target=/data"),
            Path("data").absolute(),
        )
        self.assertTrue(container_cli._mount_arg(mount).startswith("type=bind,"))
