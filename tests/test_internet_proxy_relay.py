"""Apple container reaches a remote --internet-proxy through a host-loopback relay."""

import contextlib
import io
import json
import socket
import tempfile
import threading
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from project_sandbox import (
    build_proxy_forwarder,
    cli,
    container_cli,
    internet_proxy,
)
from project_sandbox.git_identity import GitIdentity

PROXY = "http://corp.proxy.example:3128"


class ForRuntimeTests(TestCase):
    def test_only_remote_proxies_on_apple_container_are_relayed(self):
        remote = internet_proxy.parse("https://corp.proxy.example:3128")
        local = internet_proxy.parse("http://127.0.0.1:3128")
        self.assertIsNone(
            internet_proxy.for_runtime(None, container_cli.APPLE_CONTAINER)
        )
        for runtime in (container_cli.DOCKER, container_cli.PODMAN, None):
            with self.subTest(runtime=runtime):
                self.assertIs(internet_proxy.for_runtime(remote, runtime), remote)
        self.assertIs(
            internet_proxy.for_runtime(local, container_cli.APPLE_CONTAINER), local
        )

        relayed = internet_proxy.for_runtime(remote, container_cli.APPLE_CONTAINER)
        assert relayed is not None
        port = internet_proxy.relay_port(remote)
        # The sandbox speaks plain HTTP; the host re-originates TLS upstream.
        self.assertEqual(relayed.forwarded_url, f"http://host.docker.internal:{port}")
        self.assertTrue(relayed.endpoint.local)
        self.assertEqual(relayed.service.port, port)
        self.assertEqual(relayed.original_url, remote.original_url)

    def test_relay_port_is_stable_and_separate_from_build_forwarder(self):
        config = internet_proxy.parse(PROXY)
        self.assertEqual(
            internet_proxy.relay_port(config),
            internet_proxy.relay_port(internet_proxy.parse(PROXY)),
        )
        self.assertNotEqual(
            internet_proxy.relay_port(config),
            build_proxy_forwarder.preferred_port(config.endpoint),
        )


class FixedPortForwarderTests(TestCase):
    def test_busy_fixed_port_fails_instead_of_falling_back(self):
        # The firewall baked into the image admits only the derived port.
        with socket.create_server(("127.0.0.1", 0)) as busy:
            port = busy.getsockname()[1]
            with self.assertRaisesRegex(
                SystemExit, f"internet proxy forwarder on 127.0.0.1:{port}"
            ):
                build_proxy_forwarder.BuildProxyForwarder(
                    internet_proxy.parse(PROXY).endpoint,
                    port=port,
                    fallback=False,
                    label="Internet proxy",
                )

    def test_unreachable_upstream_warning_names_runtime_options(self):
        warnings: list[str] = []
        with socket.create_server(("127.0.0.1", 0)) as closed:
            port = closed.getsockname()[1]
        forwarder = build_proxy_forwarder.BuildProxyForwarder(
            internet_proxy.parse(f"http://127.0.0.1:{port}").endpoint,
            warn=warnings.append,
            port=0,
            fallback=False,
            label="Internet proxy",
            ca_option="--ca-cert",
        )
        with forwarder:
            with socket.create_connection(("127.0.0.1", forwarder.port)) as client:
                client.settimeout(5)
                self.assertEqual(client.recv(1), b"")
        self.assertEqual(len(warnings), 1)
        self.assertTrue(warnings[0].startswith("[W] Internet proxy forwarder"))


class CliRelayTests(TestCase):
    def run_session(self, runtime):
        observed: dict[str, object] = {}

        def run(cmd, env=None):
            observed["env"] = dict(env or {})
            url = observed["env"].get("HTTP_PROXY", "")
            observed["url"] = url
            if "host.docker.internal" in url:
                port = int(url.rsplit(":", 1)[1])
                observed["port"] = port
                # The relay must already be serving when the sandbox starts.
                with socket.create_connection(("127.0.0.1", port), timeout=2):
                    observed["listening"] = True
            return 0

        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with (
                patch.object(cli.container_cli, "select_runtime", return_value=runtime),
                patch.object(
                    cli.container_cli, "ensure_system_started", return_value=0
                ),
                patch.object(cli.container_cli, "image_exists", return_value=True),
                patch.object(cli.container_cli, "run", side_effect=run),
                patch.object(cli.internet_proxy, "preflight"),
                patch.object(
                    cli, "read_identity", return_value=GitIdentity("Ada", "a@b.c")
                ),
                contextlib.redirect_stdout(out),
            ):
                rc = cli.main(
                    [
                        "--no-build",
                        "--no-forward-credentials",
                        "--no-verify-dockerfile",
                        "--agent",
                        "bash",
                        "--internet-proxy",
                        PROXY,
                        tmp,
                        "debian:bookworm",
                    ]
                )
            root = Path(tmp)
            observed["firewall"] = (
                root / ".project-sandbox/init-firewall.sh"
            ).read_text()
            observed["devcontainer_firewall"] = (
                root / ".project-sandbox/init-firewall-devcontainer.sh"
            ).read_text()
            observed["devcontainer"] = json.loads(
                (root / ".devcontainer/devcontainer.json").read_text()
            )
        self.assertEqual(rc, 0)
        observed["output"] = out.getvalue()
        return observed

    def test_apple_container_relays_remote_proxy_for_the_session(self):
        port = internet_proxy.relay_port(internet_proxy.parse(PROXY))
        threads_before = set(threading.enumerate())
        observed = self.run_session(container_cli.APPLE_CONTAINER)
        self.assertEqual(observed["url"], f"http://host.docker.internal:{port}")
        self.assertTrue(observed["listening"])
        self.assertIn(f"--dport {port} ", observed["firewall"])
        self.assertNotIn("corp.proxy.example", observed["firewall"])
        # Devcontainer tooling runs on Docker and reaches the proxy directly.
        self.assertIn("corp.proxy.example", observed["devcontainer_firewall"])
        self.assertIn("--dport 3128 ", observed["devcontainer_firewall"])
        self.assertEqual(
            observed["devcontainer"]["containerEnv"]["HTTP_PROXY"],
            "http://corp.proxy.example:3128",
        )
        self.assertIn("container system dns create", observed["output"])
        # The relay stops with the session.
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
        for thread in set(threading.enumerate()) - threads_before:
            self.assertNotIn("forwarder", thread.name)

    def test_docker_connects_to_remote_proxy_directly(self):
        observed = self.run_session(container_cli.DOCKER)
        self.assertEqual(observed["url"], "http://corp.proxy.example:3128")
        self.assertNotIn("port", observed)
        self.assertIn("corp.proxy.example", observed["firewall"])
        self.assertNotIn("container system dns create", observed["output"])
