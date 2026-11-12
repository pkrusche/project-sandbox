import json
import subprocess
from unittest import TestCase
from unittest.mock import Mock, patch

from project_sandbox import container_cli as runtime_cli

IDENTITY = "sha256:" + "a" * 64


class ImageIdentityTests(TestCase):
    def test_runtime_inspection_formats(self):
        for runtime, payload in (
            (runtime_cli.DOCKER, [{"Id": IDENTITY}]),
            (runtime_cli.PODMAN, [{"Id": "a" * 64}]),
            (
                runtime_cli.APPLE_CONTAINER,
                [{"configuration": {"descriptor": {"digest": IDENTITY}}}],
            ),
        ):
            with (
                self.subTest(runtime=runtime.name),
                patch.object(
                    runtime_cli.subprocess,
                    "run",
                    return_value=Mock(stdout=json.dumps(payload)),
                ) as inspect,
            ):
                self.assertEqual(
                    runtime_cli.image_identity(runtime, "shared"), IDENTITY
                )
                self.assertEqual(
                    inspect.call_args.args[0],
                    [runtime.binary, "image", "inspect", "shared"],
                )

    def test_missing_malformed_or_ambiguous_identity_fails_closed(self):
        for payload in (
            "invalid",
            "null",
            "{}",
            "[]",
            "[{}, {}]",
            "[{}]",
            '[{"Id": "sha256:abc"}]',
            '[{"Id": 1}]',
            '[{"RepoTags": ["shared:latest"]}]',
        ):
            with (
                self.subTest(payload=payload),
                patch.object(
                    runtime_cli.subprocess, "run", return_value=Mock(stdout=payload)
                ),
            ):
                with self.assertRaisesRegex(SystemExit, "refusing to launch"):
                    runtime_cli.image_identity(runtime_cli.DOCKER, "shared")
        for error in (FileNotFoundError(), subprocess.CalledProcessError(1, "inspect")):
            with (
                self.subTest(error=error),
                patch.object(runtime_cli.subprocess, "run", side_effect=error),
            ):
                with self.assertRaises(SystemExit):
                    runtime_cli.image_identity(runtime_cli.DOCKER, "shared")

    def test_docker_and_podman_launch_content_ids_kept_alive_by_an_alias(self):
        # Regression: a concurrent launch moving the shared tag left the first
        # launch's image untagged, and Docker's containerd store deleted it
        # before `docker run <id>` ("No such image").
        for runtime in (runtime_cli.DOCKER, runtime_cli.PODMAN):
            with (
                self.subTest(runtime=runtime.name),
                patch.object(runtime_cli, "run", return_value=0) as run,
                patch.object(
                    runtime_cli, "image_identity", return_value=IDENTITY
                ) as inspect,
            ):
                with runtime_cli.pinned_image(runtime, "shared", IDENTITY) as ref:
                    self.assertEqual(ref, IDENTITY)
                    tag = run.call_args.args[0]
                    self.assertEqual(
                        tag[:4], [runtime.binary, "image", "tag", "shared"]
                    )
                    alias = tag[4]
                    self.assertTrue(
                        alias.startswith("localhost/project-sandbox-session:")
                    )
                    inspect.assert_called_with(runtime, alias)
                    self.assertEqual(run.call_count, 1)
                self.assertEqual(
                    run.call_args.args[0], [runtime.binary, "image", "rm", alias]
                )

    def test_docker_alias_failure_never_yields_and_still_cleans_up(self):
        for tag_rc, identity in ((1, IDENTITY), (0, "sha256:" + "b" * 64)):
            with (
                self.subTest(tag_rc=tag_rc),
                patch.object(runtime_cli, "run", side_effect=[tag_rc, 0]) as run,
                patch.object(runtime_cli, "image_identity", return_value=identity),
            ):
                with self.assertRaises(SystemExit):
                    with runtime_cli.pinned_image(
                        runtime_cli.DOCKER, "shared", IDENTITY
                    ):
                        self.fail("Unverified alias was yielded")
                self.assertEqual(run.call_args.args[0][:3], ["docker", "image", "rm"])

    def test_apple_alias_is_unique_verified_and_cleaned_on_interrupt(self):
        aliases = []
        with (
            patch.object(runtime_cli, "run", return_value=0) as run,
            patch.object(
                runtime_cli, "image_identity", return_value=IDENTITY
            ) as inspect,
        ):
            for _ in range(2):
                with self.assertRaises(KeyboardInterrupt):
                    with runtime_cli.pinned_image(
                        runtime_cli.APPLE_CONTAINER, "shared", IDENTITY
                    ) as ref:
                        aliases.append(ref)
                        self.assertTrue(
                            ref.startswith("localhost/project-sandbox-session:")
                        )
                        inspect.assert_called_with(runtime_cli.APPLE_CONTAINER, ref)
                        self.assertEqual(
                            run.call_args.args[0],
                            ["container", "image", "tag", "shared", ref],
                        )
                        raise KeyboardInterrupt
                self.assertEqual(
                    run.call_args.args[0], ["container", "image", "delete", ref]
                )
        self.assertNotEqual(*aliases)

    def test_apple_alias_failure_never_yields_and_still_cleans_up(self):
        for tag_rc, identity in ((1, IDENTITY), (0, "sha256:" + "b" * 64)):
            with (
                self.subTest(tag_rc=tag_rc),
                patch.object(runtime_cli, "run", side_effect=[tag_rc, 0]) as run,
                patch.object(runtime_cli, "image_identity", return_value=identity),
            ):
                with self.assertRaises(SystemExit):
                    with runtime_cli.pinned_image(
                        runtime_cli.APPLE_CONTAINER, "shared", IDENTITY
                    ):
                        self.fail("Unverified alias was yielded")
                self.assertEqual(
                    run.call_args.args[0][:3], ["container", "image", "delete"]
                )
