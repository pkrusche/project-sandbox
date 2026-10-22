from unittest import TestCase
from unittest.mock import patch

from project_sandbox import fixture_network


class FixtureNetworkTests(TestCase):
    def test_uses_os_selected_source_address(self):
        with patch.object(fixture_network.socket, "socket") as factory:
            probe = factory.return_value.__enter__.return_value
            probe.getsockname.return_value = ("192.168.1.20", 12345)
            self.assertEqual(fixture_network.default_host(), "192.168.1.20")
            probe.connect.assert_called_once_with(("192.0.2.1", 9))
            probe.send.assert_not_called()
            probe.sendto.assert_not_called()

    def test_detection_failure_explains_override(self):
        for address in ("127.0.0.1", "0.0.0.0", "224.0.0.1"):
            with (
                self.subTest(address=address),
                patch.object(fixture_network.socket, "socket") as factory,
            ):
                factory.return_value.__enter__.return_value.getsockname.return_value = (
                    address,
                    12345,
                )
                with self.assertRaisesRegex(ValueError, "--proxy-fixture-host"):
                    fixture_network.default_host()
        with patch.object(fixture_network.socket, "socket", side_effect=OSError):
            with self.assertRaisesRegex(ValueError, "--fixture-host"):
                fixture_network.default_host()
