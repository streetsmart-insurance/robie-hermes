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
        self.assertIn("Nothing was applied", notice["body"])
        self.assertIn("Save was skipped", notice["body"])
        self.assertIn("stopped", notice["body"].casefold())
        self.assertFalse(gemini_resolved_and_job_continuing(request))

    def test_escalate_one_shot_does_not_return_gemini_as_continuing(self) -> None:
        chats: list[str] = []

        def chat_sender(message: str) -> bool:
            chats.append(message)
            return True

        response = escalate(
            _request(),
            {
                "gemini_client": type(
                    "C",
                    (),
                    {"generate_content": lambda self, _p: "truncated suggestion only"},
                )(),
                "chat_sender": chat_sender,
                "email_sender": None,
            },
        )
        self.assertFalse(response.actionable)
        self.assertEqual(response.source, "system")
        self.assertTrue(chats)
        self.assertNotIn("resolved", chats[0].casefold())
        self.assertNotIn("continuing", chats[0].casefold())
        self.assertIn("Nothing was applied", chats[0])

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
        self.assertIn("live control shows", blob.casefold())
        self.assertIn("stop and ask", blob.casefold())
        self.assertNotIn("job is continuing", blob.casefold())
        self.assertNotIn("job continuing", blob.casefold())
        self.assertFalse(gemini_resolved_and_job_continuing(request))

    def test_named_option_without_apply_is_not_resolved(self) -> None:
        request = _request(
            gemini_applied=False,
            gemini_named_option="Personal Lines (P/L)",
            live_control_shows="",
        )
        notice = build_hitl_notice(request, None)
        blob = notice["subject"] + notice["body"] + notice["chat"]
        self.assertIn("named a live option", blob.casefold())
        self.assertIn("nothing was applied", blob.casefold())
        self.assertNotIn("resolved", blob.casefold())
        self.assertIn("stop and ask", blob.casefold())

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

    def test_escalate_never_returns_actionable_continue(self) -> None:
        response = escalate(
            _request(
                gemini_applied=True,
                gemini_named_option="Direct",
                live_control_shows="Direct",
                formentry_exists=True,
                job_still_running=True,
                script_or_job_stopped=False,
            ),
            {"chat_sender": lambda _m: True, "email_sender": None},
        )
        self.assertFalse(response.actionable)
        self.assertTrue(response.hitl_posted)
        self.assertEqual(response.source, "system")


if __name__ == "__main__":
    unittest.main()
