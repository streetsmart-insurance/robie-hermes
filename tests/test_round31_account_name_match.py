"""Search hits on other accounts are not extra Buster Browns.

EZLynx also returns accounts where the name is a linked contact,
co-applicant, or driver. Only the account's own name is that client.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_job_controls import outbound_is_clarify
from robie_job_engine.client_name_lookup import (
    account_name_matches,
    pending_named_lookup_line,
    prepare_named_client_lookup,
)
from robie_job_engine.store import JobStore
from robie_job_engine.user_reply import format_user_reply
from test_round24_named_lookup import _job, _searcher

BROWN = "26356199"
MIELE = "62963499"
RINA = "143786127"
CARLO = [
    {"applicant_id": BROWN, "name": "BUSTER BROWN", "city": "Austin"},
    {"applicant_id": MIELE, "name": "MIELE JOHANSON", "city": "Dallas"},
    {"applicant_id": RINA, "name": "MARJORIE ANNE RINA", "city": "Houston"},
]


def _reply(store: JobStore, job_id: str, reply: str) -> None:
    payload = dict(store.get_job(job_id).get("payload") or {})
    payload["clarification_reply"] = reply
    store.update_payload(job_id, payload)
    store.checkpoint(job_id, "clarification_reply", {"text": reply})


class AccountNameMatchTests(unittest.TestCase):
    def test_one_account_name_binds_without_asking(self):
        self.assertTrue(account_name_matches("buster brown", "BUSTER BROWN"))
        self.assertTrue(account_name_matches("buster brown", "Buster A. Brown"))
        self.assertTrue(account_name_matches("buster brown", "Buster-Brown"))
        self.assertFalse(account_name_matches("buster brown", "MIELE JOHANSON"))
        self.assertFalse(account_name_matches("buster brown", "MARJORIE ANNE RINA"))
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _job(store)
            line = prepare_named_client_lookup(
                store, job_id, searcher=_searcher(CARLO)
            )
            self.assertIsNone(line)
            payload = store.get_job(job_id)["payload"]
            self.assertEqual(payload["applicant_id"], BROWN)
            note = store.get_checkpoint(job_id, "client_name_search")
            self.assertEqual(note["source"], "search")
            self.assertEqual(note["applicant_ids"], [BROWN])
            self.assertFalse(str(note.get("user_line") or ""))
            self.assertNotIn(MIELE, note["applicant_ids"])
            self.assertNotIn(RINA, note["applicant_ids"])
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            other = _job(store)
            initial = prepare_named_client_lookup(
                store,
                other,
                searcher=_searcher(
                    [
                        {"applicant_id": BROWN, "name": "Buster A. Brown"},
                        {"applicant_id": MIELE, "name": "Miele Johanson"},
                    ]
                ),
            )
            self.assertIsNone(initial)
            self.assertEqual(store.get_job(other)["payload"]["applicant_id"], BROWN)

    def test_several_account_names_list_only_those(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _job(store)
            line = prepare_named_client_lookup(
                store,
                job_id,
                searcher=_searcher(
                    [
                        {
                            "applicant_id": BROWN,
                            "name": "BUSTER BROWN",
                            "address": "12 Oak St, Austin",
                        },
                        {
                            "applicant_id": "88001123",
                            "name": "Buster A. Brown",
                            "city": "Dallas",
                        },
                        {
                            "applicant_id": MIELE,
                            "name": "MIELE JOHANSON",
                            "address": "9 Elm Ave, Houston",
                        },
                    ]
                ),
            )
            self.assertIn("I found more than one Buster Brown.", line or "")
            self.assertIn("12 Oak St, Austin", line or "")
            self.assertIn("Dallas", line or "")
            self.assertIn(f"account {BROWN}", line or "")
            self.assertIn("account 88001123", line or "")
            self.assertIn("1. ", line or "")
            self.assertIn("2. ", line or "")
            self.assertTrue(str(line or "").endswith("Which one should I use?"))
            self.assertNotIn("Miele", line or "")
            self.assertNotIn("Johanson", line or "")
            self.assertNotIn("Elm", line or "")
            self.assertNotIn("Houston", line or "")
            self.assertNotIn(MIELE, line or "")
            self.assertNotIn("applicant_id", line or "")
            self.assertFalse(str(store.get_job(job_id)["payload"].get("applicant_id") or ""))

    def test_linked_accounts_ask_under_their_own_names(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _job(store)
            line = prepare_named_client_lookup(
                store,
                job_id,
                searcher=_searcher(
                    [
                        {"applicant_id": MIELE, "name": "MIELE JOHANSON"},
                        {"applicant_id": RINA, "name": "MARJORIE ANNE RINA"},
                    ]
                ),
            )
            self.assertNotIn("\n", line or "")
            self.assertIn(
                "I didn't find an account named Buster Brown, but the name shows up on 2 other accounts:",
                line or "",
            )
            self.assertIn(f"1. Miele Johanson (account {MIELE})", line or "")
            self.assertIn(f"2. Marjorie Anne Rina (account {RINA})", line or "")
            self.assertTrue(str(line or "").endswith("Which one, or none?"))
            self.assertNotIn("I found more than one", line or "")
            self.assertNotIn("a Buster Brown", line or "")
            self.assertNotIn("applicant_id", line or "")
            self.assertEqual(format_user_reply(line or ""), line)
            self.assertTrue(outbound_is_clarify(line or ""))
            self.assertFalse(str(store.get_job(job_id)["payload"].get("applicant_id") or ""))

            _reply(store, job_id, "none")
            declined = pending_named_lookup_line(db, job_id)
            self.assertEqual(declined, "I couldn't find a client named Buster Brown.")
            self.assertFalse(str(store.get_job(job_id)["payload"].get("applicant_id") or ""))
            self.assertEqual(
                store.get_checkpoint(job_id, "client_name_search")["source"], "none"
            )
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            asked = _job(store)
            prepare_named_client_lookup(
                store,
                asked,
                searcher=_searcher(
                    [
                        {"applicant_id": MIELE, "name": "MIELE JOHANSON"},
                        {"applicant_id": RINA, "name": "MARJORIE ANNE RINA"},
                    ]
                ),
            )
            _reply(store, asked, MIELE)
            self.assertEqual(pending_named_lookup_line(db, asked), "")
            payload = store.get_job(asked)["payload"]
            self.assertEqual(payload["applicant_id"], MIELE)
            self.assertIn("Johanson", str(payload.get("client_name") or ""))
            self.assertNotIn("buster", str(payload.get("client_name") or "").casefold())
            self.assertEqual(
                store.get_checkpoint(asked, "client_name_search")["applicant_ids"],
                [MIELE],
            )
