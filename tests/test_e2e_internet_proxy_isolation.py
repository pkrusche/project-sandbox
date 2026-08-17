from pathlib import Path
from unittest import TestCase


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
            'control(args.internet_proxy_control, "stop"',
            'control(args.internet_proxy_control, "restart"',
            'control(args.agentgateway_control, "stop"',
            "proxy loss fails closed without direct fallback",
            "ordinary Internet still works while Agentgateway is stopped",
        ):
            with self.subTest(evidence=evidence):
                self.assertIn(evidence, self.source)
