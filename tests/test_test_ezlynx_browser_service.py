"""Static safety contract for the isolated Test browser service."""
import unittest
from pathlib import Path




ROOT = Path(__file__).resolve().parents[1]




class TestBrowserServiceTests(unittest.TestCase):
    def test_unit_is_test_only_and_loopback_only(self):
        service = (ROOT / "deploy/systemd/robie-ezlynx-browser-test.service").read_text()
        self.assertIn("ConditionHost=hermes-test-01.c.streetsmart-hermes-poc.internal", service)
        self.assertIn("User=streetsmart-hermes-test", service)
        self.assertIn("Environment=HOME=/opt/streetsmart-hermes-test", service)
        self.assertIn("--remote-debugging-address=127.0.0.1", service)
        self.assertIn("--remote-debugging-port=9222", service)
        self.assertIn(
            "--user-data-dir=/opt/streetsmart-hermes-test/.hermes/browser-profiles/ezlynx",
            service,
        )
        self.assertNotIn("User=streetsmart-hermes\n", service)
        self.assertNotIn("=/opt/streetsmart-hermes/", service)


    def test_installer_refuses_wrong_host_and_current_release_mismatch(self):
        installer = (ROOT / "scripts/install-test-ezlynx-browser.sh").read_text()
        self.assertIn('EXPECTED_HOST="hermes-test-01"', installer)
        self.assertIn('TEST_ROOT="/opt/streetsmart-hermes-test"', installer)
        self.assertIn('readlink -f "${TEST_ROOT}/releases/current"', installer)
        self.assertIn("refusing non-Test host", installer)
        self.assertIn("systemctl enable --now", installer)
        self.assertIn("TEST_CDP_READY", installer)




if __name__ == "__main__":
    unittest.main()
