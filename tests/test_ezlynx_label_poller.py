"""Unit tests for the EZLynx label poller. No network, no browser, no CDP."""

from __future__ import annotations

import io
import json
import unittest
from urllib import error

from robie_job_engine import ezlynx_label_poller as poller
from robie_job_engine.ezlynx_label_poller import (
    BLAND_CAMPAIGN_LABELS,
    LabelPoller,
    LabelPollerError,
    campaign_for_labels,
    note_id_of,
)


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


class FakeDiscussionClient:
    """Minimal DiscussionApiClient double: get_discussions + get_discussion."""

    def __init__(self, discussions, details):
        self._discussions = discussions
        self._details = details

    def get_discussions(self, applicant_id):
        return self._discussions

    def get_discussion(self, discussion_id):
        return self._details[discussion_id]


def make_poller(discussions, details, *, label_rows=None, webhook_payload=None):
    """Poller with injectable urlopen + cookie loader. No network."""
    label_rows = label_rows if label_rows is not None else []
    calls = {"webhook": [], "label_gets": []}

    def fake_urlopen(url, *, data, headers, timeout):
        if data is None:
            # Portal label GET
            calls["label_gets"].append(url)
            return FakeResponse(label_rows)
        # Webhook POST
        body = json.loads(data.decode("utf-8"))
        calls["webhook"].append({"url": url, "body": body, "headers": headers})
        return FakeResponse(
            webhook_payload
            if webhook_payload is not None
            else {"status": "received", "campaign_id": body["campaign_id"]}
        )

    def fake_cookies():
        return [{"name": "session", "value": "abc", "domain": "app.ezlynx.com"}]

    client = FakeDiscussionClient(discussions, details)
    p = LabelPoller(
        client,
        portal_base_url="https://app.ezlynx.com/EZLynxPortalAPI/",
        portal_origin="https://app.ezlynx.com",
        urlopen=fake_urlopen,
        load_cookies=fake_cookies,
    )
    return p, calls


class CampaignMappingTests(unittest.TestCase):
    def test_all_eleven_labels_map(self):
        self.assertEqual(len(BLAND_CAMPAIGN_LABELS), 11)
        self.assertEqual(BLAND_CAMPAIGN_LABELS["Robie Call"], "robie-call")
        self.assertEqual(
            BLAND_CAMPAIGN_LABELS["Robie renewal reach-out"], "robie-renewal-reachout"
        )

    def test_campaign_for_labels_exact_match(self):
        rows = [{"name": "Robie audit", "id": "9"}]
        self.assertEqual(campaign_for_labels(rows), "robie-audit")

    def test_campaign_for_labels_no_match_returns_none(self):
        self.assertIsNone(campaign_for_labels([{"name": "Something Else"}]))
        self.assertIsNone(campaign_for_labels([]))
        self.assertIsNone(campaign_for_labels(None))

    def test_campaign_for_labels_case_sensitive(self):
        # Character-for-character: wrong case must not match.
        self.assertIsNone(campaign_for_labels([{"name": "robie call"}]))

    def test_note_id_of_variants(self):
        self.assertEqual(note_id_of({"noteId": "n1"}), "n1")
        self.assertEqual(note_id_of({"Id": "n2"}), "n2")
        self.assertEqual(note_id_of({}), "")


class LabelReadTests(unittest.TestCase):
    def test_read_note_labels_uses_portal_path_not_oauth(self):
        p, calls = make_poller([], {}, label_rows=[{"name": "Robie Call"}])
        rows = p.read_note_labels("note-1")
        self.assertEqual(rows, [{"name": "Robie Call"}])
        self.assertTrue(calls["label_gets"])
        url = calls["label_gets"][0]
        self.assertIn("/EZLynxPortalAPI/Notes/note-1/OrganizationLabels", url)

    def test_read_note_labels_sends_no_bearer(self):
        seen_headers = {}

        def fake_urlopen(url, *, data, headers, timeout):
            seen_headers.update(headers)
            return FakeResponse([])

        def fake_cookies():
            return [{"name": "s", "value": "v", "domain": "app.ezlynx.com"}]

        client = FakeDiscussionClient([], {})
        p = LabelPoller(
            client,
            portal_base_url="https://app.ezlynx.com/EZLynxPortalAPI/",
            portal_origin="https://app.ezlynx.com",
            urlopen=fake_urlopen,
            load_cookies=fake_cookies,
        )
        p.read_note_labels("note-1")
        self.assertNotIn("Authorization", seen_headers)
        self.assertIn("Cookie", seen_headers)

    def test_read_note_labels_http_error_is_fail_closed(self):
        def bad_urlopen(url, *, data, headers, timeout):
            raise error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b""))

        def fake_cookies():
            return [{"name": "s", "value": "v", "domain": "app.ezlynx.com"}]

        client = FakeDiscussionClient([], {})
        p = LabelPoller(
            client,
            portal_base_url="https://app.ezlynx.com/EZLynxPortalAPI/",
            portal_origin="https://app.ezlynx.com",
            urlopen=bad_urlopen,
            load_cookies=fake_cookies,
        )
        with self.assertRaises(LabelPollerError) as caught:
            p.read_note_labels("note-1")
        self.assertEqual(caught.exception.code, poller.LABEL_READ_FAILED)

    def test_read_note_labels_without_cookie_loader_is_fail_closed(self):
        client = FakeDiscussionClient([], {})
        p = LabelPoller(
            client,
            portal_base_url="https://app.ezlynx.com/EZLynxPortalAPI/",
            portal_origin="https://app.ezlynx.com",
            urlopen=lambda *a, **k: FakeResponse([]),
            load_cookies=None,
        )
        with self.assertRaises(LabelPollerError):
            p.read_note_labels("note-1")


class WebhookTests(unittest.TestCase):
    def test_fire_webhook_posts_expected_payload(self):
        p, calls = make_poller([], {})
        result = p.fire_webhook(
            applicant_id="25486692",
            note_id="n1",
            label="Robie Call",
            campaign_id="robie-call",
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(calls["webhook"]), 1)
        body = calls["webhook"][0]["body"]
        self.assertEqual(body["applicant_id"], "25486692")
        self.assertEqual(body["note_id"], "n1")
        self.assertEqual(body["label"], "Robie Call")
        self.assertEqual(body["campaign_id"], "robie-call")
        self.assertEqual(
            calls["webhook"][0]["url"], poller.DEFAULT_WEBHOOK_URL
        )

    def test_fire_webhook_failure_raises(self):
        def bad_urlopen(url, *, data, headers, timeout):
            raise error.HTTPError(url, 500, "Server Error", {}, io.BytesIO(b""))

        def fake_cookies():
            return [{"name": "s", "value": "v", "domain": "app.ezlynx.com"}]

        client = FakeDiscussionClient([], {})
        p = LabelPoller(
            client,
            portal_base_url="https://app.ezlynx.com/EZLynxPortalAPI/",
            portal_origin="https://app.ezlynx.com",
            urlopen=bad_urlopen,
            load_cookies=fake_cookies,
        )
        with self.assertRaises(LabelPollerError) as caught:
            p.fire_webhook(
                applicant_id="1", note_id="n", label="L", campaign_id="c"
            )
        self.assertEqual(caught.exception.code, poller.WEBHOOK_FAILED)


class PollApplicantTests(unittest.TestCase):
    def _discussions(self):
        return [
            {"discussionId": "d1", "title": "TEST - Robie Call"},
            {"discussionId": "d2", "title": "Other"},
        ]

    def _details(self):
        return {
            "d1": {"notes": [{"noteId": "n1"}, {"noteId": "n2"}]},
            "d2": {"notes": [{"noteId": "n3"}]},
        }

    def test_matching_label_fires_webhook_and_marks_processed(self):
        p, calls = make_poller(
            self._discussions(), self._details(), label_rows=[{"name": "Robie Call"}]
        )
        processed = set()
        summary = p.poll_applicant(
            "25486692",
            is_processed=processed.__contains__,
            mark_processed=processed.add,
        )
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["discussions_scanned"], 2)
        self.assertEqual(summary["notes_seen"], 3)
        self.assertEqual(summary["labels_matched"], 3)
        self.assertEqual(summary["webhooks_fired"], 3)
        self.assertEqual(processed, {"n1", "n2", "n3"})
        campaigns = [c["body"]["campaign_id"] for c in calls["webhook"]]
        self.assertEqual(campaigns, ["robie-call"] * 3)

    def test_no_label_match_marks_processed_without_firing(self):
        p, calls = make_poller(
            self._discussions(), self._details(), label_rows=[{"name": "Unrelated"}]
        )
        processed = set()
        summary = p.poll_applicant(
            "25486692",
            is_processed=processed.__contains__,
            mark_processed=processed.add,
        )
        self.assertEqual(summary["webhooks_fired"], 0)
        self.assertEqual(summary["labels_matched"], 0)
        self.assertEqual(processed, {"n1", "n2", "n3"})
        self.assertEqual(calls["webhook"], [])

    def test_already_processed_notes_are_skipped(self):
        p, calls = make_poller(
            self._discussions(), self._details(), label_rows=[{"name": "Robie Call"}]
        )
        processed = {"n1", "n2", "n3"}
        summary = p.poll_applicant(
            "25486692",
            is_processed=processed.__contains__,
            mark_processed=processed.add,
        )
        self.assertEqual(summary["webhooks_fired"], 0)
        self.assertEqual(calls["webhook"], [])
        self.assertEqual(calls["label_gets"], [])

    def test_label_read_failure_skips_note_without_marking(self):
        def bad_urlopen(url, *, data, headers, timeout):
            if data is None:
                raise error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b""))
            return FakeResponse({"status": "received"})

        def fake_cookies():
            return [{"name": "s", "value": "v", "domain": "app.ezlynx.com"}]

        client = FakeDiscussionClient(self._discussions(), self._details())
        p = LabelPoller(
            client,
            portal_base_url="https://app.ezlynx.com/EZLynxPortalAPI/",
            portal_origin="https://app.ezlynx.com",
            urlopen=bad_urlopen,
            load_cookies=fake_cookies,
        )
        processed = set()
        summary = p.poll_applicant(
            "25486692",
            is_processed=processed.__contains__,
            mark_processed=processed.add,
        )
        # Fail-closed: nothing fired, nothing marked, outcomes recorded.
        self.assertEqual(summary["webhooks_fired"], 0)
        self.assertEqual(processed, set())
        failed = [o for o in summary["outcomes"] if o["status"] == "label_read_failed"]
        self.assertEqual(len(failed), 3)

    def test_webhook_failure_leaves_note_unprocessed_for_retry(self):
        def flaky_urlopen(url, *, data, headers, timeout):
            if data is None:
                return FakeResponse([{"name": "Robie Call"}])
            raise error.HTTPError(url, 500, "Server Error", {}, io.BytesIO(b""))

        def fake_cookies():
            return [{"name": "s", "value": "v", "domain": "app.ezlynx.com"}]

        client = FakeDiscussionClient(self._discussions(), self._details())
        p = LabelPoller(
            client,
            portal_base_url="https://app.ezlynx.com/EZLynxPortalAPI/",
            portal_origin="https://app.ezlynx.com",
            urlopen=flaky_urlopen,
            load_cookies=fake_cookies,
        )
        processed = set()
        summary = p.poll_applicant(
            "25486692",
            is_processed=processed.__contains__,
            mark_processed=processed.add,
        )
        self.assertEqual(summary["webhooks_fired"], 0)
        self.assertEqual(processed, set())
        failed = [o for o in summary["outcomes"] if o["status"] == "webhook_failed"]
        self.assertEqual(len(failed), 3)

    def test_second_poll_after_success_fires_nothing(self):
        p, calls = make_poller(
            self._discussions(), self._details(), label_rows=[{"name": "Robie Call"}]
        )
        processed = set()
        first = p.poll_applicant(
            "25486692",
            is_processed=processed.__contains__,
            mark_processed=processed.add,
        )
        self.assertEqual(first["webhooks_fired"], 3)
        calls["webhook"].clear()
        second = p.poll_applicant(
            "25486692",
            is_processed=processed.__contains__,
            mark_processed=processed.add,
        )
        self.assertEqual(second["webhooks_fired"], 0)
        self.assertEqual(calls["webhook"], [])

    def test_distinct_labels_map_to_distinct_campaigns(self):
        label_sequences = [
            [{"name": "Robie Call"}],
            [{"name": "Robie audit"}],
            [{"name": "Robie e-sign"}],
        ]
        get_count = {"n": 0}

        def seq_urlopen(url, *, data, headers, timeout):
            if data is None:
                rows = label_sequences[get_count["n"] % 3]
                get_count["n"] += 1
                return FakeResponse(rows)
            body = json.loads(data.decode("utf-8"))
            return FakeResponse({"status": "received", "campaign_id": body["campaign_id"]})

        def fake_cookies():
            return [{"name": "s", "value": "v", "domain": "app.ezlynx.com"}]

        client = FakeDiscussionClient(self._discussions(), self._details())
        p = LabelPoller(
            client,
            portal_base_url="https://app.ezlynx.com/EZLynxPortalAPI/",
            portal_origin="https://app.ezlynx.com",
            urlopen=seq_urlopen,
            load_cookies=fake_cookies,
        )
        fired = []

        def capture_urlopen(url, *, data, headers, timeout):
            resp = seq_urlopen(url, data=data, headers=headers, timeout=timeout)
            if data is not None:
                fired.append(json.loads(data.decode("utf-8"))["campaign_id"])
            return resp

        p._urlopen = capture_urlopen
        processed = set()
        p.poll_applicant(
            "25486692",
            is_processed=processed.__contains__,
            mark_processed=processed.add,
        )
        self.assertEqual(fired, ["robie-call", "robie-audit", "robie-e-sign"])


if __name__ == "__main__":
    unittest.main()
