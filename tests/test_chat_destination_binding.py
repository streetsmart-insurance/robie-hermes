"""Tests for slice 1 destination binding.

Each test is named for the thing that goes wrong without it.
"""

import unittest

from robie_job_engine.chat_destination_binding import (
    CLAIMED,
    DERIVED,
    bind_destination_for_job,
    derive_destination,
    extract_ezlynx_urls,
)

APPLICANT = "194066748"
POLICY = "73834086"


def exec_row(url, tool="playwright.goto", status="ok"):
    return {
        "tool": tool,
        "status": status,
        "code_preview": f'page.goto("{url}")',
        "result_json": '{"ok": true}',
    }


def result_row(url):
    """Some tools record the URL only in the result, not the code."""
    return {
        "tool": "playwright.snapshot",
        "status": "ok",
        "code_preview": "page.url",
        "result_json": '{"url": "%s"}' % url,
    }


ACCOUNT_URL = f"https://app.ezlynx.com/web/account/{APPLICANT}/overview"
EDIT_URL = f"https://app.ezlynx.com/applicantportal/policy/actions/edit/{APPLICANT}"


class FakeStore:
    def __init__(self, rows):
        self._rows = rows
        self.checkpoints = {}
        self.payloads = {}

    def playwright_exec_rows(self, job_id):
        return self._rows

    def checkpoint(self, job_id, kind, data):
        self.checkpoints[(job_id, kind)] = data

    def update_payload(self, job_id, payload):
        self.payloads[job_id] = payload


class ExtractTests(unittest.TestCase):
    def test_finds_urls_in_code_and_in_results(self):
        urls = extract_ezlynx_urls([exec_row(ACCOUNT_URL), result_row(EDIT_URL)])
        self.assertIn(ACCOUNT_URL, urls)
        self.assertIn(EDIT_URL, urls)

    def test_ignores_non_ezlynx_hosts(self):
        urls = extract_ezlynx_urls([exec_row("https://example.com/web/account/1/x")])
        self.assertEqual(urls, [])

    def test_survives_non_string_result_blobs(self):
        rows = [{"code_preview": None, "result_json": {"url": ACCOUNT_URL}}]
        self.assertIn(ACCOUNT_URL, extract_ezlynx_urls(rows))


class DeriveTests(unittest.TestCase):

    def test_applicant_comes_from_the_browser_not_the_worker(self):
        b = derive_destination([exec_row(ACCOUNT_URL)], {"policy_number": POLICY})
        self.assertEqual(b.applicant_id, APPLICANT)
        self.assertEqual(b.provenance["applicant_id"], DERIVED)
        self.assertTrue(b.bindable)

    def test_policy_number_is_always_marked_as_the_workers_claim(self):
        """It is not in the URL. Pretending otherwise would launder a guess."""
        b = derive_destination([exec_row(ACCOUNT_URL)], {"policy_number": POLICY})
        self.assertEqual(b.policy_number, POLICY)
        self.assertEqual(b.provenance["policy_number"], CLAIMED)

    def test_two_applicants_is_refused_not_guessed(self):
        rows = [exec_row(ACCOUNT_URL), exec_row("https://app.ezlynx.com/web/account/999/x")]
        b = derive_destination(rows, {})
        self.assertFalse(b.bindable)
        self.assertIn("more than one EZLynx applicant", b.refusal)
        self.assertEqual(len(b.applicants_seen), 2)

    def test_worker_claiming_an_applicant_the_browser_never_touched_is_refused(self):
        b = derive_destination([exec_row(ACCOUNT_URL)], {"applicant_id": "999999999"})
        self.assertFalse(b.bindable)
        self.assertIn("only touched", b.refusal)

    def test_claim_is_accepted_when_nothing_was_recorded_but_is_labelled(self):
        b = derive_destination([], {"applicant_id": APPLICANT, "policy_number": POLICY})
        self.assertTrue(b.bindable)
        self.assertEqual(b.provenance["applicant_id"], CLAIMED)

    def test_nothing_recorded_and_nothing_claimed_is_refused(self):
        b = derive_destination([], {})
        self.assertFalse(b.bindable)
        self.assertIn("no destination to verify", b.refusal)

    def test_same_applicant_seen_many_times_is_one_applicant(self):
        rows = [exec_row(ACCOUNT_URL) for _ in range(12)] + [result_row(EDIT_URL)]
        b = derive_destination(rows, {})
        self.assertTrue(b.bindable)
        self.assertEqual(b.applicants_seen, [APPLICANT])
        self.assertEqual(b.urls_seen, 13)


class CheckpointTests(unittest.TestCase):

    def test_checkpoint_shape_is_what_chat_guard_looks_for(self):
        b = derive_destination(
            [exec_row(ACCOUNT_URL)],
            {"policy_number": POLICY, "discussion_title": "Bond",
             "document_names": ["Bond - Western Surety.pdf"]},
        )
        cp = b.checkpoint("85f5eae0")
        self.assertEqual(cp["destination"]["applicant_id"], APPLICANT)
        self.assertEqual(cp["destination"]["policy_number"], POLICY)
        self.assertEqual(cp["destination"]["discussion_title"], "Bond")
        self.assertEqual(cp["destination"]["document_names"], ["Bond - Western Surety.pdf"])

    def test_checkpoint_says_out_loud_that_it_is_a_claim(self):
        b = derive_destination([exec_row(ACCOUNT_URL)], {"policy_number": POLICY})
        cp = b.checkpoint("j1")
        self.assertTrue(cp["detail"]["this_is_a_claim_not_evidence"])
        self.assertEqual(cp["detail"]["provenance"]["policy_number"], CLAIMED)

    def test_payload_patch_carries_ids_only_never_prose(self):
        b = derive_destination(
            [exec_row(ACCOUNT_URL)],
            {"policy_number": POLICY, "discussion_title": "Bond",
             "document_names": ["x.pdf"], "summary": "I did the thing"},
        )
        self.assertEqual(b.payload_patch(), {"applicant_id": APPLICANT, "policy_number": POLICY})


class BindTests(unittest.TestCase):

    def test_binding_writes_checkpoint_and_patches_payload(self):
        store = FakeStore([exec_row(ACCOUNT_URL)])
        job = {"id": "85f5eae0", "payload": {"prompt": "set up the bond"}}
        b = bind_destination_for_job(store, job, {"policy_number": POLICY})
        self.assertTrue(b.bindable)
        self.assertIn(("85f5eae0", "action"), store.checkpoints)
        self.assertEqual(job["payload"]["applicant_id"], APPLICANT)
        self.assertEqual(job["payload"]["policy_number"], POLICY)
        self.assertEqual(job["payload"]["prompt"], "set up the bond")

    def test_an_id_the_job_was_already_bound_to_is_never_overwritten(self):
        store = FakeStore([exec_row(ACCOUNT_URL)])
        job = {"id": "j", "payload": {"applicant_id": "111111111"}}
        bind_destination_for_job(store, job, {"policy_number": POLICY})
        self.assertEqual(job["payload"]["applicant_id"], "111111111")

    def test_a_refused_binding_writes_nothing(self):
        rows = [exec_row(ACCOUNT_URL), exec_row("https://app.ezlynx.com/web/account/999/x")]
        store = FakeStore(rows)
        job = {"id": "j", "payload": {"prompt": "p"}}
        b = bind_destination_for_job(store, job, {})
        self.assertFalse(b.bindable)
        self.assertEqual(store.checkpoints, {})
        self.assertEqual(job["payload"], {"prompt": "p"})

    def test_reads_from_a_raw_sqlite_store_too(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE playwright_exec (id INTEGER PRIMARY KEY, job_id TEXT,"
            " tool TEXT, status TEXT, code_preview TEXT, result_json TEXT, created_at TEXT)"
        )
        conn.execute(
            "INSERT INTO playwright_exec (job_id,tool,status,code_preview,result_json,created_at)"
            " VALUES ('j','goto','ok',?,'{}','now')",
            (f'page.goto("{ACCOUNT_URL}")',),
        )
        conn.commit()

        class RawStore:
            _conn = conn
            def checkpoint(self, *a): pass

        b = derive_destination(
            __import__("pkg.chat_destination_binding", fromlist=["read_exec_rows"])
            .read_exec_rows(RawStore(), "j"),
            {},
        )
        self.assertEqual(b.applicant_id, APPLICANT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
