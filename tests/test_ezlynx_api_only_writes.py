"""EZLynx notes/docs are API-only. Playwright helpers fail; API path works.

No live EZLynx. HTTP and Playwright are faked.
"""

from __future__ import annotations

import asyncio
import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from robie_job_engine.complete_guard import require_complete_postcondition
from robie_job_engine.ezlynx_api_only_writes import (
    EZLYNX_NOTE_DOC_API_ONLY,
    EzlynxNoteDocReadbackError,
    EzlynxPlaywrightNoteDocForbidden,
    add_note_to_discussion,
    claimed_ezlynx_document_write,
    claimed_ezlynx_note_write,
    confirm_uploaded_document_id,
    note_or_document_write_missing_api_id,
    playwright_note_doc_block_reason,
    refuse_playwright_note_or_doc,
    upload_document_via_api,
)
from robie_job_engine.ezlynx_discussions import DiscussionApiError
from robie_job_engine.ezlynx_policy_setup import EzlynxPolicySetupPage
from robie_job_engine.models import VERIFIER_AUTHORITY, JobStatus
from robie_job_engine.playwright_write_guard import install_playwright_write_guards
from test_ezlynx_discussions import ALLOWED_APPLICANT, _file_routes, make_client
from test_playwright_artifact_fail_closed import (
    _install_hermes_registry_stub,
    _restore_modules,
)
from test_playwright_write_guard_no_delete import _FakeLocator

ROOT = Path(__file__).resolve().parents[1]
NOTE_TOOL = ROOT / "deploy" / "hermes" / "tools" / "ezlynx_note_tool.py"


class _PageLocator(_FakeLocator):
    def __init__(self, selector="", *, aria_label=None, text=None, page=None):
        super().__init__(selector, aria_label=aria_label, text=text)
        self.page = page
        self.filled = []
        self.files = []

    def fill(self, value):
        self.filled.append(value)
        return "filled"

    def set_input_files(self, path):
        self.files.append(path)
        return "uploaded"

    def click(self):
        self.clicked = True
        return "clicked"


class _EzlynxPage:
    def __init__(self, url):
        self.url = url


def _complete_kwargs(**overrides):
    now = datetime.now(timezone.utc).isoformat()
    args = dict(
        current=JobStatus.VERIFYING,
        authority=VERIFIER_AUTHORITY,
        verified=True,
        authoritative=True,
        expected={"applicant_id": "220250093", "policy_number": "TEST-HO-1"},
        observed={"applicant_id": "220250093", "policy_number": "TEST-HO-1"},
        captured_at=now,
        evidence_ref="sha",
        locator="220250093",
        job_id="job-1",
        verifier_authority=VERIFIER_AUTHORITY,
        intended="220250093",
    )
    args.update(overrides)
    return args


class PlaywrightHelperFailClosedTests(unittest.TestCase):
    def test_refuse_helper_raises_clearly(self):
        with self.assertRaises(EzlynxPlaywrightNoteDocForbidden) as ctx:
            refuse_playwright_note_or_doc("unit-test")
        self.assertIn(EZLYNX_NOTE_DOC_API_ONLY, str(ctx.exception))
        self.assertIn("Playwright must never", str(ctx.exception))

    def test_policy_setup_playwright_helper_is_removed(self):
        page = EzlynxPolicySetupPage(page=object())
        with self.assertRaises(EzlynxPlaywrightNoteDocForbidden) as ctx:
            asyncio.run(page.add_discussion_note("Title", "Body"))
        self.assertIn("add_discussion_note", str(ctx.exception))

    def test_live_script_helpers_fail_closed(self):
        script = ROOT / "scripts" / "test_live_policy_setup_full_simulation.py"
        source = script.read_text(encoding="utf-8")
        self.assertIn("refuse_playwright_note_or_doc", source)
        self.assertNotIn("await add_note_btn.click()", source)

    def test_note_selector_click_is_blocked(self):
        locator = _PageLocator("#add-note-header", text="Add Note")
        reason = playwright_note_doc_block_reason(
            locator, method_name="click", selector="#add-note-header"
        )
        self.assertIsNotNone(reason)
        self.assertIn(EZLYNX_NOTE_DOC_API_ONLY, reason)

    def test_save_note_click_is_blocked(self):
        locator = _PageLocator("#btnSaveNote", text="Save Note")
        reason = playwright_note_doc_block_reason(
            locator, method_name="click", selector="#btnSaveNote"
        )
        self.assertIsNotNone(reason)

    def test_note_body_fill_is_blocked(self):
        locator = _PageLocator("#txtNote", text="Note")
        reason = playwright_note_doc_block_reason(
            locator, method_name="fill", selector="#txtNote"
        )
        self.assertIsNotNone(reason)

    def test_ezlynx_set_input_files_is_blocked(self):
        page = _EzlynxPage("https://app.ezlynx.com/web/account/220250093/documents")
        locator = _PageLocator("input[type=file]", page=page)
        reason = playwright_note_doc_block_reason(
            locator,
            method_name="set_input_files",
            selector="input[type=file]",
            page_url=page.url,
        )
        self.assertIsNotNone(reason)
        self.assertIn("DocumentApi", reason)

    def test_ascend_file_input_is_allowed(self):
        page = _EzlynxPage("https://dashboard.useascend.com/create/new")
        locator = _PageLocator("input[type=file]", text="Import document", page=page)
        reason = playwright_note_doc_block_reason(
            locator,
            method_name="set_input_files",
            selector="input[type=file]",
            page_url=page.url,
        )
        self.assertIsNone(reason)

    def test_benign_ezlynx_save_form_is_allowed(self):
        page = _EzlynxPage("https://app.ezlynx.com/web/account/220250093/policies")
        locator = _PageLocator("#finishButton-header", text="Save & Close", page=page)
        reason = playwright_note_doc_block_reason(
            locator,
            method_name="click",
            selector="#finishButton-header",
            page_url=page.url,
        )
        self.assertIsNone(reason)

    def test_wrapped_save_note_click_raises_hard(self):
        class FakeLocatorClass:
            def click(self):
                return "clicked"

        scope = {"Locator": FakeLocatorClass}
        install_playwright_write_guards(scope)
        locator = FakeLocatorClass()
        locator._selector = "#btnSaveNote"
        locator.get_attribute = lambda name: None
        locator.inner_text = lambda: "Save Note"
        locator.text_content = lambda: "Save Note"
        with self.assertRaisesRegex(RuntimeError, EZLYNX_NOTE_DOC_API_ONLY):
            locator.click()

    def test_wrapped_ezlynx_set_input_files_raises_hard(self):
        class FakeLocatorClass:
            def set_input_files(self, path):
                return path

        scope = {"Locator": FakeLocatorClass}
        install_playwright_write_guards(scope)
        locator = FakeLocatorClass()
        locator._selector = "input[type=file]"
        locator.page = _EzlynxPage(
            "https://app.ezlynx.com/web/account/220250093/documents"
        )
        locator.get_attribute = lambda name: None
        locator.inner_text = lambda: "Choose file"
        locator.text_content = lambda: "Choose file"
        with self.assertRaisesRegex(RuntimeError, EZLYNX_NOTE_DOC_API_ONLY):
            locator.set_input_files("/tmp/dec.pdf")


class CompleteGateTests(unittest.TestCase):
    def test_policy_only_complete_still_allowed(self):
        require_complete_postcondition(**_complete_kwargs())

    def test_discussion_title_receipt_is_not_a_note_claim(self):
        self.assertFalse(
            claimed_ezlynx_note_write(
                expected={"discussion_title": "Bond", "policy_number": "1"},
                observed={"discussion_title": "Bond", "policy_number": "1"},
            )
        )
        self.assertIsNone(
            note_or_document_write_missing_api_id(
                expected={"discussion_title": "Bond", "policy_number": "1"},
                observed={"discussion_title": "Bond", "policy_number": "1"},
            )
        )

    def test_note_claim_without_api_id_blocks_complete(self):
        with self.assertRaises(PermissionError) as ctx:
            require_complete_postcondition(
                **_complete_kwargs(
                    expected={
                        "applicant_id": "220250093",
                        "policy_number": "TEST-HO-1",
                        "note_id": None,
                    },
                    observed={
                        "applicant_id": "220250093",
                        "policy_number": "TEST-HO-1",
                    },
                    action={"action_type": "ezlynx.note"},
                )
            )
        self.assertIn("note_id", str(ctx.exception))
        self.assertIn("COMPLETE prohibited", str(ctx.exception))

    def test_document_claim_without_api_id_blocks_complete(self):
        reason = note_or_document_write_missing_api_id(
            expected={"document_names": ["dec.pdf"], "applicant_id": "220250093"},
            observed={"document_names": ["dec.pdf"], "applicant_id": "220250093"},
        )
        self.assertIsNotNone(reason)
        self.assertIn("document_id", reason)

    def test_note_claim_with_api_id_is_allowed(self):
        require_complete_postcondition(
            **_complete_kwargs(
                expected={
                    "applicant_id": "220250093",
                    "policy_number": "TEST-HO-1",
                    "note_id": "n7",
                },
                observed={
                    "applicant_id": "220250093",
                    "policy_number": "TEST-HO-1",
                    "note_id": "n7",
                },
            )
        )

    def test_document_claim_with_api_id_is_allowed(self):
        require_complete_postcondition(
            **_complete_kwargs(
                expected={
                    "applicant_id": "220250093",
                    "policy_number": "TEST-HO-1",
                    "document_id": "818921949",
                },
                observed={
                    "applicant_id": "220250093",
                    "policy_number": "TEST-HO-1",
                    "document_id": "818921949",
                },
            )
        )

    def test_playwright_path_blocks_complete_even_with_fabricated_id(self):
        reason = note_or_document_write_missing_api_id(
            expected={"note_id": "n7"},
            observed={
                "note_id": "n7",
                "playwright_note_or_doc_write": True,
            },
        )
        self.assertIsNotNone(reason)
        self.assertIn("Playwright path", reason)

    def test_document_upload_action_type_requires_id(self):
        self.assertTrue(
            claimed_ezlynx_document_write(
                payload={"action_type": "ezlynx.document_upload"}
            )
        )

    def test_apply_label_document_name_is_not_an_upload_claim(self):
        self.assertFalse(
            claimed_ezlynx_document_write(
                expected={
                    "resource_id": "test-document-1",
                    "document_name": "je-kill-1.pdf",
                    "label": "JE-KILL-01",
                },
                observed={
                    "resource_id": "test-document-1",
                    "document_name": "je-kill-1.pdf",
                    "label": "JE-KILL-01",
                },
                payload={"document_name": "je-kill-1.pdf", "action_type": "ezlynx.apply_label"},
            )
        )
        self.assertIsNone(
            note_or_document_write_missing_api_id(
                expected={
                    "resource_id": "test-document-1",
                    "document_name": "je-kill-1.pdf",
                },
                observed={
                    "resource_id": "test-document-1",
                    "document_name": "je-kill-1.pdf",
                },
                payload={"document_name": "je-kill-1.pdf"},
            )
        )


class ApiPathStillWorksTests(unittest.TestCase):
    def test_add_note_to_discussion_files_and_reads_back(self):
        client = make_client(_file_routes([{"discussionId": "d1", "title": "PCR"}]))
        result = add_note_to_discussion(
            ALLOWED_APPLICANT,
            "Filed note via API",
            title_hint="PCR",
            discussion_client=client,
        )
        self.assertEqual(result["status"], "filed")
        self.assertEqual(result["note_id"], "n7")
        self.assertTrue(result["read_back"])
        self.assertEqual(len(client._urlopen.posts_to("/notes")), 1)

    def test_file_note_readback_miss_fails_closed(self):
        from robie_job_engine import ezlynx_discussions as disc

        routes = [
            ("connect/token", {"access_token": "tok123", "expires_in": 3600}),
            ("by-applicant", [{"discussionId": "d1", "title": "PCR"}]),
            ("/notes", {"noteId": "n7"}),
            ("v8/discussions/", {"discussionId": "d1", "notes": []}),
        ]
        client = make_client(routes)
        with self.assertRaises((EzlynxNoteDocReadbackError, DiscussionApiError)):
            disc.file_note_to_existing_discussion(
                client, ALLOWED_APPLICANT, "Filed note"
            )

    def test_upload_document_via_api_reads_back(self):
        class FakeClient:
            def __init__(self):
                self.uploaded = False

            def upload_applicant_document(self, applicant_id, name, data, **kwargs):
                self.uploaded = True
                return "818921949"

            def search_applicant_documents(self, applicant_id):
                return {"results": [{"id": "818921949", "name": "dec.pdf"}]}

        result = upload_document_via_api(
            ALLOWED_APPLICANT,
            "dec.pdf",
            b"%PDF-1.4 x",
            client=FakeClient(),
        )
        self.assertEqual(result["document_id"], "818921949")
        self.assertTrue(result["read_back"])

    def test_upload_readback_miss_fails_closed(self):
        class FakeClient:
            def upload_applicant_document(self, *args, **kwargs):
                return "818921949"

            def search_applicant_documents(self, applicant_id):
                return {"results": []}

        with self.assertRaises(EzlynxNoteDocReadbackError):
            confirm_uploaded_document_id(FakeClient(), ALLOWED_APPLICANT, "818921949")


class NoteToolTests(unittest.TestCase):
    def _load(self):
        previous = _install_hermes_registry_stub()
        spec = importlib.util.spec_from_file_location(
            "robie_note_tool_under_test", NOTE_TOOL
        )
        module = importlib.util.module_from_spec(spec)
        assert spec is not None and spec.loader is not None
        try:
            spec.loader.exec_module(module)
        except Exception:
            _restore_modules(previous)
            raise
        return module, previous

    def test_schema_requires_applicant_and_body(self):
        tool, previous = self._load()
        try:
            required = tool.DISCUSSION_NOTE_SCHEMA["parameters"]["required"]
            self.assertEqual(sorted(required), ["applicant_id", "note_text"])
            self.assertEqual(tool.DISCUSSION_NOTE_SCHEMA["name"], "ezlynx_discussion_note")
        finally:
            _restore_modules(previous)

    def test_handler_uses_add_note_to_discussion(self):
        tool, previous = self._load()
        try:
            from durable_temp import durable_temporary_directory
            from robie_job_engine.store import JobStore

            with durable_temporary_directory() as tmp:
                db = str(Path(tmp) / "jobs.db")
                job = JobStore(db).create_job(
                    "hermes.plain_english", {"text": "hello"}
                )
                with patch(
                    "robie_job_engine.ezlynx_api_only_writes.add_note_to_discussion",
                    return_value={
                        "status": "filed",
                        "note_id": "n7",
                        "discussion_id": "d1",
                        "discussion_title": "PCR",
                        "read_back": True,
                    },
                ) as mocked:
                    result = tool.ezlynx_discussion_note_handler(
                        {
                            "applicant_id": ALLOWED_APPLICANT,
                            "note_text": "Hello\n\nRobie was here",
                            "title_hint": "PCR",
                        },
                        job_id=job["id"],
                        db_path=db,
                    )
            self.assertTrue(result["ok"])
            self.assertEqual(result["note_id"], "n7")
            self.assertIn("Robie was here", result["note_text"])
            mocked.assert_called_once()
        finally:
            _restore_modules(previous)

    def test_checkpoint_keeps_note_text_for_discussion_readback(self):
        from durable_temp import durable_temporary_directory

        from robie_job_engine.store import JobStore

        tool, previous = self._load()
        try:
            with durable_temporary_directory() as tmp:
                db = str(Path(tmp) / "jobs.db")
                job = JobStore(db).create_job(
                    "hermes.plain_english", {"text": "hello"}
                )
                with patch(
                    "robie_job_engine.ezlynx_api_only_writes.add_note_to_discussion",
                    return_value={
                        "status": "filed",
                        "note_id": "n7",
                        "discussion_id": "848144886",
                        "discussion_title": "follw up 1",
                        "read_back": True,
                    },
                ):
                    tool.ezlynx_discussion_note_handler(
                        {
                            "applicant_id": ALLOWED_APPLICANT,
                            "note_text": "Hello\n\nRobie was here",
                            "title_hint": "follw up 1",
                        },
                        job_id=job["id"],
                        db_path=db,
                    )
                saved = JobStore(db).get_checkpoint(job["id"], "discussion_note")
            self.assertEqual(saved["note_text"], "Hello\n\nRobie was here")
            self.assertEqual(saved["discussion_id"], "848144886")
            self.assertEqual(saved["applicant_id"], ALLOWED_APPLICANT)
            self.assertEqual(saved["discussion_title"], "follw up 1")
        finally:
            _restore_modules(previous)

    def test_handler_fails_closed_without_note_id(self):
        tool, previous = self._load()
        try:
            from durable_temp import durable_temporary_directory
            from robie_job_engine.store import JobStore

            with durable_temporary_directory() as tmp:
                db = str(Path(tmp) / "jobs.db")
                job = JobStore(db).create_job(
                    "hermes.plain_english", {"text": "hello"}
                )
                with patch(
                    "robie_job_engine.ezlynx_api_only_writes.add_note_to_discussion",
                    return_value={"status": "pending", "reason": "no titled discussion"},
                ):
                    result = tool.ezlynx_discussion_note_handler(
                        {"applicant_id": ALLOWED_APPLICANT, "note_text": "x"},
                        job_id=job["id"],
                        db_path=db,
                    )
            self.assertFalse(result["ok"])
            self.assertIn("error", result)
        finally:
            _restore_modules(previous)


class AgentsDocsContractTests(unittest.TestCase):
    def test_agents_and_docs_name_the_api_only_rule(self):
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        docs = (ROOT / "docs" / "EZLYNX_NOTES_DOCS_API_ONLY.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("API only", agents)
        self.assertIn("DiscussionApi", agents)
        self.assertIn("DocumentApi", agents)
        self.assertIn("Playwright must never", docs)
        self.assertIn("note_id", docs)


if __name__ == "__main__":
    unittest.main()
