"""Standalone synthetic HITL isolation — no live EZLynx, no GCP, no Chrome.

Confirms email + Chat HITL both land on a forced failure. This is the
cloud-agent substitute for ``scripts/e01_diagnostic.py --mode=hitl-test``,
which is hardcoded to ``/opt/streetsmart-hermes/releases/current/robie-main2``
and must not be SSH-run from this agent.

Applicant / policy strings below are labels only. Nothing is written.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from robie_job_engine.chat_app_post import post_hitl_to_originating_thread
from robie_job_engine.hitl_escalation import HitlRequest, escalate, ping_carlo


def _synthetic_failure(**overrides) -> HitlRequest:
    payload = dict(
        job_id="hitl-iso-synthetic",
        phase="hitl_isolation_test",
        error=(
            "FORCED TEST FAILURE: bad selector "
            "'.nonexistent-button-xyz' not found after 5 strategies"
        ),
        page_state={
            "url": "https://example.test/synthetic-hitl-isolation",
            "title": "Synthetic HITL isolation (no EZLynx)",
            "buttons": ["Save & Continue Edit", "Cancel"],
        },
        attempted=["css_selector", "xpath", "text_content", "role_button"],
        applicant_id="synthetic-hitl-iso",
        policy_id="0",
        notify_carlo=True,
        notify_requester=False,
        formentry_exists=False,
        job_still_running=False,
        save_skipped=True,
        script_or_job_stopped=True,
    )
    payload.update(overrides)
    return HitlRequest(**payload)


class SyntheticHitlIsolationTests(unittest.TestCase):
    def test_escalate_invokes_email_and_chat_on_synthetic_failure(self) -> None:
        emails: list[dict[str, str]] = []
        chats: list[str] = []

        def email_sender(*, to: str, subject: str, body: str) -> None:
            emails.append({"to": to, "subject": subject, "body": body})

        def chat_sender(message: str) -> bool:
            chats.append(message)
            return True

        response = escalate(
            _synthetic_failure(),
            {"email_sender": email_sender, "chat_sender": chat_sender},
        )

        self.assertEqual(response.source, "system")
        self.assertFalse(response.actionable)
        self.assertTrue(response.hitl_posted)
        self.assertEqual(len(emails), 1)
        self.assertEqual(emails[0]["to"], "carlo@streetsmart.insurance")
        self.assertIn("[ROBIE HITL]", emails[0]["subject"])
        self.assertIn("FORCED TEST FAILURE", emails[0]["body"])
        self.assertEqual(len(chats), 1)
        self.assertIn("ROBIE HITL", chats[0])
        self.assertIn("FORCED TEST FAILURE", chats[0])
        self.assertIn("STOP AND ASK", chats[0])

    def test_chat_originating_thread_lands_without_email(self) -> None:
        posted: list[tuple[str, str | None]] = []

        def poster(space, message, thread_name=None):
            posted.append((space, thread_name))
            self.assertIn("ROBIE HITL", message)
            return {"name": "spaces/SYNTH/messages/1"}

        store = MagicMock()
        store.get_job.return_value = {
            "id": "hitl-iso-synthetic",
            "action_type": "hermes.google_chat_task",
            "payload": {
                "conversation_id": "spaces/SYNTH",
                "thread_name": "spaces/SYNTH/threads/t1",
            },
        }
        ok = post_hitl_to_originating_thread(
            "ROBIE HITL: synthetic Chat isolation",
            job_id="hitl-iso-synthetic",
            store=store,
            poster=poster,
        )
        self.assertTrue(ok)
        self.assertEqual(posted, [("spaces/SYNTH", "spaces/SYNTH/threads/t1")])

    def test_email_job_without_thread_does_not_fake_chat_hitl(self) -> None:
        """Live email job c75aab5c: no conversation_id → Chat HITL cannot land.

        Email then falls through to signBlob. Isolation here proves the Chat
        miss is a missing thread, not a send. No Gmail call is made.
        """
        store = MagicMock()
        store.get_job.return_value = {
            "id": "hitl-iso-email",
            "action_type": "hermes.email_task",
            "payload": {},
        }
        self.assertFalse(
            post_hitl_to_originating_thread(
                "ROBIE HITL: synthetic email isolation",
                job_id="hitl-iso-email",
                store=store,
                poster=lambda *_a, **_k: {"name": "x"},
            )
        )

    def test_empty_deps_do_not_claim_hitl_posted(self) -> None:
        """e01_diagnostic --mode=hitl-test calls escalate(request, {})."""
        response = escalate(_synthetic_failure(), {})
        self.assertFalse(response.hitl_posted)
        self.assertFalse(response.actionable)
        self.assertIn("HITL posted=false", response.suggestion)
        self.assertIn("no chat_sender", response.suggestion)

    def test_signblob_style_email_failure_does_not_count_as_hitl_post(self) -> None:
        chats: list[str] = []

        def boom(**_kwargs):
            raise RuntimeError(
                "403 Forbidden: iam.serviceAccounts.signBlob denied on "
                "hermes-poc@streetsmart-hermes-poc.iam.gserviceaccount.com"
            )

        sent, error = ping_carlo(
            _synthetic_failure(),
            {"email_sender": boom, "chat_sender": lambda msg: chats.append(msg) or True},
        )
        self.assertTrue(sent)
        self.assertEqual(len(chats), 1)
        self.assertIn("signBlob", error)
        self.assertIn("403", error)


if __name__ == "__main__":
    unittest.main()
