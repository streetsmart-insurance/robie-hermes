"""HITL must not claim Gemini resolved a stopped FormEntry mint."""

from __future__ import annotations

import unittest

from robie_job_engine.hitl_escalation import (
    HitlRequest,
    HitlResponse,
    build_hitl_notice,
    escalate,
    gemini_resolved_and_job_continuing,
    ping_carlo,
)


def _request(**overrides) -> HitlRequest:
    payload = dict(
        job_id="e01-diagnostic",
        phase="formentry_mint",
        error="Option 'Personal' not found in Department dropdown",
        page_state={"url": "https://app.ezlynx.com/applicantportal/Policy/Actions/Edit/220250093/83669533"},
        attempted=["field_fill"],
        applicant_id="220250093",
        policy_id="83669533",
        gemini_applied=False,
        formentry_exists=False,
        job_still_running=False,
        save_skipped=True,
        script_or_job_stopped=True,
    )
    payload.update(overrides)
    return HitlRequest(**payload)


class HitlHonestyTests(unittest.TestCase):
    def test_gemini_answered_not_applied_does_not_say_resolved_or_continuing(self) -> None:
        request = _request()
        gemini = HitlResponse(
            source="gemini",
            suggestion="FILE: robie_job_engine/ezlynx_policy_setup.py\nAFTER: pick Department",
            actionable=True,
        )
        notice = build_hitl_notice(request, gemini)
        blob = notice["subject"] + notice["body"] + notice["chat"]
        self.assertNotIn("resolved", blob.casefold())
        self.assertNotIn("job is continuing", blob.casefold())
        self.assertNotIn("job continuing", blob.casefold())
        self.assertIn("Gemini answered", notice["body"])
        self.assertIn("could not apply", notice["body"].casefold())
        self.assertNotIn("PLAYWRIGHT_BLOCKED", notice["body"])
        self.assertNotIn("Job ID:", notice["body"])
        self.assertFalse(gemini_resolved_and_job_continuing(request))

    def test_escalate_gemini_actionable_continues_without_carlo(self) -> None:
        chats: list[str] = []

        def chat_sender(message: str) -> bool:
            chats.append(message)
            return True

        response = escalate(
            _request(gemini_asked=False, gemini_named_option=None, gemini_applied=False),
            {
                "gemini_client": type(
                    "C",
                    (),
                    {
                        "generate_content": lambda self, _p: (
                            "FILE: robie_job_engine/ezlynx_policy_setup.py\n"
                            "AFTER: apply the live Department option"
                        )
                    },
                )(),
                "chat_sender": chat_sender,
                "email_sender": None,
            },
        )
        self.assertTrue(response.actionable)
        self.assertEqual(response.source, "gemini")
        self.assertFalse(response.hitl_posted)
        self.assertEqual(chats, [])

    def test_escalate_gemini_miss_loops_carlo(self) -> None:
        chats: list[str] = []

        def chat_sender(message: str) -> bool:
            chats.append(message)
            return True

        response = escalate(
            _request(gemini_asked=False, gemini_named_option=None, gemini_applied=False),
            {
                "gemini_client": type(
                    "C",
                    (),
                    {"generate_content": lambda self, _p: "UNSURE"},
                )(),
                "chat_sender": chat_sender,
                "email_sender": None,
            },
        )
        self.assertFalse(response.actionable)
        self.assertEqual(response.source, "system")
        self.assertTrue(response.hitl_posted)
        self.assertTrue(chats)
        self.assertNotIn("resolved", chats[0].casefold())
        self.assertNotIn("job is continuing", chats[0].casefold())
        self.assertIn("reply in this chat thread", chats[0].casefold())
        self.assertNotIn("playwright_blocked", chats[0].casefold())

    def test_ping_carlo_does_not_claim_email_when_send_fails(self) -> None:
        def boom(**_kwargs):
            raise RuntimeError("signBlob failed")

        sent, error = ping_carlo(
            _request(),
            {"email_sender": boom, "chat_sender": lambda _m: True},
            HitlResponse(source="gemini", suggestion="use the live Department option", actionable=True),
        )
        self.assertTrue(sent)
        self.assertIn("signBlob", error)

    def test_live_control_match_still_stop_and_ask_never_continuing(self) -> None:
        request = _request(
            gemini_applied=True,
            gemini_named_option="Personal Lines (P/L)",
            live_control_shows="Personal Lines (P/L)",
            formentry_exists=True,
            job_still_running=True,
            script_or_job_stopped=False,
            save_skipped=False,
        )
        notice = build_hitl_notice(
            request,
            HitlResponse(source="gemini", suggestion="picked live option", actionable=True),
        )
        blob = notice["subject"] + notice["body"] + notice["chat"]
        self.assertIn("the page shows", blob.casefold())
        self.assertNotIn("job is continuing", blob.casefold())
        self.assertNotIn("job continuing", blob.casefold())
        self.assertNotIn("playwright_blocked", blob.casefold())
        self.assertTrue(gemini_resolved_and_job_continuing(request))

    def test_named_option_without_apply_is_not_resolved(self) -> None:
        request = _request(
            gemini_applied=False,
            gemini_named_option="Personal Lines (P/L)",
            live_control_shows="",
        )
        notice = build_hitl_notice(request, None)
        blob = notice["subject"] + notice["body"] + notice["chat"]
        self.assertIn("named a live option", blob.casefold())
        self.assertIn("could not apply", blob.casefold())
        self.assertNotIn("resolved", blob.casefold())
        self.assertNotIn("playwright_blocked", blob.casefold())

    def test_chat_failure_is_fail_closed_even_if_email_sends(self) -> None:
        sent, error = ping_carlo(
            _request(),
            {
                "email_sender": lambda **_k: None,
                "chat_sender": lambda _m: False,
            },
        )
        self.assertFalse(sent)
        self.assertIn("chat", error.casefold())

    def test_escalate_gemini_applied_still_running_continues(self) -> None:
        chats: list[str] = []
        response = escalate(
            _request(
                gemini_applied=True,
                gemini_named_option="Direct",
                live_control_shows="Direct",
                formentry_exists=True,
                job_still_running=True,
                script_or_job_stopped=False,
            ),
            {"chat_sender": lambda m: chats.append(m) or True, "email_sender": None},
        )
        self.assertTrue(response.actionable)
        self.assertFalse(response.hitl_posted)
        self.assertEqual(response.source, "gemini")
        self.assertEqual(chats, [])

    def test_escalate_applied_retry_failed_loops_carlo(self) -> None:
        chats: list[str] = []
        response = escalate(
            _request(
                gemini_applied=True,
                gemini_asked=True,
                gemini_named_option="Direct",
                live_control_shows="Direct",
                applied_retry_failed=True,
                script_or_job_stopped=True,
                job_still_running=False,
            ),
            {"chat_sender": lambda m: chats.append(m) or True, "email_sender": None},
        )
        self.assertFalse(response.actionable)
        self.assertTrue(response.hitl_posted)
        self.assertEqual(response.source, "system")
        self.assertTrue(chats)


if __name__ == "__main__":
    unittest.main()
