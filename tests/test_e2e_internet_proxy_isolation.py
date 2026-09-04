import importlib.util
import re
from pathlib import Path
from unittest import TestCase


def _load_checker():
    path = Path(__file__).parents[1] / "scripts/e2e-internet-proxy-isolation.py"
    spec = importlib.util.spec_from_file_location("e2e_internet_proxy_isolation", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InternetProxyEndToEndScriptTests(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (
            Path(__file__).parents[1] / "scripts/e2e-internet-proxy-isolation.py"
        ).read_text(encoding="utf-8")

    def test_covers_every_bypass_path(self) -> None:
        for evidence in (
            '"--noproxy", "*"',
            '"HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"',
            "socket.SOCK_STREAM, 443",
            "socket.SOCK_DGRAM, 53",
            '"direct DNS is blocked"',
        ):
            with self.subTest(evidence=evidence):
                self.assertIn(evidence, self.source)

    def test_covers_policy_separation_and_failure_phases(self) -> None:
        for evidence in (
            "recognizable proxy-policy denial",
            "AI completion succeeds through Agentgateway",
            "proxy loss fails closed without direct fallback",
            "ordinary Internet still works while Agentgateway is stopped",
        ):
            with self.subTest(evidence=evidence):
                self.assertIn(evidence, self.source)

    def test_service_control_actions_match_the_service_clis(self) -> None:
        # Both controlled services take up/down/restart and neither has a
        # "stop": `ipl` (internet-proxy-locally) and agentgateway-locally's
        # run.py. Comparing the full set also fails if a retired verb returns.
        actions = set(re.findall(r'control\(\s*args\.(\w+),\s*"(\w+)"', self.source))
        self.assertEqual(
            actions,
            {
                ("internet_proxy_control", "down"),
                ("internet_proxy_control", "restart"),
                ("agentgateway_control", "down"),
                ("agentgateway_control", "restart"),
            },
        )

    def test_uses_current_service_control_commands(self) -> None:
        args = (
            _load_checker()
            .parser()
            .parse_args(
                [
                    "--runtime",
                    "docker",
                    "--blocked-url",
                    "https://blocked.example.test/",
                    "--internet-proxy-dir",
                    "/tmp/internet-proxy-locally",
                    "--agentgateway-dir",
                    "/tmp/agentgateway-locally",
                ]
            )
        )

        self.assertEqual(args.internet_proxy_control, "uv run ipl {action}")
        self.assertEqual(args.agentgateway_control, "./run.py {action}")
