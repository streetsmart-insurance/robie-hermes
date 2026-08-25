from __future__ import annotations

import unittest
from pathlib import Path

from robie_job_engine.submission_audit_runner import _authenticated


class _CountLocator:
    def __init__(self, count: int) -> None:
        self._count = count

    def count(self) -> int:
        return self._count


class _Page:
    def __init__(self, url: str, *, login_controls: int = 0, internal_links: int = 0) -> None:
        self.url = url
        self.login_controls = login_controls
        self.internal_links = internal_links

    def locator(self, selector: str) -> _CountLocator:
        if "txtUserName" in selector:
            return _CountLocator(self.login_controls)
        if "href" in selector:
            return _CountLocator(self.internal_links)
        raise AssertionError(f"unexpected selector: {selector}")


class EzlynxAuthEvidenceTests(unittest.TestCase):
    def test_arbitrary_app_url_is_not_authenticated_evidence(self):
        page = _Page("https://app.ezlynx.com/account", internal_links=3)
        self.assertFalse(_authenticated(page))

    def test_web_shell_requires_internal_navigation_evidence(self):
        page = _Page("https://app.ezlynx.com/web/submission-center/overview/submissions")
        self.assertFalse(_authenticated(page))

    def test_visible_login_controls_fail_closed(self):
        page = _Page(
            "https://app.ezlynx.com/web/submission-center/overview/submissions",
            login_controls=1,
            internal_links=3,
        )
        self.assertFalse(_authenticated(page))

    def test_fresh_internal_web_navigation_is_authenticated_evidence(self):
        page = _Page(
            "https://app.ezlynx.com/web/submission-center/overview/submissions",
            internal_links=3,
        )
        self.assertTrue(_authenticated(page))

    def test_login_helper_uses_the_same_fail_closed_evidence_contract(self):
        helper = (Path(__file__).resolve().parents[1] / "ezlynx_login_bootstrap.py").read_text()
        self.assertIn('AUTHENTICATED_APP_PREFIX = "https://app.ezlynx.com/web/"', helper)
        self.assertIn('LOGIN_CONTROL_SELECTOR = "#txtUserName, #txtPassword, #btnLogin"', helper)
        self.assertIn("internal_links > 0", helper)
        self.assertIn("login_controls == 0", helper)


if __name__ == "__main__":
    unittest.main()
