"""4372 mortgagee enrichment scaffold — no network, no live EZLynx.

Locks: multi-mortgage, conflict→HITL, empty→prove zero, no invented
fields, no PDF scrape, no SSN, Playwright note/doc writes fail closed.
"""

from __future__ import annotations

import csv
import io
from datetime import date

import pytest

from robie_job_engine import gmail_report_ingestion as ing
from robie_job_engine import mortgagee_enrichment as menc
from robie_job_engine import verification_workers as vw
from robie_job_engine.ezlynx_api_only_writes import (
    EZLYNX_NOTE_DOC_API_ONLY,
    EzlynxPlaywrightNoteDocForbidden,
    PLAYWRIGHT_BLOCKED,
)


DAY = date(2026, 9, 19)


class FakePolicy:
    def __init__(self, payload, *, downloads=None):
        self.payload = payload
        self.calls = []
        self.downloads = downloads or []

    def search_policy_by_number(self, policy_number):
        self.calls.append(("search_policy_by_number", policy_number))
        return self.payload

    def download_document(self, document_id):
        self.downloads.append(document_id)
        raise AssertionError("PDF download must not be used for field fill")


class FakeDocs:
    def __init__(self, payload, *, downloads=None):
        self.payload = payload
        self.calls = []
        self.downloads = downloads or []

    def search_applicant_documents(self, applicant_id):
        self.calls.append(("search_applicant_documents", applicant_id))
        return self.payload

    def download_document(self, document_id):
        self.downloads.append(document_id)
        raise AssertionError("PDF download must not be used for field fill")


def _ports(policy=None, documents=None):
    return menc.EnrichmentPorts(policy=policy, documents=documents)


def _policy_payload(*mortgages, include_empty_list=False, property_zip=""):
    body = {"policyNumber": "HO-1", "status": "Active"}
    if mortgages or include_empty_list:
        body["mortgagees"] = list(mortgages)
    if property_zip:
        body["property_zip"] = property_zip
    return {"status": "success", "data": body}


def _dec_row(doc_id, name, **fields):
    row = {"id": doc_id, "name": name, "documentName": name}
    row.update(fields)
    return row


def _csv_bytes(rows):
    headers = ing.expected_headers("4372")
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({header: row.get(header, "") for header in headers})
    return buffer.getvalue().encode("utf-8")


def _row4372(policy, account="Test Account", status="Open", **overrides):
    row = {
        "Applicant ID": "220250093",
        "Account Name": account,
        "Policy Number": policy,
        "Task Due Date": "10/15/2026",
        "Task Status": status,
        "Department": "Personal Lines",
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# Multi-mortgage
# ---------------------------------------------------------------------------


class TestMultiMortgage:
    def test_two_mortgages_emitted_separately_when_sources_agree(self):
        policy = FakePolicy(_policy_payload(
            {"MortgageeName": "First National Bank", "LoanNumber": "LN-111"},
            {"MortgageeName": "Second Servicing LLC", "LoanNumber": "LN-222"},
        ))
        docs = FakeDocs({
            "results": [
                _dec_row("1001", "HO Declarations",
                         MortgageeName="First National Bank", LoanNumber="LN-111"),
                _dec_row("1002", "HO Declarations — 2nd mortgage",
                         MortgageeName="Second Servicing LLC", LoanNumber="LN-222"),
            ]
        })
        result = menc.enrich_work_item(
            policy_number="HO-1",
            applicant_id="220250093",
            ports=_ports(policy, docs),
            dry_run=True,
        )
        assert result.status == menc.STATUS_READY
        assert [m.loan_number for m in result.mortgages] == ["LN-111", "LN-222"]
        assert [m.lender_name for m in result.mortgages] == [
            "First National Bank",
            "Second Servicing LLC",
        ]
        assert all(m.source == "agreed" for m in result.mortgages)
        assert policy.downloads == []
        assert docs.downloads == []

    def test_does_not_merge_two_loans_into_one_guessed_lender(self):
        policy = FakePolicy(_policy_payload(
            {"lender_name": "Alpha", "loan_number": "A-1"},
            {"lender_name": "Beta", "loan_number": "B-2"},
        ))
        docs = FakeDocs({
            "results": [
                _dec_row("9", "Declarations Page",
                         lender="Alpha", loan_number="A-1"),
                _dec_row("8", "Dec page endorsement",
                         lender="Beta", loan_number="B-2"),
            ]
        })
        result = menc.enrich_work_item(
            policy_number="HO-1", applicant_id="1", ports=_ports(policy, docs),
        )
        assert len(result.mortgages) == 2
        lenders = {m.lender_name for m in result.mortgages}
        assert lenders == {"Alpha", "Beta"}
        assert "Alpha / Beta" not in lenders


# ---------------------------------------------------------------------------
# Conflict → HITL
# ---------------------------------------------------------------------------


class TestConflictHitl:
    def test_same_loan_different_lender_is_hitl_does_not_pick_a_side(self):
        policy = FakePolicy(_policy_payload(
            {"MortgageeName": "Wells Fargo", "LoanNumber": "LN-1"},
        ))
        docs = FakeDocs({
            "results": [
                _dec_row("55", "Declaration",
                         MortgageeName="Chase", LoanNumber="LN-1"),
            ]
        })
        result = menc.enrich_work_item(
            policy_number="HO-1", applicant_id="1", ports=_ports(policy, docs),
        )
        assert result.status == menc.STATUS_HITL
        assert "HITL" in result.reason
        assert result.mortgages == []
        kind, detail, target, status, reason = menc.plan_from_enrichment(
            result, due_txt="task due 2026-10-15",
        )
        assert status == "blocked"
        assert target == "HITL"
        assert "lender=" not in detail
        assert "Wells Fargo" not in target and "Chase" not in target

    def test_disjoint_loan_sets_are_hitl(self):
        policy = FakePolicy(_policy_payload(
            {"MortgageeName": "Alpha", "LoanNumber": "111"},
        ))
        docs = FakeDocs({
            "results": [
                _dec_row("1", "Declarations",
                         MortgageeName="Alpha", LoanNumber="999"),
            ]
        })
        result = menc.enrich_work_item(
            policy_number="HO-1", applicant_id="1", ports=_ports(policy, docs),
        )
        assert result.status == menc.STATUS_HITL
        assert result.mortgages == []

    def test_policy_empty_dec_has_mortgage_is_hitl(self):
        policy = FakePolicy(_policy_payload(include_empty_list=True))
        docs = FakeDocs({
            "mortgagees": [{"MortgageeName": "Alpha", "LoanNumber": "111"}],
            "results": [_dec_row("1", "HO Declaration")],
        })
        result = menc.enrich_work_item(
            policy_number="HO-1", applicant_id="1", ports=_ports(policy, docs),
        )
        assert result.status == menc.STATUS_HITL
        assert result.mortgages == []

    def test_intra_record_field_disagreement_is_hitl(self):
        policy = FakePolicy({
            "data": {
                "mortgagees": [{
                    "MortgageeName": "Alpha",
                    "LenderName": "Beta",
                    "LoanNumber": "111",
                }]
            }
        })
        docs = FakeDocs({"results": [_dec_row("1", "Declaration")],
                         "mortgagees": []})
        result = menc.enrich_work_item(
            policy_number="HO-1", applicant_id="1", ports=_ports(policy, docs),
        )
        assert result.status == menc.STATUS_HITL
        assert "not picking a side" in result.reason


# ---------------------------------------------------------------------------
# Empty → prove zero (never silent skip)
# ---------------------------------------------------------------------------


class TestProveZero:
    def test_explicit_empty_collections_on_both_sources_is_proven_zero(self):
        policy = FakePolicy(_policy_payload(include_empty_list=True))
        docs = FakeDocs({"results": [], "mortgagees": []})
        result = menc.enrich_work_item(
            policy_number="HO-1", applicant_id="1", ports=_ports(policy, docs),
        )
        assert result.status == menc.STATUS_PROVEN_ZERO
        assert result.mortgages == []
        assert "explicit empty" in result.reason

    def test_unbound_ports_are_incomplete_not_zero(self):
        result = menc.enrich_work_item(
            policy_number="HO-1", applicant_id="1",
            ports=_ports(), dry_run=True,
        )
        assert result.status == menc.STATUS_INCOMPLETE
        assert result.status != menc.STATUS_PROVEN_ZERO
        assert "lender/loan not on file" in result.reason

    def test_missing_collection_key_is_not_proven_zero(self):
        policy = FakePolicy({"data": {"policyNumber": "HO-1"}})
        docs = FakeDocs({"results": [_dec_row("1", "HO Declaration")]})
        result = menc.enrich_work_item(
            policy_number="HO-1", applicant_id="1", ports=_ports(policy, docs),
        )
        assert result.status == menc.STATUS_INCOMPLETE
        assert result.status != menc.STATUS_PROVEN_ZERO
        assert result.mortgages == []

    def test_failed_document_search_is_incomplete_not_zero(self):
        class Boom:
            def search_applicant_documents(self, applicant_id):
                raise RuntimeError("timeout")

        policy = FakePolicy(_policy_payload(include_empty_list=True))
        result = menc.enrich_work_item(
            policy_number="HO-1", applicant_id="1",
            ports=_ports(policy, Boom()),
        )
        assert result.status == menc.STATUS_INCOMPLETE
        assert result.status != menc.STATUS_PROVEN_ZERO

    def test_run_worker_does_not_drop_item_when_zero_unproven(self, tmp_path):
        run = vw.run_worker(
            "4372", day=DAY, mode="dry_run", queue_dir=str(tmp_path),
            csv_bytes=_csv_bytes([_row4372("OPEN1")]),
        )
        assert run.work_items == 1
        assert len(run.actions) == 1
        assert run.actions[0].status == "blocked"
        assert run.enrichment[0]["status"] == menc.STATUS_INCOMPLETE


# ---------------------------------------------------------------------------
# Never invent / never PDF scrape
# ---------------------------------------------------------------------------


class TestNoInventedFields:
    def test_partial_fields_are_not_filled_in(self):
        payload = {"data": {"mortgagees": [{"MortgageeName": "Alpha"}]}}
        mortgages, conflict, saw = menc.extract_structured_mortgages(
            payload, source="policy",
        )
        assert mortgages == []
        assert saw is True
        assert "will not invent" in conflict

    def test_document_title_is_not_a_lender_name(self):
        mortgages, conflict, _ = menc.extract_structured_mortgages(
            {"results": [_dec_row("1", "Wells Fargo Declaration")]},
            source="declaration",
        )
        assert mortgages == []
        assert conflict == ""

    def test_ocr_blob_is_refused_not_scraped(self):
        with pytest.raises(menc.MortgageeEnrichmentError, match="scrape"):
            menc.extract_structured_mortgages(
                {
                    "ocr_text": "MORTGAGEE: Guessed Bank LOAN: 999",
                    "mortgagees": [{"MortgageeName": "Guessed Bank", "LoanNumber": "999"}],
                },
                source="declaration",
            )

    def test_pdf_text_on_policy_payload_halts_without_invented_mortgage(self):
        policy = FakePolicy({
            "data": {
                "pdf_text": "lender should not be read from this",
                "mortgagees": [{"MortgageeName": "Scraped", "LoanNumber": "1"}],
            }
        })
        docs = FakeDocs({"results": [], "mortgagees": []})
        result = menc.enrich_work_item(
            policy_number="HO-1", applicant_id="1", ports=_ports(policy, docs),
        )
        assert result.status == menc.STATUS_HITL
        assert result.mortgages == []
        assert "scrape" in result.reason.casefold() or "pdf" in result.reason.casefold()

    def test_bare_string_in_mortgagees_list_is_not_a_match(self):
        mortgages, conflict, saw = menc.extract_structured_mortgages(
            {"mortgagees": ["First National Bank"]},
            source="policy",
        )
        assert saw is True
        assert mortgages == []
        assert conflict == ""


# ---------------------------------------------------------------------------
# SSN
# ---------------------------------------------------------------------------


class TestNoSsn:
    def test_ssn_field_is_refused(self):
        with pytest.raises(menc.MortgageeEnrichmentError, match="SSN"):
            menc.extract_structured_mortgages(
                {"ssn": "123-45-6789", "MortgageeName": "A", "LoanNumber": "1"},
                source="policy",
            )

    def test_ssn_shaped_loan_value_is_refused(self):
        with pytest.raises(menc.MortgageeEnrichmentError, match="SSN"):
            menc.MortgageRecord(
                lender_name="Alpha", loan_number="123-45-6789", source="policy",
            )

    def test_output_dict_never_carries_ssn_keys(self):
        result = menc.enrich_work_item(
            policy_number="HO-1", applicant_id="1", ports=_ports(), dry_run=True,
        )
        blob = repr(result.to_dict()).casefold()
        assert "ssn" not in blob
        assert "123-45-6789" not in blob


# ---------------------------------------------------------------------------
# Playwright fail-closed + dry-run note path
# ---------------------------------------------------------------------------


class TestNotesDocsApiOnly:
    def test_playwright_note_write_is_refused(self):
        with pytest.raises(EzlynxPlaywrightNoteDocForbidden) as exc:
            menc.file_enrichment_note(
                applicant_id="220250093",
                note_text="enrichment",
                via="playwright",
            )
        text = str(exc.value)
        assert PLAYWRIGHT_BLOCKED in text
        assert EZLYNX_NOTE_DOC_API_ONLY in text

    def test_cdp_and_file_chooser_are_refused(self):
        for via in ("cdp", "file_chooser", "browser"):
            with pytest.raises(EzlynxPlaywrightNoteDocForbidden):
                menc.file_enrichment_note(
                    applicant_id="1", note_text="x", via=via,
                )

    def test_dry_run_api_note_does_not_claim_a_note_id(self):
        filed = menc.file_enrichment_note(
            applicant_id="220250093",
            note_text="dry enrichment",
            via="discussion_api",
            dry_run=True,
        )
        assert filed["executed"] is False
        assert filed["read_back"] is False
        assert "note_id" not in filed


# ---------------------------------------------------------------------------
# DocumentApi read-back / declaration detection
# ---------------------------------------------------------------------------


class TestDocumentApiLookup:
    def test_declaration_docs_require_numeric_id_read_back(self):
        payload = {
            "results": [
                _dec_row("88001", "Homeowners Declaration"),
                {"id": "not-a-number", "name": "Declaration"},
                {"documentUrl": "https://old/wrong", "name": "Declaration"},
            ]
        }
        docs = menc._declaration_docs_from_search(payload)
        assert [d.document_id for d in docs] == ["88001"]
        assert docs[0].read_back is True

    def test_thin_client_confirms_ids_and_never_downloads(self):
        fake = FakeDocs({
            "results": [_dec_row("42", "Declarations Page",
                                 MortgageeName="A", LoanNumber="1")]
        })
        lookup = menc.ThinDocumentApiLookup(fake)
        payload = lookup.search_applicant_documents("220250093")
        assert menc._document_ids_present(payload, ["42"])
        assert fake.downloads == []

    def test_looks_like_declaration_does_not_match_december(self):
        assert menc.looks_like_declaration({"name": "December invoice"}) is False
        assert menc.looks_like_declaration({"name": "HO Declaration"}) is True
        assert menc.looks_like_declaration({"name": "DEC page"}) is True
        assert menc.looks_like_declaration({"documentType": "Declaration"}) is True


# ---------------------------------------------------------------------------
# Wiring into 4372 worker
# ---------------------------------------------------------------------------


class TestWorkerWiring:
    def test_closed_tasks_are_not_enriched(self, tmp_path):
        rows = [
            _row4372("CLOSED1", status="Closed", **{
                "Task Closed Date": "2025-12-12",
                "Task Closed By": "Tester",
            }),
            _row4372("OPEN1"),
        ]
        run = vw.run_worker(
            "4372", day=DAY, mode="dry_run", queue_dir=str(tmp_path),
            csv_bytes=_csv_bytes(rows),
        )
        assert run.work_items == 1
        assert run.actions[0].policy_number == "OPEN1"
        assert [e["policy_number"] for e in run.enrichment] == ["OPEN1"]

    def _ready_ports(self, *, property_zip="08527"):
        return _ports(
            FakePolicy(_policy_payload(
                {"MortgageeName": "Alpha", "LoanNumber": "A-1"},
                {"MortgageeName": "Beta", "LoanNumber": "B-2"},
                property_zip=property_zip,
            )),
            FakeDocs({
                "results": [
                    _dec_row("1", "Declaration",
                             MortgageeName="Alpha", LoanNumber="A-1"),
                    _dec_row("2", "Dec page",
                             MortgageeName="Beta", LoanNumber="B-2"),
                ]
            }),
        )

    def test_injected_ports_ready_runs_verify_lender_per_mortgage(self, tmp_path):
        run = vw.run_worker(
            "4372", day=DAY, mode="dry_run", queue_dir=str(tmp_path),
            csv_bytes=_csv_bytes([_row4372("HO-1")]),
            enrichment_ports=self._ready_ports(),
        )
        assert run.enrichment[0]["status"] == menc.STATUS_READY
        checks = run.enrichment[0]["lender_checks"]
        assert len(checks) == 2
        assert [c["loan_number"] for c in checks] == ["A-1", "B-2"]
        assert all(c["verify_ok"] is True for c in checks)
        assert all(c["property_zip"] == "08527" for c in checks)
        # Producer gate still blocks delivery (CSV has no review flag).
        assert run.actions[0].status == "blocked"
        assert "producer_review_complete" in run.actions[0].reason
        assert "lender/loan not on file" not in run.actions[0].reason
        detail = run.actions[0].action.detail
        assert "mortgage 1:" in detail and "mortgage 2:" in detail
        assert "verify_lender=pass" in detail

    def test_injected_conflict_marks_hitl(self, tmp_path):
        ports = _ports(
            FakePolicy(_policy_payload(
                {"MortgageeName": "Alpha", "LoanNumber": "1"},
            )),
            FakeDocs({
                "results": [
                    _dec_row("1", "Declaration",
                             MortgageeName="Omega", LoanNumber="1"),
                ]
            }),
        )
        run = vw.run_worker(
            "4372", day=DAY, mode="dry_run", queue_dir=str(tmp_path),
            csv_bytes=_csv_bytes([_row4372("HO-1")]),
            enrichment_ports=ports,
        )
        assert run.enrichment[0]["status"] == menc.STATUS_HITL
        assert run.actions[0].status == "blocked"
        assert "HITL" in run.actions[0].reason

    def test_proven_zero_waits_and_keeps_the_work_item(self, tmp_path):
        ports = _ports(
            FakePolicy(_policy_payload(include_empty_list=True)),
            FakeDocs({"results": [], "mortgagees": []}),
        )
        run = vw.run_worker(
            "4372", day=DAY, mode="dry_run", queue_dir=str(tmp_path),
            csv_bytes=_csv_bytes([_row4372("HO-1")]),
            enrichment_ports=ports,
        )
        assert run.work_items == 1
        assert run.actions[0].status == "waiting"
        assert "zero mortgages proven" in run.actions[0].reason

    def test_default_dry_run_stays_blocked_on_lender(self, tmp_path):
        run = vw.run_worker(
            "4372", day=DAY, mode="dry_run", queue_dir=str(tmp_path),
            csv_bytes=_csv_bytes([_row4372("HO-1"), _row4372("HO-2")]),
        )
        assert run.work_items == 2
        assert all(a.status == "blocked" for a in run.actions)
        assert all("lender" in a.reason.casefold() for a in run.actions)
        assert all(e["dry_run"] is True for e in run.enrichment)


# ---------------------------------------------------------------------------
# Ready → verify_lender; HITL / zero / incomplete do not
# ---------------------------------------------------------------------------


def _ready_result(**kwargs):
    mortgages = [
        menc.MortgageRecord("Alpha", "A-1", "agreed"),
        menc.MortgageRecord("Beta", "B-2", "agreed"),
    ]
    return menc.EnrichmentResult(
        policy_number="HO-1",
        applicant_id="220250093",
        status=menc.STATUS_READY,
        reason="enriched 2 mortgage(s)",
        dry_run=True,
        mortgages=mortgages,
        property_zip="08527",
        **kwargs,
    )


class TestReadyLenderChecks:
    def test_verify_lender_called_once_per_mortgage(self):
        calls = []

        def spy(company, loan, zip_code):
            calls.append((company, loan, zip_code))
            return True, "lender verified"

        def of_record(*_args):
            return False, "portal lookup not performed"

        checks = menc.check_ready_mortgages(
            _ready_result().mortgages,
            property_zip="08527",
            verify_lender_fn=spy,
            verify_of_record_fn=of_record,
        )
        assert calls == [("Alpha", "A-1", "08527"), ("Beta", "B-2", "08527")]
        assert [c.verify_ok for c in checks] == [True, True]
        assert all("portal" in c.of_record_reason for c in checks)

    def test_ready_plus_producer_clear_is_waiting_not_missing_lender(self):
        kind, detail, target, status, reason = menc.plan_from_enrichment(
            _ready_result(),
            due_txt="task due 2026-10-15",
            property_zip="08527",
            producer_state={"producer_review_complete": True},
        )
        assert status == "waiting"
        assert "lender inputs verified" in reason
        assert "lender/loan not on file" not in reason
        assert "verify_lender=pass" in detail
        assert target == "Alpha"

    def test_conflict_hitl_does_not_call_verify_lender(self):
        def boom(*_args, **_kwargs):
            raise AssertionError("verify_lender must not run on HITL")

        result = menc.EnrichmentResult(
            policy_number="HO-1", applicant_id="1",
            status=menc.STATUS_HITL, reason=menc.REASON_HITL_CONFLICT,
            dry_run=True,
        )
        _kind, _detail, target, status, reason = menc.plan_from_enrichment(
            result, due_txt="x", verify_lender_fn=boom,
        )
        assert status == "blocked"
        assert target == "HITL"
        assert "HITL" in reason

    def test_proven_zero_still_skips_without_verify_lender(self):
        def boom(*_args, **_kwargs):
            raise AssertionError("verify_lender must not run on proven_zero")

        result = menc.EnrichmentResult(
            policy_number="HO-1", applicant_id="1",
            status=menc.STATUS_PROVEN_ZERO, reason=menc.REASON_PROVEN_ZERO,
            dry_run=True,
        )
        _kind, _detail, _target, status, reason = menc.plan_from_enrichment(
            result, due_txt="x", verify_lender_fn=boom,
        )
        assert status == "waiting"
        assert "zero mortgages proven" in reason

    def test_incomplete_still_blocked_without_verify_lender(self):
        def boom(*_args, **_kwargs):
            raise AssertionError("verify_lender must not run on incomplete")

        result = menc.EnrichmentResult(
            policy_number="HO-1", applicant_id="1",
            status=menc.STATUS_INCOMPLETE,
            reason=menc.REASON_LENDER_NOT_ON_FILE,
            dry_run=True,
        )
        _kind, _detail, _target, status, reason = menc.plan_from_enrichment(
            result, due_txt="x", verify_lender_fn=boom,
        )
        assert status == "blocked"
        assert "lender/loan not on file" in reason


class TestTestOnlyClientFactory:
    def test_resolve_default_is_unbound(self, monkeypatch):
        monkeypatch.delenv(menc.ENRICHMENT_LIVE_TEST_FLAG, raising=False)
        monkeypatch.setenv("ROBIE_ENV", "TEST")
        ports = menc.resolve_enrichment_ports()
        assert ports.policy is None
        assert ports.documents is None

    def test_bind_refuses_production_env_and_never_loads_config(self, monkeypatch):
        monkeypatch.setenv("ROBIE_ENV", "PRODUCTION")
        loaded = []

        def load(environment=None, accessor=None):
            loaded.append(environment)
            return object()

        with pytest.raises(menc.ProductionClientRefused, match="Production"):
            menc.bind_test_enrichment_ports(
                load_config=load, client_cls=lambda config: config,
            )
        assert loaded == []

    def test_bind_refuses_explicit_prod_request(self, monkeypatch):
        monkeypatch.setenv("ROBIE_ENV", "TEST")
        with pytest.raises(menc.ProductionClientRefused, match="Production"):
            menc.bind_test_enrichment_ports(environment="PRODUCTION")

    def test_bind_test_passes_TEST_not_prod_to_loader(self, monkeypatch):
        monkeypatch.setenv("ROBIE_ENV", "TEST")
        loaded = []

        class Client:
            def __init__(self, config):
                self.config = config

        def load(environment=None, accessor=None):
            loaded.append(environment)
            assert environment == "TEST"
            return {"environment": environment}

        ports = menc.bind_test_enrichment_ports(
            load_config=load, client_cls=Client,
        )
        assert loaded == ["TEST"]
        assert ports.policy is not None
        assert ports.documents is not None

    def test_run_worker_test_flag_refuses_prod(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ROBIE_ENV", "PRODUCTION")
        with pytest.raises(menc.ProductionClientRefused):
            vw.run_worker(
                "4372", day=DAY, mode="dry_run", queue_dir=str(tmp_path),
                csv_bytes=_csv_bytes([_row4372("HO-1")]),
                test_enrichment=True,
            )


class TestMetadataProbe:
    def test_collect_and_classify_keys_does_not_invent(self):
        keys = menc.collect_payload_keys({
            "results": [
                {"id": "1", "documentName": "HO Declaration",
                 "MortgageeName": "A", "LoanNumber": "1", "extraField": "x"},
            ]
        })
        assert "MortgageeName" in keys
        assert "LoanNumber" in keys
        assert "extraField" in keys
        classified = menc.classify_metadata_keys(keys)
        assert "MortgageeName" in classified["allowlisted"]
        assert "LoanNumber" in classified["allowlisted"]
        assert "extraField" in classified["unknown"]

    def test_probe_refuses_production_host_and_wrong_applicant(self, monkeypatch):
        from robie_job_engine.probe_4372_mortgagee_metadata import (
            probe_metadata,
            refuse_production_host,
            require_test_applicant,
        )

        with pytest.raises(menc.ProductionClientRefused, match="hermes-poc-01"):
            refuse_production_host("hermes-poc-01.c.streetsmart-hermes-poc.internal")
        with pytest.raises(menc.ProductionClientRefused, match="220250093"):
            require_test_applicant("221398001")
        monkeypatch.setenv("ROBIE_ENV", "PRODUCTION")
        with pytest.raises(menc.ProductionClientRefused, match="Production"):
            probe_metadata(ports=_ports())

    def test_probe_reads_keys_from_fake_ports(self, monkeypatch):
        from robie_job_engine.probe_4372_mortgagee_metadata import probe_metadata

        monkeypatch.setenv("ROBIE_ENV", "TEST")
        ports = _ports(
            FakePolicy(_policy_payload(
                {"MortgageeName": "Alpha", "LoanNumber": "A-1"},
                property_zip="08527",
            )),
            FakeDocs({"results": [_dec_row("1", "HO Declaration")]}),
        )
        summary = probe_metadata(
            ports=ports,
            policy_number="HO-1",
            hostname="hermes-test-01",
        )
        assert summary["document_search"]["ok"] is True
        assert summary["policy_search"]["ok"] is True
        assert summary["writes"] is False
        assert summary["downloads"] is False
        assert "MortgageeName" in summary["classification"]["allowlisted"]
        assert ports.documents.downloads == []
        assert ports.policy.downloads == []
