"""Regressions for the de9c530a EZLynx search / URL-guess loop.

Job de9c530a already named account 220250093. The worker searched, then
enumerated Summary/Details/Index URLs for 36+ minutes. These tests lock the
zip-path helper the Job Engine actually loads.

No live EZLynx. No SSH. No bind.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import build_chat_execution_text, open_chat_job
from robie_job_engine.ezlynx_account_nav import (
    HITL_OPERATOR,
    STUCK_NO_ID,
    after_search_locator_failure,
    direct_account_url,
    extract_known_account_id,
    first_navigation,
    install_account_nav_guard,
    is_account_url_guess,
    is_search_attempt,
    plan_open_applicant,
    refuse_guessed_account_url,
)
from robie_job_engine.playwright_write_guard import install_playwright_write_guards
from robie_job_engine.worker_contract import (
    UNVERIFIED_STUCK_TEXT,
    claims_unverified_destination_progress,
    sanitize_worker_response,
)
from robie_job_engine.store import JobStore


ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "220250093"
POLICIES = f"https://app.ezlynx.com/web/account/{ACCOUNT}/policies"
DE9C530A_PROMPT = (
    "Open EZLynx account 220250093 (ROBIE Test LLC). "
    "The known Policies URL is "
    "https://app.ezlynx.com/web/account/220250093/policies."
)
GUESS_URLS = (
    f"https://app.ezlynx.com/web/account/{ACCOUNT}/summary",
    f"https://app.ezlynx.com/web/account/{ACCOUNT}/details",
    f"https://app.ezlynx.com/web/account/{ACCOUNT}/index",
    f"https://app.ezlynx.com/applicantportal/Applicant/Summary/{ACCOUNT}",
    f"https://app.ezlynx.com/Applicant/Details/{ACCOUNT}",
)
SEARCH_CODE = (
    "page.get_by_placeholder('Search applicants').fill('ROBIE Test LLC')"
)


def _page_class():
    """Fresh Page class so goto wraps do not leak across tests."""

    class FakePage:
        def __init__(self) -> None:
            self.urls: list[str] = []

        def goto(self, url, **_kwargs):
            self.urls.append(str(url))
            return url

    return FakePage


class EzlynxAccountNavTests(unittest.TestCase):
    def test_known_account_id_first_navigation_is_direct_url_not_search(self):
        decision = first_navigation(DE9C530A_PROMPT)
        self.assertEqual(decision.action, "DIRECT")
        self.assertEqual(decision.account_id, ACCOUNT)
        self.assertEqual(decision.url, POLICIES)
        self.assertTrue(decision.url.startswith(f"https://app.ezlynx.com/web/account/{ACCOUNT}/"))
        self.assertFalse(decision.allow_search)
        self.assertFalse(decision.allow_url_guesses)
        self.assertFalse(is_search_attempt(decision.url or ""))

        planned = plan_open_applicant(DE9C530A_PROMPT, playwright_code=SEARCH_CODE)
        self.assertEqual(planned.action, "DIRECT")
        self.assertEqual(planned.url, POLICIES)
        self.assertFalse(planned.allow_search)
        self.assertIn("/web/account/", planned.url)

    def test_search_locator_failure_with_known_id_is_one_direct_fallback(self):
        first = after_search_locator_failure(DE9C530A_PROMPT)
        self.assertEqual(first.action, "DIRECT_FALLBACK")
        self.assertEqual(first.url, POLICIES)
        self.assertEqual(first.max_direct_attempts, 1)
        self.assertFalse(first.allow_url_guesses)

        after_direct = after_search_locator_failure(
            DE9C530A_PROMPT, attempted_urls=[POLICIES]
        )
        self.assertEqual(after_direct.action, "STOP")
        self.assertIsNone(after_direct.url)
        self.assertEqual(after_direct.hitl_operator, HITL_OPERATOR)
        self.assertFalse(after_direct.allow_url_guesses)

        after_guess = after_search_locator_failure(
            DE9C530A_PROMPT, attempted_urls=[GUESS_URLS[0]]
        )
        self.assertEqual(after_guess.action, "STOP")
        self.assertFalse(after_guess.allow_url_guesses)

        for guess in GUESS_URLS:
            with self.assertRaisesRegex(RuntimeError, "PLAYWRIGHT_BLOCKED"):
                refuse_guessed_account_url(guess, known_account_id=ACCOUNT)
            self.assertTrue(is_account_url_guess(guess))

        page_cls = _page_class()
        scope = {"Page": page_cls, "_robie_known_account_id": ACCOUNT}
        install_account_nav_guard(scope)
        live = page_cls()
        live.goto(POLICIES)
        self.assertEqual(live.urls, [POLICIES])
        with self.assertRaisesRegex(RuntimeError, "Do not enumerate"):
            live.goto(GUESS_URLS[1])
        self.assertEqual(live.urls, [POLICIES])
        with self.assertRaisesRegex(RuntimeError, "PLAYWRIGHT_BLOCKED"):
            live.goto(GUESS_URLS[2])
        self.assertEqual(len(live.urls), 1)

    def test_search_locator_failure_without_id_is_hitl_not_url_enumeration(self):
        prompt = "Open the ROBIE Test LLC applicant on EZLynx."
        self.assertIsNone(extract_known_account_id(prompt))
        decision = after_search_locator_failure(prompt)
        self.assertEqual(decision.action, "HITL")
        self.assertIsNone(decision.url)
        self.assertFalse(decision.allow_search)
        self.assertFalse(decision.allow_url_guesses)
        self.assertEqual(decision.hitl_operator, HITL_OPERATOR)
        self.assertIn("HITL Carlo", decision.reason)
        self.assertIn("stuck", decision.reason.casefold())
        self.assertIn("no verified", decision.reason.casefold())
        self.assertIn("stuck", STUCK_NO_ID.casefold())
        for guess in GUESS_URLS:
            with self.assertRaisesRegex(RuntimeError, "HITL Carlo"):
                refuse_guessed_account_url(guess)

    def test_unlabeled_policy_number_is_not_treated_as_account_id(self):
        self.assertIsNone(extract_known_account_id("edit policy 83184565"))
        self.assertEqual(
            extract_known_account_id("account 220250093 and policy 83184565"),
            ACCOUNT,
        )

    def test_payload_account_id_is_enough_without_prompt_label(self):
        decision = first_navigation(
            "continue the commercial auto",
            {"account_id": ACCOUNT},
        )
        self.assertEqual(decision.url, POLICIES)

    def test_direct_url_rejects_guess_page_suffix(self):
        self.assertEqual(direct_account_url(ACCOUNT, "summary"), POLICIES)
        self.assertEqual(
            direct_account_url(ACCOUNT, "overview"),
            f"https://app.ezlynx.com/web/account/{ACCOUNT}/overview",
        )

    def test_execution_contract_names_the_direct_url_when_id_is_known(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(db, "message-de9c530a", DE9C530A_PROMPT)
            execution = build_chat_execution_text(db, job_id, DE9C530A_PROMPT)
            self.assertIn(POLICIES, execution)
            self.assertIn(f"Known EZLynx account id: {ACCOUNT}", execution)
            self.assertIn("Do not search", execution)
            self.assertIn("Do not enumerate Summary, Details, or Index", execution)
            self.assertIn("try the direct /web/account/<id>/ URL once", execution)

    def test_pr22_destination_verified_contract_is_unchanged(self):
        self.assertTrue(
            claims_unverified_destination_progress(
                "I identified DRIVE NJ INS CO and I am Filling Policy Shell."
            )
        )
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(db, "message-pr22", DE9C530A_PROMPT)
            payload = sanitize_worker_response(
                JobStore(db),
                job_id,
                "I filled and saved the policy on 220250093.",
            )
            self.assertTrue(payload["rewritten"])
            self.assertEqual(payload["response_text"], UNVERIFIED_STUCK_TEXT)

    def test_write_guard_install_wraps_goto_and_keeps_unique_write(self):
        scope = {
            "Page": _page_class(),
            "Locator": None,
            "_robie_known_account_id": ACCOUNT,
        }
        patched = install_playwright_write_guards(scope)
        self.assertTrue(scope.get("_robie_unique_write_guard"))
        self.assertTrue(scope.get("_robie_account_nav_guard"))
        self.assertTrue(patched.get("Page.goto"))
        live = scope["Page"]()
        with self.assertRaisesRegex(RuntimeError, "PLAYWRIGHT_BLOCKED"):
            live.goto(GUESS_URLS[0])


class OverlayAndSkillCopyTests(unittest.TestCase):
    def test_commercial_auto_skill_copies_match(self):
        overlay = (
            ROOT / "deploy/hermes/skills/ezlynx-commercial-auto-from-quote/SKILL.md"
        ).read_text()
        repo = (ROOT / "skills/ezlynx-commercial-auto-from-quote/SKILL.md").read_text()
        self.assertEqual(overlay, repo)
        self.assertIn("/web/account/<id>/policies", overlay)
        self.assertIn("Do not guess Summary, Details, or Index", overlay)
        self.assertIn("Never bind", overlay)

    def test_playwright_skill_and_soul_forbid_url_guess_loop(self):
        skill = (
            ROOT / "deploy/hermes/skills/robie-playwright-browser/SKILL.md"
        ).read_text()
        soul = (ROOT / "deploy/hermes/SOUL.playwright.md").read_text()
        for text in (skill, soul):
            self.assertIn("/web/account/<id>/", text)
            self.assertIn("Summary", text)
            self.assertIn("HITL Carlo", text)


if __name__ == "__main__":
    unittest.main()
