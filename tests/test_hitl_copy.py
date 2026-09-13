"""Human HITL copy for email and Chat. No PLAYWRIGHT_BLOCKED dumps.

Mocks only. No live EZLynx. No Production. Job 28bff7c8 copy is the sample.
"""

from __future__ import annotations

import unittest

from robie_job_engine.hitl import (
    coverage_fill_miss_hitl_text,
    interaction_for_blocker,
    sanitize_hitl_chat_text,
)
from robie_job_engine.hitl_copy import (
    COVERAGE_AMOUNTS_MISSING_CHAT,
    COVERAGE_AMOUNTS_MISSING_EMAIL,
    COVERAGE_LABELS_EMPTY_EMAIL,
    COVERAGE_TAB_STUCK_EMAIL,
    human_hitl_notice,
    sanitize_plain_text,
    worker_report_human_text,
)
from robie_job_engine.hitl_escalation import HitlRequest, build_hitl_notice

from _sibling_fakes import ensure_real_module

# Unittest discover imports sibling worker tests first; those install a
# fake robie_job_engine.verification_mailer with only send_verification_email.
# Bind the real module so build_plain_email_message exists.
_vm = ensure_real_module("robie_job_engine.verification_mailer")
build_plain_email_message = _vm.build_plain_email_message


class CoverageHitlEmailBodyTests(unittest.TestCase):
    def test_email_body_is_carlo_plain_english_sample(self) -> None:
        text = coverage_fill_miss_hitl_text(
            detail="PLAYWRIGHT_BLOCKED: coverage amounts not on the job; "
            "will not guess coverage amounts",
            channel="email",
        )
        self.assertEqual(text, COVERAGE_AMOUNTS_MISSING_EMAIL)
        self.assertEqual(
            text,
            "I opened the homeowners coverage page.\n"
            "I need the Coverage A, B, C, D, E, and F dollar amounts. "
            "They were not on the email and I will not invent them.\n"
            "Reply to this email with those six numbers, or attach a quote/dec PDF and say RETRY.",
        )
        folded = text.casefold()
        self.assertNotIn("playwright_blocked", folded)
        self.assertNotIn("chat thread", folded)
        self.assertNotIn("gemini did not handle", folded)
        self.assertNotIn("job id:", folded)
        self.assertNotIn("phase:", folded)
        self.assertNotIn("error:", folded)

    def test_chat_body_says_chat_thread_not_email(self) -> None:
        text = coverage_fill_miss_hitl_text(
            detail="coverage amounts not on the job",
            channel="chat",
        )
        self.assertEqual(text, COVERAGE_AMOUNTS_MISSING_CHAT)
        self.assertIn("Reply in this Chat thread", text)
        self.assertNotIn("Reply to this email", text)
        self.assertNotIn("PLAYWRIGHT_BLOCKED", text)

    def test_notice_email_channel_never_says_chat(self) -> None:
        notice = build_hitl_notice(
            HitlRequest(
                job_id="28bff7c8",
                phase="coverage_fill",
                error="PLAYWRIGHT_BLOCKED: coverage amounts not on the job; "
                "will not guess coverage amounts",
                page_state={},
                attempted=["coverage_fill"],
                applicant_id="220250093",
                policy_id="83669533",
                formentry_exists=True,
                unguessable=True,
                channel="email",
            )
        )
        self.assertEqual(notice["body"], COVERAGE_AMOUNTS_MISSING_EMAIL)
        self.assertNotIn("Chat thread", notice["body"])
        self.assertNotIn("Chat thread", notice["subject"])
        self.assertIn("Reply in this Chat thread", notice["chat"])
        self.assertNotIn("PLAYWRIGHT_BLOCKED", notice["body"])
        self.assertNotIn("Gemini did not handle this", notice["body"])
        self.assertNotIn("Gemini suggestion", notice["body"])

    def test_notice_chat_channel_says_chat_thread(self) -> None:
        notice = human_hitl_notice(
            HitlRequest(
                job_id="28bff7c8",
                phase="coverage_fill",
                error="coverage amounts not on the job",
                page_state={},
                attempted=[],
                applicant_id="220250093",
                unguessable=True,
                channel="chat",
            )
        )
        self.assertEqual(notice["chat"], COVERAGE_AMOUNTS_MISSING_CHAT)
        self.assertIn("Reply in this Chat thread", notice["chat"])

    def test_any_channel_email_body_never_says_chat_thread(self) -> None:
        notice = build_hitl_notice(
            HitlRequest(
                job_id="28bff7c8",
                phase="coverage_fill",
                error="coverage amounts not on the job",
                page_state={},
                attempted=[],
                applicant_id="220250093",
                unguessable=True,
                channel="any",
            )
        )
        self.assertEqual(notice["body"], COVERAGE_AMOUNTS_MISSING_EMAIL)
        self.assertNotIn("Chat thread", notice["body"])
        self.assertIn("Reply to this email", notice["body"])
        self.assertIn("Reply in this Chat thread", notice["chat"])


class GenericHumanHitlTests(unittest.TestCase):
    def test_chat_blocker_is_plain_english(self) -> None:
        state = interaction_for_blocker(
            "PLAYWRIGHT_BLOCKED: submit control not found",
            action_type="ezlynx.submission_audit",
            channel="chat",
        )
        prompt = state["prompt"]
        self.assertIn("Reply in this Chat thread", prompt)
        self.assertNotIn("PLAYWRIGHT_BLOCKED", prompt)
        self.assertNotIn("Job ID:", prompt)

    def test_generic_notice_email_and_chat_use_different_reply_lines(self) -> None:
        notice = human_hitl_notice(
            HitlRequest(
                job_id="abc",
                phase="field_fill",
                error="PLAYWRIGHT_BLOCKED: submit control not found",
                page_state={},
                attempted=[],
                applicant_id="220250093",
                channel="email",
            )
        )
        self.assertIn("Reply to this email", notice["body"])
        self.assertNotIn("Chat thread", notice["body"])
        self.assertIn("Reply in this Chat thread", notice["chat"])
        self.assertNotIn("PLAYWRIGHT_BLOCKED", notice["body"])
        self.assertNotIn("Job ID:", notice["body"])

    def test_email_blocker_says_reply_to_this_email(self) -> None:
        state = interaction_for_blocker(
            "PLAYWRIGHT_BLOCKED: submit control not found",
            action_type="ezlynx.submission_audit",
            channel="email",
        )
        self.assertIn("Reply to this email", state["prompt"])
        self.assertNotIn("Chat thread", state["prompt"])

    def test_cowboy_rewrite_is_human_not_jargon(self) -> None:
        cowboy = "Listen up, Jake! it ain't my fault I cannot open /artifacts/quote.pdf"
        rewritten = sanitize_hitl_chat_text(cowboy)
        self.assertNotIn("Listen up", rewritten)
        self.assertNotIn("ain't", rewritten)
        self.assertNotIn("PLAYWRIGHT_BLOCKED", rewritten)
        self.assertIn("RETRY", rewritten)


class PlainEmailSanitizerTests(unittest.TestCase):
    def test_strips_zwsp_nbsp_soft_hyphen_and_html(self) -> None:
        dirty = "I\u200b need\u00a0Coverage\u00ad A <b>now</b>"
        clean = sanitize_plain_text(dirty)
        self.assertEqual(clean, "I need Coverage A now")
        self.assertNotIn("\u200b", clean)
        self.assertNotIn("\u00a0", clean)
        self.assertNotIn("\u00ad", clean)
        self.assertNotIn("<b>", clean)

    def test_does_not_smash_letters(self) -> None:
        clean = sanitize_plain_text("Coverage A  $1,200,000")
        self.assertEqual(clean, "Coverage A $1,200,000")
        self.assertIn("Coverage", clean)

    def test_mailer_is_text_plain_8bit_no_html(self) -> None:
        message = build_plain_email_message(
            sender="robie@streetsmart.insurance",
            to=["carlo@streetsmart.insurance"],
            cc=[],
            subject="[ROBIE HITL] Job 28bff7c8 stuck at coverage_fill",
            text_body=COVERAGE_AMOUNTS_MISSING_EMAIL,
            html_body="<p style='letter-spacing:0.4em'>smashed</p>",
            plain_only=True,
        )
        raw = message.as_string()
        self.assertIn("text/plain", raw)
        self.assertIn("charset=\"utf-8\"", raw.casefold())
        self.assertNotIn("quoted-printable", raw.casefold())
        self.assertNotIn("letter-spacing", raw)
        self.assertNotIn("<p", raw)
        self.assertIn("I opened the homeowners coverage page.", raw)
        cte = str(message.get("Content-Transfer-Encoding") or "").casefold()
        self.assertIn(cte, {"8bit", "7bit", ""})


class WorkerReportEmailTests(unittest.TestCase):
    def test_playwright_blocked_is_not_the_lead(self) -> None:
        text = worker_report_human_text(
            "PLAYWRIGHT_BLOCKED: no coverage labels were filled after "
            "Gemini apply + retry. Looked for ['Dwelling', 'Other Structures']",
            channel="email",
        )
        self.assertEqual(text, COVERAGE_LABELS_EMPTY_EMAIL)
        self.assertFalse(text.casefold().startswith("playwright_blocked"))
        self.assertIn("Reply to this email", text)
        self.assertNotIn("Chat thread", text)
        lines = text.split("\n")
        self.assertGreaterEqual(len(lines), 2)
        self.assertTrue(all(len(line) <= 160 for line in lines))

    def test_address_label_miss_does_not_say_amounts_were_not_on_the_email(
        self,
    ) -> None:
        text = worker_report_human_text(
            "PLAYWRIGHT_BLOCKED: no coverage labels were filled after "
            "Gemini apply + retry. Looked for ['Address']. "
            "live_labels=['Name', 'Address', 'City', 'State', 'Zip', "
            "'Country', 'Location #']. HITL posted to the email. STOP AND ASK.",
            channel="email",
        )
        self.assertEqual(text, COVERAGE_TAB_STUCK_EMAIL)
        self.assertNotIn("They were not on the email", text)
        self.assertIn("I am on the address tab and cannot open Coverages", text)
        self.assertNotIn("I need the Coverage A", text)
        self.assertIn("Reply to this email", text)
        self.assertNotIn("Chat thread", text)

    def test_worker_report_mail_is_text_plain_8bit(self) -> None:
        body = worker_report_human_text(
            "PLAYWRIGHT_BLOCKED: no coverage labels were filled after Gemini apply + retry.",
            channel="email",
        )
        message = build_plain_email_message(
            sender="Robie AI <robie@streetsmart.insurance>",
            to=["carlo@streetsmart.insurance"],
            cc=[],
            subject="Re: Set up homeowners policy TEST-HO-20260911-E01",
            text_body=body,
            plain_only=True,
        )
        raw = message.as_string()
        self.assertIn("text/plain", raw)
        self.assertNotIn("quoted-printable", raw.casefold())
        self.assertNotIn("letter-spacing", raw)
        self.assertFalse("PLAYWRIGHT_BLOCKED" in raw.split("\n\n", 1)[-1][:40])
        cte = str(message.get("Content-Transfer-Encoding") or "").casefold()
        self.assertIn(cte, {"8bit", "7bit", ""})


if __name__ == "__main__":
    unittest.main()
