"""Carlo asked that HITL Chat/email read like a coworker, not an engineer.

Job hitl-carlo-send-20260913-0703 reached him, but the lead was
``ROBIE HITL: Job uuid stuck at phase`` plus a selector-strategy list.
"""

from __future__ import annotations

import unittest

from robie_job_engine.hitl_escalation import (
    HitlRequest,
    build_hitl_chat,
    build_hitl_email,
    escalate,
    ping_carlo,
)


JOB_ID = "hitl-carlo-send-20260913-0703"
APPLICANT = "220250093"
POLICY = "83669533"
STALE_LEAD = "ROBIE HITL:"
STALE_PHASE_LEAD = f"Job {JOB_ID} stuck at"


def _request(**overrides) -> HitlRequest:
    payload = {
        "job_id": JOB_ID,
        "phase": "formentry_mint",
        "error": "Billing Type option 'Direct Bill' not found",
        "page_state": {
            "url": (
                "https://app.ezlynx.com/applicantportal/Policy/Actions/Edit/"
                f"{APPLICANT}/{POLICY}"
            ),
            "title": "Edit Policy",
            "buttons": ["Save & Continue Edit", "Cancel"],
        },
        "attempted": ["field_fill", "save_and_continue_edit"],
        "applicant_id": APPLICANT,
        "policy_id": POLICY,
    }
    payload.update(overrides)
    return HitlRequest(**payload)


class HitlPlainEnglishCopyTests(unittest.TestCase):
    def test_chat_and_email_read_like_a_coworker(self) -> None:
        request = _request()
        chat = build_hitl_chat(request)
        subject, body = build_hitl_email(request)

        self.assertTrue(chat.startswith("Hey Carlo — Robie needs a hand."))
        self.assertTrue(body.startswith("Hey Carlo — Robie needs a hand."))
        self.assertTrue(subject.startswith("Robie needs a hand"))

        self.assertNotIn(STALE_LEAD, chat)
        self.assertNotIn(STALE_LEAD, subject)
        self.assertNotIn(STALE_LEAD, body)
        self.assertNotIn(STALE_PHASE_LEAD, chat.splitlines()[0])
        self.assertNotIn("stuck at formentry_mint", subject)
        self.assertNotIn("stuck at formentry_mint", chat.splitlines()[0])
        self.assertFalse(chat.startswith("🚨"))

        self.assertIn("coverage form", chat)
        self.assertIn("limits and deductibles", chat)
        self.assertIn("What went wrong: Billing Type option 'Direct Bill' not found", chat)
        self.assertIn("I already tried", chat)
        self.assertIn(f"Applicant {APPLICANT}", chat)
        self.assertIn(f"Policy {POLICY}", chat)
        self.assertIn("SKIP", chat)
        self.assertIn("ABORT", chat)
        self.assertIn("30 minutes", chat)
        self.assertIn(JOB_ID, chat)
        # Job id and phase are not the lead.
        self.assertGreater(chat.find(JOB_ID), 20)
        self.assertNotIn("formentry_mint", chat.splitlines()[0])

        self.assertIn(APPLICANT, subject)
        self.assertIn("coverage form", body)
        self.assertIn("What went wrong: Billing Type option 'Direct Bill' not found", body)
        self.assertIn("filling the required policy fields", body)
        self.assertIn("clicking Save & Continue Edit", body)
        self.assertIn(f"Applicant {APPLICANT}", body)
        self.assertIn(f"Policy {POLICY}", body)
        self.assertIn("SKIP", body)
        self.assertIn("ABORT", body)
        self.assertIn("30 minutes", body)
        self.assertIn(JOB_ID, body)
        self.assertNotIn("Phase:", body)

    def test_selector_strategy_list_is_not_the_lead(self) -> None:
        request = _request(
            attempted=["css_selector", "xpath", "text_content", "role_button"],
        )
        chat = build_hitl_chat(request)
        _subject, body = build_hitl_email(request)
        first_line = chat.splitlines()[0]
        self.assertNotIn("css_selector", first_line)
        self.assertNotIn("xpath", first_line)
        self.assertNotIn("Tried: css_selector, xpath", chat)
        self.assertIn("several ways to find the control on the page", chat)
        self.assertIn("several ways to find the control on the page", body)

    def test_ping_carlo_send_wiring_and_recipients_unchanged(self) -> None:
        emails: list[dict] = []
        chats: list[str] = []

        def email_sender(*, to: str, subject: str, body: str) -> None:
            emails.append({"to": to, "subject": subject, "body": body})

        def chat_sender(msg: str) -> bool:
            chats.append(msg)
            return True

        request = _request(original_requester="jake@streetsmart.insurance")
        sent, error = ping_carlo(
            request,
            {"email_sender": email_sender, "chat_sender": chat_sender},
        )
        self.assertTrue(sent)
        self.assertEqual(error, "")
        self.assertEqual(
            [item["to"] for item in emails],
            ["carlo@streetsmart.insurance", "jake@streetsmart.insurance"],
        )
        self.assertEqual(len(chats), 1)
        self.assertTrue(chats[0].startswith("Hey Carlo — Robie needs a hand."))
        self.assertTrue(emails[0]["subject"].startswith("Robie needs a hand"))
        self.assertNotIn(STALE_LEAD, chats[0])
        self.assertNotIn(STALE_LEAD, emails[0]["subject"])

    def test_escalate_still_asks_gemini_first_and_skips_ping(self) -> None:
        pings: list[str] = []

        class _Gemini:
            def generate_content(self, _prompt: str) -> str:
                return "Click the Actions dropdown first"

        def email_sender(**_kwargs) -> None:
            pings.append("email")

        def chat_sender(_msg: str) -> bool:
            pings.append("chat")
            return True

        response = escalate(
            _request(),
            {
                "gemini_client": _Gemini(),
                "email_sender": email_sender,
                "chat_sender": chat_sender,
            },
        )
        self.assertEqual(response.source, "gemini")
        self.assertTrue(response.actionable)
        self.assertEqual(pings, [])

    def test_escalate_pings_carlo_with_plain_english_after_gemini_unsure(self) -> None:
        chats: list[str] = []

        class _Unsure:
            def generate_content(self, _prompt: str) -> str:
                return "UNSURE"

        def chat_sender(msg: str) -> bool:
            chats.append(msg)
            return True

        response = escalate(
            _request(),
            {
                "gemini_client": _Unsure(),
                "chat_sender": chat_sender,
                "email_checker": None,
                "hitl_timeout": 0,
            },
        )
        self.assertEqual(len(chats), 1)
        self.assertTrue(chats[0].startswith("Hey Carlo — Robie needs a hand."))
        self.assertNotIn(STALE_LEAD, chats[0])
        self.assertFalse(response.actionable)
        self.assertEqual(response.source, "system")


if __name__ == "__main__":
    unittest.main()
