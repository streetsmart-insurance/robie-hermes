"""Regression test: browser systemd units support optional proxy routing.

Carrier portals (e.g. AmTrust's F5 wall) bot-block datacenter egress IPs.
The persistent Chrome units accept an optional env file
/etc/streetsmart-hermes/robie-browser-proxy.env defining PROXY_CHROME_FLAGS
(e.g. --proxy-server=http://proxy.webshare.io:8080, with Webshare IP-auth so
no proxy credentials live on the box). The leading '-' on EnvironmentFile
keeps the file optional: without it Chrome launches exactly as before.

This test pins that contract so a future unit edit cannot silently drop the
proxy wiring or, worse, hardcode proxy credentials into the repo.
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

BROWSER_UNITS = [
    "deploy/systemd/robie-ezlynx-browser.service",
    "deploy/systemd/robie-ezlynx-browser-test.service",
    "deploy/systemd/robie-magellan-browser.service",
    "robie-ezlynx-browser.service",
]

ENV_FILE_LINE = "EnvironmentFile=-/etc/streetsmart-hermes/robie-browser-proxy.env"
FLAGS_VAR = "${PROXY_CHROME_FLAGS}"


class BrowserProxyUnitTests(unittest.TestCase):
    def _read(self, rel):
        path = REPO_ROOT / rel
        self.assertTrue(path.is_file(), f"browser unit missing: {rel}")
        return path.read_text()

    def test_units_reference_optional_proxy_env_file(self):
        for rel in BROWSER_UNITS:
            with self.subTest(unit=rel):
                text = self._read(rel)
                self.assertIn(
                    ENV_FILE_LINE,
                    text,
                    f"{rel} must source the optional proxy env file",
                )

    def test_execstart_injects_proxy_flags_variable(self):
        for rel in BROWSER_UNITS:
            with self.subTest(unit=rel):
                text = self._read(rel)
                exec_lines = [
                    line
                    for line in text.splitlines()
                    if line.startswith("ExecStart=")
                ]
                self.assertTrue(exec_lines, f"{rel} has no ExecStart line")
                for line in exec_lines:
                    self.assertIn(
                        FLAGS_VAR,
                        line,
                        f"{rel} ExecStart must inject {FLAGS_VAR}",
                    )

    def test_no_hardcoded_proxy_credentials_in_units(self):
        # Proxy credentials must never be committed; Webshare IP-auth means the
        # unit only ever carries --proxy-server=<host:port> via the env file.
        credential_markers = ["proxy-password", "proxy_password", "PROXY_PASSWORD"]
        for rel in BROWSER_UNITS:
            with self.subTest(unit=rel):
                text = self._read(rel)
                for marker in credential_markers:
                    self.assertNotIn(
                        marker, text, f"{rel} must not contain {marker}"
                    )
                # No literal proxy URL baked into an *active* unit line either
                # (commented examples are documentation, not configuration).
                active_lines = [
                    line
                    for line in text.splitlines()
                    if line.strip() and not line.strip().startswith("#")
                ]
                self.assertIsNone(
                    re.search(
                        r"--proxy-server=https?://\S+",
                        "\n".join(active_lines),
                    ),
                    f"{rel} must not hardcode a --proxy-server URL",
                )


if __name__ == "__main__":
    unittest.main()
