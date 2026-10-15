import unittest

from project_sandbox import agent_proxy, internet_proxy


class EndpointTests(unittest.TestCase):
    def test_endpoint_matrix(self):
        for url, local, port, host in (
            ("http://LOCALHOST./v1", True, 80, "localhost."),
            ("http://127.2.3.4/v1", True, 80, "127.2.3.4"),
            ("http://[::1]:4000/v1", True, 4000, "::1"),
            ("http://[::ffff:127.0.0.1]/v1", True, 80, "::ffff:127.0.0.1"),
            ("https://models.corp.example/v1", False, 443, "models.corp.example"),
            ("http://10.1.2.3:8080/api/v1", False, 8080, "10.1.2.3"),
            ("https://[2001:db8::1]/v1", False, 443, "2001:db8::1"),
        ):
            with self.subTest(url=url):
                endpoint = agent_proxy.parse(url)
                self.assertEqual(
                    (endpoint.local, endpoint.port, endpoint.host), (local, port, host)
                )
                self.assertTrue(endpoint.sandbox_url.endswith(endpoint.path))
                self.assertEqual("host.docker.internal" in endpoint.sandbox_url, local)
        self.assertEqual(
            internet_proxy.parse("https://proxy.example").forwarded_url,
            "https://proxy.example:443",
        )

    def test_rejected_authorities_and_address_classes(self):
        for value in (
            "https://LOCALHOST./",
            "https://127.9.8.7",
            "https://[::ffff:127.0.0.1]",
            "http://@host",
            "http://user:secret@host",
            "http://host:",
            "http://host:0",
            "http://host:65536",
            "http://host:bad",
            "http://",
            "http://0.0.0.0",
            "http://[::]",
            "http://224.0.0.1",
            "http://[ff02::1]",
            "http://[::ffff:0.0.0.0]",
            "http://[fe80::1%eth0]",
            "http://[::1]junk",
            "http://127.1",
            "http://2130706433",
            "http://0x7f000001",
            "http://0177.0.0.1",
            "http://a;id",
            "http://a\nb",
            "http://a_b",
            "http://a\\b",
            "http://-a",
            "http://[v1.abc]",
            "http://a?",
            "http://a#",
            "ftp://host",
            "http://::1",
            "http://*.example",
        ):
            for parser in (agent_proxy.parse, internet_proxy.parse):
                with (
                    self.subTest(value=value, parser=parser),
                    self.assertRaises(SystemExit) as error,
                ):
                    parser(value)
                self.assertNotIn("secret", str(error.exception))

    def test_paths(self):
        self.assertEqual(
            agent_proxy.forwarded_url("https://0xmodels.example/api/v1/"),
            "https://0xmodels.example:443/api/v1/",
        )
        self.assertEqual(
            agent_proxy.forwarded_url("https://models.example/api/v1"),
            "https://models.example:443/api/v1",
        )
        self.assertEqual(
            internet_proxy.parse("https://proxy.example/").endpoint.path, ""
        )
        with self.assertRaises(SystemExit):
            internet_proxy.parse("https://proxy.example/api")

    def test_independent_bypass(self):
        values = internet_proxy.environment(
            internet_proxy.parse("https://proxy.example"),
            bypass_local_services=False,
            bypass_hosts=("models.example", "models.example"),
        )
        self.assertEqual(
            values["NO_PROXY"], "localhost,127.0.0.1,::1,models.example,[::1]"
        )
        self.assertEqual(values["NO_PROXY"], values["no_proxy"])

    def test_ipv6_bypass_includes_url_canonical_forms(self):
        values = internet_proxy.environment(
            internet_proxy.parse("https://proxy.example"),
            bypass_local_services=False,
            bypass_hosts=("::ffff:192.0.2.1", "2001:0db8:0:0:0:0:0:1"),
        )
        bypass = values["NO_PROXY"].split(",")
        for host in (
            "::ffff:192.0.2.1",
            "::ffff:c000:201",
            "[::ffff:c000:201]",
            "2001:db8::1",
            "[2001:db8::1]",
        ):
            self.assertIn(host, bypass)
        self.assertEqual(len(bypass), len(set(bypass)))
