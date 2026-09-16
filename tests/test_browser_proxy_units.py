"""Regression test: browser systemd units support optional proxy routing.

Carrier portals (e.g. AmTrust's F5 wall) bot-block datacenter egress IPs.
The persistent Chrome units accept an optional env file
/etc/streetsmart-hermes/robie-browser-proxy.env defining PROXY_CHROME_FLAGS
(e.g. --proxy-server=http://proxy.webshare.io:8080, with Webshare IP-auth so
no proxy credentials live on the box). The leading '-' on EnvironmentFile
keeps the file optional: without it Chrome launches exactly as before.

ExecStart runs through ``/bin/sh -c 'exec ...'`` with the flags referenced as
``$$PROXY_CHROME_FLAGS`` (the doubled dollar defers expansion to the shell).
This is load-bearing, not cosmetic: systemd does NOT do shell word-splitting,
so a bare ``${PROXY_CHROME_FLAGS}`` in ExecStart expands to an EMPTY argv entry
when the env file is absent — and Chrome exits 13 on that empty argument.
The shell drops an empty/unset variable under word-splitting instead, and
``exec`` keeps chrome as the main PID so signal handling is unchanged.

This test pins that contract so a future unit edit cannot silently drop the
proxy wiring, reintroduce a bare ${...} expansion, or hardcode proxy
credentials into the repo. Stock headless units exec google-chrome; the
Production Xvfb unit (hermes-poc-01, User=carlo_streetsmart_insurance)
execs xvfb-run — both must use the same ``/bin/sh -c 'exec`` wrapper.
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

STOCK_BROWSER_UNITS = [
    "deploy/systemd/robie-ezlynx-browser.service",
    "deploy/systemd/robie-ezlynx-browser-test.service",
    "deploy/systemd/robie-magellan-browser.service",
    "robie-ezlynx-browser.service",
]
# Production hermes-poc-01 runs this Xvfb unit (User=carlo_streetsmart_insurance);
# stock headless cannot open that mode-700 profile.
XVFB_BROWSER_UNITS = [
    "deploy/systemd/robie-ezlynx-browser-xvfb.service",
]
BROWSER_UNITS = STOCK_BROWSER_UNITS + XVFB_BROWSER_UNITS

ENV_FILE_LINE = "EnvironmentFile=-/etc/streetsmart-hermes/robie-browser-proxy.env"
# Doubled dollar: deferred to the shell inside /bin/sh -c 'exec ...'.
DEFERRED_FLAGS_VAR = "$$PROXY_CHROME_FLAGS"
# Bare systemd expansion: forbidden in ExecStart (empty argv entry -> exit 13).
BARE_FLAGS_VAR = "${PROXY_CHROME_FLAGS}"
# Shared prefix: stock units exec chrome; the Xvfb unit execs xvfb-run.
SHELL_EXEC_PREFIX = "/bin/sh -c 'exec"
STOCK_CHROME_BIN = "exec /usr/bin/google-chrome"
XVFB_BIN = "exec /usr/bin/xvfb-run"


class BrowserProxyUnitTests(unittest.TestCase):
    def _read(self, rel):
        path = REPO_ROOT / rel
        self.assertTrue(path.is_file(), f"browser unit missing: {rel}")
        return path.read_text()

    def _exec_lines(self, text, rel):
        exec_lines = [
            line for line in text.splitlines() if line.startswith("ExecStart=")
        ]
        self.assertTrue(exec_lines, f"{rel} has no ExecStart line")
        return exec_lines

    def test_units_reference_optional_proxy_env_file(self):
        for rel in BROWSER_UNITS:
            with self.subTest(unit=rel):
                text = self._read(rel)
                self.assertIn(
                    ENV_FILE_LINE,
                    text,
                    f"{rel} must source the optional proxy env file",
                )

    def test_execstart_defers_proxy_flags_to_shell(self):
        for rel in BROWSER_UNITS:
            with self.subTest(unit=rel):
                text = self._read(rel)
                for line in self._exec_lines(text, rel):
                    self.assertIn(
                        DEFERRED_FLAGS_VAR,
                        line,
                        f"{rel} ExecStart must inject {DEFERRED_FLAGS_VAR} "
                        "(shell-deferred, so empty vanishes)",
                    )

    def test_execstart_has_no_bare_systemd_flag_expansion(self):
        # Regression: a bare ${PROXY_CHROME_FLAGS} in ExecStart expands to an
        # empty argv entry when the env file is absent, and Chrome exits 13.
        for rel in BROWSER_UNITS:
            with self.subTest(unit=rel):
                text = self._read(rel)
                for line in self._exec_lines(text, rel):
                    self.assertNotIn(
                        BARE_FLAGS_VAR,
                        line,
                        f"{rel} ExecStart must not contain a bare "
                        f"{BARE_FLAGS_VAR} expansion (Chrome exit 13)",
                    )

    def test_execstart_wrapped_in_shell_exec(self):
        # The sh -c 'exec ...' wrapper is what makes the empty expansion safe.
        # Stock units exec chrome; the Production Xvfb unit execs xvfb-run.
        for rel in BROWSER_UNITS:
            with self.subTest(unit=rel):
                text = self._read(rel)
                for line in self._exec_lines(text, rel):
                    self.assertTrue(
                        line.startswith(f"ExecStart={SHELL_EXEC_PREFIX}"),
                        f"{rel} ExecStart must start with "
                        f"ExecStart={SHELL_EXEC_PREFIX}",
                    )

    def test_execstart_binary_matches_unit_kind(self):
        for rel in STOCK_BROWSER_UNITS:
            with self.subTest(unit=rel):
                text = self._read(rel)
                for line in self._exec_lines(text, rel):
                    self.assertIn(
                        STOCK_CHROME_BIN,
                        line,
                        f"{rel} stock ExecStart must {STOCK_CHROME_BIN}",
                    )
        for rel in XVFB_BROWSER_UNITS:
            with self.subTest(unit=rel):
                text = self._read(rel)
                for line in self._exec_lines(text, rel):
                    self.assertIn(
                        XVFB_BIN,
                        line,
                        f"{rel} Xvfb ExecStart must {XVFB_BIN}",
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
