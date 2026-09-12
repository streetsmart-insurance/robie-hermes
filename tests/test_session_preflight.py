#!/usr/bin/env python3
"""Tests for session_preflight. stdlib unittest — no pytest on the Mac shell.

The payloads below are the real CDP /json/list output captured from
hermes-poc-01 on 2026-09-12, including the two `browser_ui` omnibox entries
that made "the browser has tabs" look true while it showed nothing.
"""
from __future__ import annotations

import unittest

from robie_job_engine.session_preflight import (
    LOGGED_OUT,
    NO_EZLYNX_TAB,
    NO_TABS,
    SESSION_PRESENT,
    UNREACHABLE,
    check,
    classify,
)

LOGIN_TAB = {
    "id": "E7FD9BADD6E996313D607D2BD08D00DD",
    "title": "Login",
    "type": "page",
    "url": "https://app.ezlynx.com/auth/account/login?redirectURL=https%3A%2F%2Fapp.ezlynx.com%2Fweb%2F",
    "faviconUrl": "https://app.ezlynx.com/web/assets/ezlynx/favicon.svg",
}

OMNIBOX_A = {
    "id": "E141DAE7E061001EB0CD6E1037674242",
    "title": "Omnibox Popup",
    "type": "browser_ui",
    "url": "chrome://omnibox-popup.top-chrome/",
}

OMNIBOX_B = {
    "id": "BBF89891F30EFE927FB348284730702F",
    "title": "Omnibox Popup",
    "type": "browser_ui",
    "url": "chrome://omnibox-popup.top-chrome/omnibox_popup_aim.html",
}

POLICIES_TAB = {
    "id": "AAAA",
    "title": "ROBIE Test LLC - Policies",
    "type": "page",
    "url": "https://app.ezlynx.com/web/account/220250093/policies",
}


class TestClassifyLoggedOut(unittest.TestCase):
    def test_real_production_payload_is_logged_out(self):
        result = classify([LOGIN_TAB, OMNIBOX_A, OMNIBOX_B])
        self.assertEqual(result["state"], LOGGED_OUT)
        self.assertTrue(result["blocking"])

    def test_blocker_reason_names_the_condition_not_a_symptom(self):
        result = classify([LOGIN_TAB])
        self.assertIn("SESSION_LOGGED_OUT", result["reason"])
        self.assertIn("Secret Manager", result["reason"])
        self.assertNotIn("DISPLAY", result["reason"])

    def test_evidence_carries_the_login_url(self):
        result = classify([LOGIN_TAB, OMNIBOX_A])
        self.assertEqual(len(result["login_urls"]), 1)
        self.assertIn("/auth/account/login", result["login_urls"][0])

    def test_login_detected_from_path_even_when_title_differs(self):
        tab = dict(LOGIN_TAB, title="EZLynx")
        self.assertEqual(classify([tab])["state"], LOGGED_OUT)

    def test_login_detected_from_title_when_host_matches(self):
        tab = {"type": "page", "title": "Login", "url": "https://app.ezlynx.com/web/"}
        self.assertEqual(classify([tab])["state"], LOGGED_OUT)

    def test_login_titled_page_on_another_host_is_not_our_login(self):
        tab = {"type": "page", "title": "Login", "url": "https://accounts.google.com/signin"}
        self.assertEqual(classify([tab])["state"], NO_EZLYNX_TAB)


class TestClassifyNonBlocking(unittest.TestCase):
    def test_authenticated_page_is_session_present(self):
        result = classify([POLICIES_TAB, OMNIBOX_A])
        self.assertEqual(result["state"], SESSION_PRESENT)
        self.assertFalse(result["blocking"])

    def test_session_present_does_not_claim_proof(self):
        result = classify([POLICIES_TAB])
        self.assertIn("not proof", result["reason"])

    def test_one_live_tab_beside_a_login_tab_does_not_block(self):
        # A second window mid-login should not kill a job that has a working tab.
        result = classify([LOGIN_TAB, POLICIES_TAB])
        self.assertEqual(result["state"], SESSION_PRESENT)
        self.assertFalse(result["blocking"])

    def test_browser_ui_entries_alone_count_as_no_tabs(self):
        result = classify([OMNIBOX_A, OMNIBOX_B])
        self.assertEqual(result["state"], NO_TABS)
        self.assertFalse(result["blocking"])

    def test_empty_list_is_no_tabs(self):
        self.assertEqual(classify([])["state"], NO_TABS)

    def test_non_ezlynx_page_is_not_blocking(self):
        tab = {"type": "page", "title": "New Tab", "url": "about:blank"}
        result = classify([tab])
        self.assertEqual(result["state"], NO_EZLYNX_TAB)
        self.assertFalse(result["blocking"])

    def test_malformed_entries_are_ignored_not_fatal(self):
        result = classify([None, "junk", 7, LOGIN_TAB])
        self.assertEqual(result["state"], LOGGED_OUT)


class TestCheckNeverRaises(unittest.TestCase):
    def test_unreachable_cdp_is_reported_not_raised(self):
        # Port 1 is reserved and never listening.
        result = check("http://127.0.0.1:1", timeout=0.25)
        self.assertEqual(result["state"], UNREACHABLE)

    def test_unreachable_cdp_does_not_block(self):
        # A dead CDP endpoint is a different failure with a different fix.
        # Calling it "logged out" sends the next person to the wrong place.
        result = check("http://127.0.0.1:1", timeout=0.25)
        self.assertFalse(result["blocking"])

    def test_unreachable_reason_names_the_endpoint(self):
        result = check("http://127.0.0.1:1", timeout=0.25)
        self.assertIn("127.0.0.1:1", result["reason"])


if __name__ == "__main__":
    unittest.main()
