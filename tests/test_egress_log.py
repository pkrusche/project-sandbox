"""egress_log: access-log classification, session windows and --since."""

import datetime as dt
import os
import time
from unittest import TestCase
from unittest.mock import patch

from project_sandbox import egress_log, squid_config
from project_sandbox.egress_log import LOCAL, UPSTREAM, Denial


class ClassifyTests(TestCase):
    # Lines in the sidecar's logformat, from live runs (one per cause).
    SAMPLE_LINES = (
        "2026/11/04 20:23:24| Accepting SSL bumped HTTP Socket connections at conn7",
        "SBX 1793823724.998 NONE_NONE/200 CONNECT example.com:443 sni=example.com bump=splice err=- cause=- peer=-",
        "SBX 1793823725.040 TCP_TUNNEL/200 CONNECT example.com:443 sni=example.com bump=splice err=- cause=- peer=172.66.147.243",
        "SBX 1793823725.046 TCP_DENIED/200 CONNECT www.wikipedia.org:443 sni=- bump=bump err=TCP_RESET cause=host-not-allowed peer=-",
        "SBX 1793823725.047 TCP_DENIED/200 CONNECT www.wikipedia.org:443 sni=- bump=bump err=TCP_RESET cause=host-not-allowed peer=-",
        "SBX 1793823725.053 TCP_DENIED/200 CONNECT 1.1.1.1:443 sni=- bump=bump err=TCP_RESET cause=ip-literal peer=-",
        "SBX 1793823725.058 TCP_DENIED/200 CONNECT example.com:8443 sni=- bump=bump err=TCP_RESET cause=port-not-allowed peer=-",
        "SBX 1793823725.120 TCP_DENIED/200 CONNECT 127.0.0.1.nip.io:443 sni=127.0.0.1.nip.io bump=bump err=TCP_RESET cause=private-address peer=-",
        "SBX 1793823725.151 TCP_DENIED/403 GET http://www.wikipedia.org/ sni=- bump=- err=ERR_SBX_HOST_NOT_ALLOWED cause=host-not-allowed peer=-",
        "SBX 1793823725.164 TCP_DENIED/403 GET http://api.github.com/ sni=- bump=- err=ERR_SBX_PLAIN_HTTP_INTERCEPTED cause=plain-http-intercepted peer=-",
        "SBX 1793823816.571 TCP_MISS/401 GET https://api.github.com/copilot_internal/user sni=api.github.com bump=bump err=- cause=- peer=20.250.119.72",
        "SBX 1793823816.619 TCP_DENIED/403 GET https://api.github.com/repos/octocat/Hello-World sni=api.github.com bump=bump err=ERR_SBX_PATH_NOT_ALLOWED cause=path-not-allowed peer=-",
        "SBX 1793823725.297 NONE_NONE/200 CONNECT example.com:443 sni=api.github.com bump=terminate err=- cause=- peer=-",
        "SBX 1793823727.342 NONE_NONE/200 CONNECT example.com:443 sni=- bump=peek err=ERR_PROTOCOL_UNKNOWN cause=- peer=-",
        "SBX 1793824508.385 TCP_DENIED/403 GET http://sidecar-readiness.invalid/ sni=- bump=- err=ERR_SBX_HOST_NOT_ALLOWED cause=host-not-allowed peer=-",
        # Upstream (parent proxy) denials: a refused CONNECT and a 407.
        "SBX 1793830000.100 TCP_TUNNEL/403 CONNECT blocked.corp.example:443 sni=- bump=- err=- cause=- peer=10.0.0.5",
        "SBX 1793830000.200 TCP_MISS/407 GET http://intranet.corp.example/ sni=- bump=- err=- cause=- peer=10.0.0.5",
    )
    SAMPLE = "\n".join(SAMPLE_LINES)

    def test_each_cause_is_classified(self) -> None:
        counts = egress_log.denials(self.SAMPLE, upstream=True)
        expected = {
            Denial("www.wikipedia.org", "host-not-allowed", LOCAL): 3,
            Denial("1.1.1.1", "ip-literal", LOCAL): 1,
            Denial("example.com", "port-not-allowed", LOCAL): 1,
            Denial("127.0.0.1.nip.io", "private-address", LOCAL): 1,
            Denial("api.github.com", "plain-http-intercepted", LOCAL): 1,
            Denial("api.github.com/repos/octocat/…", "path-not-allowed", LOCAL): 1,
            Denial("example.com", "sni-mismatch", LOCAL): 1,
            Denial("example.com", "non-tls-tunnel", LOCAL): 1,
            Denial("blocked.corp.example", "HTTP 403", UPSTREAM): 1,
            Denial("intranet.corp.example", "HTTP 407", UPSTREAM): 1,
        }
        self.assertEqual(dict(counts), expected)

    def test_origin_errors_are_not_upstream_denials_without_a_parent(self) -> None:
        counts = egress_log.denials(self.SAMPLE, upstream=False)
        self.assertFalse([d for d in counts if d.attribution == UPSTREAM])
        # A 401 from GitHub through an allowed path is never a denial.
        self.assertFalse([d for d in counts if "copilot_internal" in d.target])


class ClientFieldTests(TestCase):
    """Task 2.1: lines carry client=%>a; older lines without it still parse."""

    WITH = (
        "SBX 1793823725.046 TCP_DENIED/200 CONNECT www.wikipedia.org:443 sni=- "
        "bump=bump err=TCP_RESET cause=host-not-allowed peer=- client=192.168.128.3"
    )
    WITHOUT = WITH.rsplit(" client=", 1)[0]

    def test_logformat_records_the_client(self) -> None:
        from project_sandbox import egress_policy

        conf = squid_config.render(egress_policy.effective(egress_policy.PolicyFile()))
        logformat = next(l for l in conf.splitlines() if l.startswith("logformat sbx"))
        self.assertTrue(logformat.endswith("peer=%<a client=%>a"))

    def test_lines_with_and_without_client(self) -> None:
        with_client = egress_log.classify(self.WITH, upstream=False)
        without = egress_log.classify(self.WITHOUT, upstream=False)
        assert with_client is not None and without is not None
        self.assertEqual(with_client.client, "192.168.128.3")
        self.assertIsNone(without.client)
        self.assertEqual(with_client, without)  # identity ignores ts/client
        self.assertEqual(with_client.ts, 1793823725.046)

    def test_format_denial_shows_cause_and_client(self) -> None:
        denial = egress_log.classify(self.WITH, upstream=False)
        assert denial is not None
        line = egress_log.format_denial(denial)
        self.assertIn("192.168.128.3", line)
        self.assertIn("www.wikipedia.org  host-not-allowed", line)


def _line(ts: float, host: str = "blocked.example") -> str:
    return (
        f"SBX {ts:.3f} TCP_DENIED/200 CONNECT {host}:443 sni=- bump=bump "
        "err=TCP_RESET cause=host-not-allowed peer=- client=10.0.0.2"
    )


class WindowTests(TestCase):
    """Task 6.6: count denials in the session window on the sidecar's clock."""

    TEXT = "\n".join(
        [
            _line(1000.0),  # before
            _line(1010.0),  # at start
            "2026/11/06 10:00:00| cache.log noise",
            _line(1015.5),
            (
                "SBX 1016.000 TCP_TUNNEL/200 CONNECT ok.example:443 sni=ok.example "
                "bump=splice err=- cause=- peer=1.2.3.4 client=10.0.0.3"
            ),
            _line(1020.0),  # at end
            _line(1020.5),  # after
            _line(1012.0, egress_log.READINESS_HOST),  # readiness probe: ignored
        ]
    )

    def test_window_is_inclusive_and_skips_allowed_and_probe(self) -> None:
        self.assertEqual(
            egress_log.count_in_window(self.TEXT, start=1010.0, end=1020.0), 3
        )
        self.assertEqual(egress_log.count_in_window(self.TEXT, start=0, end=1e12), 5)
        self.assertEqual(egress_log.count_in_window("", start=0, end=1e12), 0)

    def test_summary_names_the_log_command(self) -> None:
        with patch.dict(os.environ, {"TZ": "UTC"}):
            time.tzset()
            try:
                since = dt.datetime(
                    2026, 11, 5, 9, 2, 13, 900000, tzinfo=dt.UTC
                ).timestamp()
                lines = egress_log.session_summary(3, since=since)
            finally:
                os.environ.pop("TZ")
                time.tzset()
        self.assertIn("denied 3 requests during this session", lines[0])
        self.assertIn("all sandboxes", lines[0])
        self.assertEqual(
            lines[1].strip(),
            "List them: project-sandbox-egress log --denied --since 2026-11-05T09:02:13",
        )
        self.assertIn("1 request during", egress_log.session_summary(1, since=0)[0])

    def test_no_denials_prints_nothing(self) -> None:
        self.assertEqual(egress_log.session_summary(0, since=0), [])


class SinceTests(TestCase):
    """Task 7.4: --since takes an ISO 8601 time or a duration."""

    def test_durations(self) -> None:
        for value, seconds in (("30s", 30), ("10m", 600), ("2h", 7200), ("1d", 86400)):
            with self.subTest(value=value):
                self.assertEqual(
                    egress_log.parse_since(value, now=10_000.0), 10_000.0 - seconds
                )

    def test_iso_times(self) -> None:
        aware = egress_log.parse_since("2026-11-06T10:42:13+00:00", now=0)
        self.assertEqual(
            aware, dt.datetime(2026, 11, 6, 10, 42, 13, tzinfo=dt.UTC).timestamp()
        )
        naive = egress_log.parse_since("2026-11-06T10:42:13", now=0)
        self.assertEqual(
            naive, dt.datetime(2026, 11, 6, 10, 42, 13).astimezone().timestamp()
        )
        # iso_local round-trips through parse_since.
        self.assertEqual(
            egress_log.parse_since(egress_log.iso_local(naive), now=0), naive
        )

    def test_invalid(self) -> None:
        for value in ("yesterday", "10 m", "5w", ""):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "--since"),
            ):
                egress_log.parse_since(value, now=0)
