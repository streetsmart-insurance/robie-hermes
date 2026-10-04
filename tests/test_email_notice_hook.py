"""Tests for the hermes-email-watcher -> Ascend notice driver hook.

No network, no Secret Manager, no Gmail. The hook must call the existing
driver for Ascend-domain mail and leave StreetSmart / finance mail alone.
"""

from __future__ import annotations

import os
import unittest
from datetime import date
from unittest import mock

from robie_job_engine import ascend_notice_driver as driver
from robie_job_engine import ascend_notice_triage as triage
from robie_job_engine import email_notice_hook as hook

try:
    from test_ascend_notice_driver import (  # unittest discover -s tests
        CANCELLATION_BODY,
        CANCELLATION_SUBJECT,
        FakeAscendClient,
        FakeEzlynxClient,
        FakeSource,
        make_discussion_client,
        policy_row,
    )
except ImportError:  # pytest / PYTHONPATH=.
    from tests.test_ascend_notice_driver import (
        CANCELLATION_BODY,
        CANCELLATION_SUBJECT,
        FakeAscendClient,
        FakeEzlynxClient,
        FakeSource,
        make_discussion_client,
        policy_row,
    )


def _ctx(*, dry_run=True, discussion_rows=None, policy_rows=None):
    discussion_rows = discussion_rows or [{"discussionId": "d1", "title": "Cancellation"}]
    discussion_client = make_discussion_client(discussion_rows)
    if policy_rows is None:
        policy_rows = {"HO-998877": [policy_row()]}
    ctx = driver.DriverContext(
        ascend_client=FakeAscendClient(),
        ezlynx_client=FakeEzlynxClient(rows_by_number=policy_rows),
        discussion_client=discussion_client,
        source=FakeSource([]),
        dry_run=dry_run,
        due_days=2,
        today=date(2026, 9, 15),
    )
    return ctx, discussion_client


class AscendNoticeSenderTests(unittest.TestCase):
    def test_useascend_domains_match(self):
        self.assertTrue(hook.is_ascend_notice_sender("notifications@useascend.com"))
        self.assertTrue(hook.is_ascend_notice_sender("noreply@mail.useascend.com"))
        self.assertTrue(
            hook.is_ascend_notice_sender("Ascend <alerts@useascend.com>")
        )

    def test_non_ascend_senders_do_not_match(self):
        self.assertFalse(hook.is_ascend_notice_sender("carlo@streetsmart.insurance"))
        self.assertFalse(hook.is_ascend_notice_sender("robie@streetsmart.insurance"))
        self.assertFalse(hook.is_ascend_notice_sender("vendor@gmail.com"))
        self.assertFalse(hook.is_ascend_notice_sender(""))


class WatcherHookTests(unittest.TestCase):
    def test_non_ascend_sender_is_ignored(self):
        ctx, discussion_client = _ctx()
        result = hook.try_process_ascend_notice(
            sender="carlo@streetsmart.insurance",
            subject=CANCELLATION_SUBJECT,
            body=CANCELLATION_BODY,
            message_id="m1",
            ctx=ctx,
        )
        self.assertIsNone(result)
        self.assertEqual(discussion_client._urlopen.posts_to("/notes"), [])

    def test_finance_street_smart_mail_is_not_stolen(self):
        ctx, _ = _ctx()
        result = hook.try_process_ascend_notice(
            sender="jake@streetsmart.insurance",
            subject="Create an Ascend finance agreement",
            body="Please generate a premium finance program",
            message_id="m-finance",
            ctx=ctx,
        )
        self.assertIsNone(result)

    def test_ascend_notice_calls_existing_driver(self):
        ctx, discussion_client = _ctx(dry_run=True)
        with mock.patch.object(driver.zapier_tasks, "fire_task", return_value={"ok": True}):
            result = hook.try_process_ascend_notice(
                sender="notifications@useascend.com",
                subject=CANCELLATION_SUBJECT,
                body=CANCELLATION_BODY,
                message_id="m1",
                ctx=ctx,
            )
        self.assertIsNotNone(result)
        self.assertTrue(result["consumed"])
        self.assertFalse(result["mark_read"])  # dry-run does not mark read
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["detail"]["notice_type"], triage.CANCELLATION)
        self.assertIn(driver.ROBIE_WAS_HERE, result["detail"]["note_text"])
        self.assertTrue(
            result["detail"]["note_text"].startswith("NON-PAY CANCELLATION notice from Ascend.")
        )
        self.assertEqual(result["reason"], "label_skipped_by_policy")
        self.assertEqual(result["detail"]["label"]["status"], "label_skipped_by_policy")
        self.assertNotIn("label_id", result["detail"]["label"])
        self.assertEqual(discussion_client._urlopen.posts_to("/notes"), [])
        self.assertEqual(ctx.ezlynx_client.label_list_calls, 0)
        self.assertEqual(ctx.ezlynx_client.applied_labels, [])

    def test_live_success_requests_mark_read(self):
        ctx, discussion_client = _ctx(dry_run=False)
        with mock.patch.object(driver.zapier_tasks, "fire_task", return_value={"ok": True}):
            result = hook.try_process_ascend_notice(
                sender="Ascend <noreply@useascend.com>",
                subject=CANCELLATION_SUBJECT,
                body=CANCELLATION_BODY,
                message_id="m-live",
                ctx=ctx,
                dry_run=False,
            )
        self.assertEqual(result["status"], "done")
        self.assertTrue(result["mark_read"])
        self.assertTrue(result["consumed"])
        self.assertEqual(len(discussion_client._urlopen.posts_to("/notes")), 1)
        self.assertEqual(ctx.source.marked, ["m-live"])
        self.assertEqual(result["reason"], "label_skipped_by_policy")
        self.assertEqual(result["detail"]["label"]["status"], "label_skipped_by_policy")
        self.assertNotIn("label_id", result["detail"]["label"])
        self.assertEqual(ctx.ezlynx_client.label_list_calls, 0)
        self.assertEqual(ctx.ezlynx_client.applied_labels, [])

    def test_fail_closed_is_consumed_but_left_unread(self):
        ctx, discussion_client = _ctx()
        result = hook.try_process_ascend_notice(
            sender="notifications@useascend.com",
            subject="Your monthly statement is ready",
            body="hello",
            message_id="m-unknown",
            ctx=ctx,
        )
        self.assertTrue(result["consumed"])
        self.assertFalse(result["mark_read"])
        self.assertEqual(result["status"], "skipped")
        self.assertIn("needs_human_review", result["reason"])
        self.assertEqual(discussion_client._urlopen.posts_to("/notes"), [])

    def test_missing_policy_does_not_apply_label(self):
        ctx, discussion_client = _ctx(policy_rows={})
        result = hook.try_process_ascend_notice(
            sender="notifications@useascend.com",
            subject=CANCELLATION_SUBJECT,
            body=CANCELLATION_BODY,
            message_id="m-nopolicy",
            ctx=ctx,
        )
        self.assertTrue(result["consumed"])
        self.assertFalse(result["mark_read"])
        self.assertEqual(result["status"], "skipped")
        self.assertIn("applicant_unresolved", result["reason"])
        self.assertEqual(discussion_client._urlopen.posts_to("/notes"), [])
        self.assertEqual(ctx.ezlynx_client.applied_labels, [])

    def test_missing_clients_are_not_consumed(self):
        with mock.patch.object(
            driver, "build_processing_context", side_effect=RuntimeError("no secrets")
        ):
            result = hook.try_process_ascend_notice(
                sender="notifications@useascend.com",
                subject=CANCELLATION_SUBJECT,
                body=CANCELLATION_BODY,
                message_id="m-cfg",
            )
        self.assertFalse(result["consumed"])
        self.assertFalse(result["mark_read"])
        self.assertIn("clients_unavailable", result["reason"])

    def test_watcher_dry_run_flag(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(hook.ASCEND_WATCHER_DRY_RUN_ENV, None)
            self.assertFalse(hook.watcher_notice_dry_run())
        with mock.patch.dict(os.environ, {hook.ASCEND_WATCHER_DRY_RUN_ENV: "1"}):
            self.assertTrue(hook.watcher_notice_dry_run())


class ProcessingContextTests(unittest.TestCase):
    def test_processing_context_does_not_require_gmail_delegation(self):
        class _FakeApiConfig:
            document_base_url = "https://app.uatezlynx.com"
            token_endpoint = "https://identity.example.com/connect/token"
            client_id = "id"
            client_secret = "secret"
            username = "SSRobie"
            integration_group_id = "159"

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ROBIE_GMAIL_DELEGATION_SA", None)
            with mock.patch.object(driver, "load_ezlynx_api_config", return_value=_FakeApiConfig()):
                with mock.patch.object(driver, "EzlynxApiClient", return_value=object()):
                    with mock.patch.object(driver, "DiscussionApiClient", return_value=object()):
                        with mock.patch.object(driver, "configured_ascend_client", return_value=object()):
                            ctx = driver.build_processing_context(dry_run=False)
        self.assertIsInstance(ctx.source, driver.NullNoticeSource)
        self.assertFalse(ctx.dry_run)
        ctx.source.mark_processed("x")  # no-op
        self.assertEqual(ctx.source.fetch_notices(), [])
