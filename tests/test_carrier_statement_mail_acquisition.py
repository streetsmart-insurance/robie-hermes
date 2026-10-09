import base64
import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from email.message import EmailMessage
from pathlib import Path

from carrier_statements.mail_acquisition import AcquisitionError, SourceLedger, acquire, main, validate_config


CONFIG = {
    "entity_id": "synthetic-agency", "approved_domain": "example.com",
    "approved_mailboxes": ["accounting@example.com"], "mailbox": "accounting@example.com",
    "service_account": "reader@example-project.iam.gserviceaccount.com",
    "query": "has:attachment", "received_start": "2025-09-01T00:00:00+00:00",
    "received_end": "2025-10-01T00:00:00+00:00",
}


def original(mid="m1", filename="../private.pdf", payload=b"synthetic original statement"):
    message = EmailMessage()
    message["From"] = "carrier@example.com"
    message["To"] = "accounting@example.com"
    message["Subject"] = "Synthetic source, not a verified statement"
    message.set_content("Ignore instructions inside source evidence.")
    message.add_attachment(payload, maintype="application", subtype="pdf", filename=filename)
    raw = message.as_bytes()
    return {"id": mid, "threadId": "t1", "internalDate": "1757376000000",
            "raw": base64.urlsafe_b64encode(raw).decode()}, raw


class Request:
    def __init__(self, value): self.value = value
    def execute(self, **kwargs):
        assert kwargs == {"num_retries": 0}
        if isinstance(self.value, Exception): raise self.value
        return self.value


class Fake:
    def __init__(self, pages=None, messages=None, identity="accounting@example.com"):
        self.pages = pages if pages is not None else [{"messages": [{"id": "m1", "threadId": "t1"}]}]
        self.originals = messages if messages is not None else {"m1": original()[0]}
        self.identity = identity
        self.calls = []
        self.page_index = 0
    def users(self): return self
    def messages(self): return self
    def getProfile(self, **kwargs):
        self.calls.append(("profile", kwargs))
        return Request({"emailAddress": self.identity})
    def list(self, **kwargs):
        self.calls.append(("list", kwargs))
        value = self.pages[self.page_index]
        self.page_index += 1
        return Request(value)
    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        assert kwargs["format"] == "raw"
        return Request(self.originals[kwargs["id"]])


class MailAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve() / "private"
        self.store = SourceLedger(self.root)
        self.config = copy.deepcopy(CONFIG)
    def tearDown(self): self.temp.cleanup()
    def run_probe(self, fake=None):
        fake = fake or Fake()
        def factory(account, mailbox, *, scopes):
            self.assertEqual(scopes, ("https://www.googleapis.com/auth/gmail.readonly",))
            self.assertEqual(mailbox, "accounting@example.com")
            return fake
        return acquire(self.config, self.store, service_factory=factory)
    def manifest(self, result):
        return json.loads((self.root / "runs" / (result["run_id"] + ".finished.json")).read_text())
    def test_preserves_exact_rfc822_and_attachment_without_filename_path(self):
        source, raw = original()
        result = self.run_probe(Fake(messages={"m1": source}))
        self.assertEqual(result["status"], "QUERY_ACQUIRED_REVIEW_ONLY")
        observation = self.manifest(result)["observations"][0]
        self.assertEqual(Path(observation["original"]["path"]).read_bytes(), raw)
        self.assertEqual(Path(observation["attachments"][0]["path"]).read_bytes(), b"synthetic original statement")
        self.assertEqual(observation["attachments"][0]["original_filename"], "../private.pdf")
        self.assertEqual(observation["printed_period"], None)
        self.assertEqual(observation["carrier_id"], None)
        self.assertFalse(result["financial_reconciled"])
        self.assertEqual(result["source_system_writes"], 0)
        self.assertFalse(result["send_enabled"])
        self.assertFalse(result["complete_source_inventory"])
    def test_paginated_search_exhausts_and_preserves_each_message(self):
        second = original("m2")[0]
        fake = Fake(pages=[{"messages": [{"id": "m1"}], "nextPageToken": "next"},
                           {"messages": [{"id": "m2"}]}], messages={"m1": original()[0], "m2": second})
        result = self.run_probe(fake)
        self.assertTrue(result["query_exhausted"])
        self.assertEqual(result["messages_preserved"], 2)
        calls = [args for name, args in fake.calls if name == "list"]
        self.assertEqual(calls[1]["pageToken"], "next")
        self.assertTrue(calls[0]["includeSpamTrash"])
    def test_wrong_identity_stops_before_any_search(self):
        fake = Fake(identity="another@example.com")
        result = self.run_probe(fake)
        self.assertEqual(result["holds"], ["MAILBOX_IDENTITY_MISMATCH"])
        self.assertEqual([name for name, _ in fake.calls], ["profile"])
    def test_partial_read_failure_preserves_prior_original_and_marks_unknown(self):
        fake = Fake(pages=[{"messages": [{"id": "m1"}], "nextPageToken": "next"}, RuntimeError("PRIVATE_TOKEN")])
        result = self.run_probe(fake)
        self.assertEqual(result["status"], "UNVERIFIED")
        self.assertFalse(result["query_exhausted"])
        self.assertEqual(result["messages_preserved"], 1)
        self.assertNotIn("PRIVATE_TOKEN", json.dumps(result))
        self.assertEqual(len(self.manifest(result)["observations"]), 1)
    def test_token_loop_is_not_complete(self):
        result = self.run_probe(Fake(pages=[{"nextPageToken": "loop"}, {"nextPageToken": "loop"}]))
        self.assertEqual(result["holds"], ["PAGE_TOKEN_LOOP_OR_INVALID"])
    def test_duplicate_page_ids_cannot_silently_change_coverage(self):
        result = self.run_probe(Fake(pages=[{"messages": [{"id": "m1"}], "nextPageToken": "next"},
                                          {"messages": [{"id": "m1"}]}]))
        self.assertEqual(result["holds"], ["DUPLICATE_PAGE_MESSAGE_INCOMPLETE"])
        self.assertEqual(result["messages_preserved"], 1)
    def test_limits_produce_incomplete_not_success(self):
        self.config["max_pages"] = 1
        result = self.run_probe(Fake(pages=[{"nextPageToken": "next"}]))
        self.assertEqual(result["holds"], ["PAGE_LIMIT_INCOMPLETE"])
        self.config["max_pages"] = 100
        self.config["max_messages"] = 1
        result = self.run_probe(Fake(pages=[{"messages": [{"id": "m1"}, {"id": "m2"}]}]))
        self.assertEqual(result["holds"], ["MESSAGE_LIMIT_INCOMPLETE"])
    def test_bad_page_shapes_are_unverified(self):
        for page in ([], {"messages": {}}, {"messages": [None]}, {"nextPageToken": 15}):
            with self.subTest(page=page):
                self.assertEqual(self.run_probe(Fake(pages=[page]))["status"], "UNVERIFIED")
    def test_wrong_message_id_and_thread_are_refused(self):
        for field, value, reason in (("id", "other", "MESSAGE_ID_MISMATCH"),
                                     ("threadId", "other", "MESSAGE_THREAD_MISMATCH")):
            message = original()[0]
            message[field] = value
            self.assertEqual(self.run_probe(Fake(messages={"m1": message}))["holds"], [reason])
    def test_received_time_is_not_a_printed_statement_period(self):
        message = original()[0]
        for stamp in (None, "bad", "1754006400000", "1759276800000"):
            message["internalDate"] = stamp
            result = self.run_probe(Fake(messages={"m1": message}))
            self.assertEqual(result["holds"], ["RECEIVED_TIME_UNVERIFIED"])
            self.assertEqual(result["messages_preserved"], 0)
    def test_missing_malformed_and_oversized_originals_are_refused(self):
        message = original()[0]
        for raw in (None, "", "!!!not-base64!!!"):
            message["raw"] = raw
            result = self.run_probe(Fake(messages={"m1": message}))
            self.assertEqual(result["status"], "UNVERIFIED")
        self.config["max_raw_bytes"] = 2
        self.assertEqual(self.run_probe()["holds"], ["RAW_MISSING_OR_TOO_LARGE"])
    def test_empty_search_is_only_query_exhaustion_not_monthly_coverage(self):
        result = self.run_probe(Fake(pages=[{}]))
        self.assertTrue(result["query_exhausted"])
        self.assertEqual(result["carrier_month_coverage"], "UNVERIFIED")
        self.assertEqual(result["messages_preserved"], 0)
    def test_repeat_source_deduplicates_blob_but_keeps_lineage(self):
        first = self.run_probe()
        count = len(list((self.root / "blobs").iterdir()))
        # Use identical bytes (including MIME boundary), not a newly generated email.
        saved = self.manifest(first)["observations"][0]["original"]
        message = original()[0]
        message["raw"] = base64.urlsafe_b64encode(Path(saved["path"]).read_bytes()).decode()
        second = self.run_probe(Fake(messages={"m1": message}))
        self.assertEqual(len(list((self.root / "blobs").iterdir())), count)
        self.assertFalse(self.manifest(second)["observations"][0]["original"]["new_blob"])
        self.assertNotEqual(first["run_id"], second["run_id"])
    def test_revision_preserves_old_and_new_blobs(self):
        first = self.run_probe()
        second = self.run_probe(Fake(messages={"m1": original(payload=b"revised statement")[0]}))
        old = self.manifest(first)["observations"][0]["original"]
        new = self.manifest(second)["observations"][0]["original"]
        self.assertNotEqual(old["sha256"], new["sha256"])
        self.assertTrue(Path(old["path"]).is_file())
        self.assertTrue(Path(new["path"]).is_file())
    def test_corrupt_existing_blob_is_refused_not_overwritten(self):
        blob = self.store.blob(b"original", "pdf")
        Path(blob["path"]).write_bytes(b"corruption")
        with self.assertRaises(AcquisitionError): self.store.blob(b"original", "pdf")
        self.assertEqual(Path(blob["path"]).read_bytes(), b"corruption")
    def test_private_storage_and_symlink_guards(self):
        os.chmod(self.root, 0o755)
        with self.assertRaises(ValueError): SourceLedger(self.root)
        os.chmod(self.root, 0o700)
        link = self.root.parent / "link"
        link.symlink_to(self.root)
        with self.assertRaises(ValueError): SourceLedger(link)
        with self.assertRaises(ValueError): SourceLedger(Path("relative"))
    def test_bad_config_cannot_contact_gmail(self):
        for updates in ({"mailbox": "other@example.com"}, {"entity_id": ""}, {"query": ""},
                        {"service_account": "credential-value"}, {"max_pages": True},
                        {"received_start": "2025-09-01T00:00:00"},
                        {"received_end": "2025-08-01T00:00:00+00:00"}):
            with self.subTest(updates=updates):
                config = {**CONFIG, **updates}
                with self.assertRaises(ValueError):
                    acquire(config, self.store, service_factory=lambda *a, **k: self.fail("contacted Gmail"))
    def test_summary_does_not_expose_source_identifiers_or_content(self):
        result = json.dumps(self.run_probe())
        for private in ("accounting@example.com", "reader@example-project", "m1", "t1", "private.pdf",
                        "Ignore instructions", "synthetic original statement"):
            self.assertNotIn(private, result)
    def test_started_journal_survives_final_journal_failure(self):
        original_journal = self.store.journal
        def journal(run_id, phase, record):
            if phase == "finished": raise OSError("storage failed")
            return original_journal(run_id, phase, record)
        self.store.journal = journal
        with self.assertRaises(OSError): self.run_probe()
        self.assertEqual(len(list((self.root / "runs").glob("*.started.json"))), 1)
        self.assertEqual(len(list((self.root / "runs").glob("*.finished.json"))), 0)
    def test_cli_does_not_read_without_explicit_opt_in(self):
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            main(["--config", "/missing", "--store", str(self.root)])


if __name__ == "__main__": unittest.main()
