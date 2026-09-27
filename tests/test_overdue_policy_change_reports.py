"""Tests for robie_job_engine/overdue_policy_change_reports.py — fake clients, no network, no secrets."""

from datetime import date

import pytest

from robie_job_engine.overdue_policy_change_reports import (
    ACTION,
    CC_CARLO,
    JOB_TYPE,
    RENAG_DAYS,
    NotificationStore,
    OverduePolicyChangeReportVerifier,
    OverduePolicyChangeReportWorker,
    PolicyChangeReportContractError,
    build_csr_report,
    build_roster_maps,
    classify_policy_liveness,
    deconcatenated_variants,
    discussion_context_line,
    parse_4359_csv,
    qualify_rows,
    resolve_nag_targets,
    select_change_discussion,
)
from robie_job_engine.models import JobStatus

TODAY = date(2026, 9, 27)

HEADER = (
    "Account Name,Applicant ID,Policy Number,Line Of Business,Effective Date,"
    "Master Company,Request Status,Created By,Written Premium,Premium - Annualized,"
    "Branch,Department,Service Team,Assigned Producer,CSR,Preferred Language,"
    "Applicant Labels,Policy Labels,Change Request Created Date"
)


def csv_bytes(*rows: str) -> bytes:
    return ("\n".join([HEADER, *rows]) + "\n").encode("utf-8")


def row(account="SAPP Construction Corp", applicant="41055091", policy="S 2391821",
        created="2026-08-17", csr="Eimy Ramos", status="Open",
        carrier="Selective Insurance", lob="Commercial Pkg") -> str:
    return (
        f"{account},{applicant},{policy},{lob},2025-12-10,{carrier},{status},"
        f"\"Quezada, Zeus\",$28936.00,$28936.00,Streetsmart Insurance,"
        f"Commercial Lines,,Sandy Santana,{csr},English,,,{created}"
    )


# -- CSV parsing ---------------------------------------------------------------


def test_parse_valid_csv():
    rows = parse_4359_csv(csv_bytes(row(), row(policy="275768")))
    assert len(rows) == 2
    assert rows[0]["Policy Number"] == "S 2391821"
    assert rows[0]["CSR"] == "Eimy Ramos"


def test_parse_missing_column_raises():
    bad = HEADER.replace(",CSR,", ",")
    with pytest.raises(PolicyChangeReportContractError, match="missing required columns"):
        parse_4359_csv((bad + "\n" + row() + "\n").encode("utf-8"))


def test_parse_ragged_row_raises():
    with pytest.raises(PolicyChangeReportContractError, match="expected 19"):
        parse_4359_csv(csv_bytes(row() + ",EXTRA"))


def test_parse_empty_raises():
    with pytest.raises(PolicyChangeReportContractError):
        parse_4359_csv(b"")


# -- qualification --------------------------------------------------------------


def test_qualify_open_over_14_days():
    rows = parse_4359_csv(csv_bytes(row()))  # 41 days old
    qualified = qualify_rows(rows, TODAY)
    assert len(qualified) == 1
    assert qualified[0]["age_days"] == 41


def test_qualify_skips_fresh_and_closed():
    rows = parse_4359_csv(csv_bytes(row(created="2026-09-20"), row(policy="X2", status="Closed")))
    assert qualify_rows(rows, TODAY) == []


def test_qualify_blank_created_date_raises():
    rows = parse_4359_csv(csv_bytes(row(created="")))
    with pytest.raises(PolicyChangeReportContractError, match="blank Change Request Created Date"):
        qualify_rows(rows, TODAY)


def test_qualify_14_days_is_not_overdue():
    rows = parse_4359_csv(csv_bytes(row(created="2026-09-13")))  # exactly 14 days
    assert qualify_rows(rows, TODAY) == []


# -- liveness gate ---------------------------------------------------------------


def fake_search(mapping):
    def search(number):
        return [dict(r) for r in mapping.get(number, [])]
    return search


def policy_row(number, account="41055091", status="Active", expiration="2026-12-10"):
    return {"policyNumber": number, "accountId": account, "policyStatus": status,
            "expirationDate": expiration, "premium": 28936.0}


def test_liveness_exact_active_account_match():
    search = fake_search({"S 2391821": [policy_row("S 2391821")]})
    verdict = classify_policy_liveness(search, "S  2391821", "41055091", TODAY)
    assert verdict["verdict"] == "LIVE"


def test_liveness_exact_deleted_is_dead():
    search = fake_search({"OLD123": [policy_row("OLD123", status="Deleted")]})
    verdict = classify_policy_liveness(search, "OLD123", "41055091", TODAY)
    assert verdict["verdict"] == "DEAD"


def test_liveness_only_deleted_history_variants_is_dead():
    search = fake_search({
        "CX132093": [
            policy_row("CX132093_15195201", status="Deleted"),
            policy_row("CX132093_14083034", status="Deleted"),
        ]
    })
    verdict = classify_policy_liveness(search, "CX132093", "41055091", TODAY)
    assert verdict["verdict"] == "DEAD"


def test_liveness_expired_is_dead():
    search = fake_search({"OLD123": [policy_row("OLD123", expiration="2020-01-01")]})
    verdict = classify_policy_liveness(search, "OLD123", "41055091", TODAY)
    assert verdict["verdict"] == "DEAD"


def test_liveness_no_results_is_hold():
    verdict = classify_policy_liveness(fake_search({}), "275768", "999", TODAY)
    assert verdict["verdict"] == "HOLD"
    assert "no results" in verdict["reason"]


def test_liveness_account_mismatch_is_hold_not_live():
    search = fake_search({"S 2391821": [policy_row("S 2391821", account="DIFFERENT")]})
    verdict = classify_policy_liveness(search, "S 2391821", "41055091", TODAY)
    assert verdict["verdict"] == "HOLD"


def test_liveness_variant_fallback_finds_live_policy():
    search = fake_search({
        "BDG-312624001": [policy_row("BDG-312624001_73827270", account="82861889", status="Deleted")],
        "BDG-3126240-01": [],
        "BDG-3126240-02": [policy_row("BDG-3126240-02", account="82861889")],
    })
    verdict = classify_policy_liveness(search, "BDG-312624001", "82861889", TODAY)
    assert verdict["verdict"] == "LIVE"
    assert verdict["matched_policy_number"] == "BDG-3126240-02"


def test_deconcatenated_variants():
    assert deconcatenated_variants("BDG-312624001") == ["BDG-3126240-01", "BDG-3126240-02"]
    assert deconcatenated_variants("04283052") == []
    assert deconcatenated_variants("13WECAT1F8T") == []


# -- discussion context ----------------------------------------------------------


def disc(title, count, last):
    return {"title": title, "noteCount": count, "lastModified": last, "mostRecentNoteId": "1"}


def test_select_discussion_prefers_policy_digits():
    discussions = [
        disc("Commercial Auto Policy Change Request - CHANGE ME", 3, "2026-09-20"),
        disc("Re: S 2391821 endorsement", 8, "2026-09-25"),
    ]
    picked = select_change_discussion(discussions, "S 2391821")
    assert picked["noteCount"] == 8


def test_select_discussion_falls_back_to_pcr_title():
    discussions = [disc("Commercial Auto Policy Change Request - CHANGE ME", 3, "2026-09-20")]
    assert select_change_discussion(discussions, "999999")["noteCount"] == 3


def test_select_discussion_none_when_no_match():
    assert select_change_discussion([disc("Renewal docs", 2, "2026-09-01")], "S 2391821") is None


def test_context_line_reports_activity():
    line = discussion_context_line(disc("PCR", 8, "2026-09-25T10:00:00"), TODAY)
    assert "8 notes" in line and "2 days ago" in line


def test_context_line_no_notes_and_no_discussion():
    assert "no notes yet" in discussion_context_line(disc("PCR", 0, ""), TODAY)
    assert "No EZLynx discussion found" in discussion_context_line(None, TODAY)


# -- roster ----------------------------------------------------------------------


def registry():
    return {
        "source_status": "available",
        "employees": {
            "Eimy Ramos": {"role": "Commercial Lines Account Technician",
                           "email": "eimy@streetsmart.insurance",
                           "department": "Commercial Lines", "manager": "", "status": "Active"},
            "Sandy Santana": {"role": "Commercial Lines Department Manager",
                              "email": "sandy@streetsmart.insurance",
                              "department": "Commercial Lines", "manager": "", "status": "Active"},
        },
    }


def test_build_roster_maps():
    maps = build_roster_maps(registry())
    assert maps["directory"]["eimy ramos"] == "eimy@streetsmart.insurance"
    assert maps["managers"]["commercial lines"]["email"] == "sandy@streetsmart.insurance"


def test_build_roster_maps_rejects_unavailable():
    with pytest.raises(PolicyChangeReportContractError, match="unavailable"):
        build_roster_maps({"source_status": "missing", "employees": {}})


def test_build_roster_maps_rejects_ambiguous_name():
    reg = registry()
    reg["employees"]["Eimy  Ramos"] = reg["employees"]["Eimy Ramos"]
    with pytest.raises(PolicyChangeReportContractError, match="ambiguous"):
        build_roster_maps(reg)


def test_resolve_targets_groups_by_csr_with_manager():
    items = [{"CSR": "Eimy Ramos", "Policy Number": "S 2391821"}]
    targets = resolve_nag_targets(items, build_roster_maps(registry()))
    assert targets["Eimy Ramos"]["email"] == "eimy@streetsmart.insurance"
    assert targets["Eimy Ramos"]["manager_email"] == "sandy@streetsmart.insurance"


def test_resolve_targets_fails_closed_on_unknown_csr():
    items = [{"CSR": "Nobody Here", "Policy Number": "S 2391821"}]
    with pytest.raises(PolicyChangeReportContractError, match="could not be resolved"):
        resolve_nag_targets(items, build_roster_maps(registry()))


def test_resolve_targets_fails_closed_without_manager():
    reg = registry()
    del reg["employees"]["Sandy Santana"]
    items = [{"CSR": "Eimy Ramos", "Policy Number": "S 2391821"}]
    with pytest.raises(PolicyChangeReportContractError, match="no department manager"):
        resolve_nag_targets(items, build_roster_maps(reg))


# -- dedupe -----------------------------------------------------------------------


def test_notification_store_renag_after_7_days(tmp_path):
    store = NotificationStore(tmp_path / "sent.json")
    item = {"CSR": "Eimy Ramos", "Policy Number": "S 2391821", "created_date": "2026-08-17"}
    assert store.is_due(item, TODAY)
    store.mark_sent(item, TODAY)
    store.save()
    assert not NotificationStore(tmp_path / "sent.json").is_due(item, TODAY)
    assert NotificationStore(tmp_path / "sent.json").is_due(item, date(2026, 10, 4))


# -- worker ------------------------------------------------------------------------


def make_worker(tmp_path, **overrides):
    sent = []

    def fake_mailer(**kw):
        record = dict(kw)
        record["message_id"] = f"m{len(sent)}"
        sent.append(record)
        return record
    worker = OverduePolicyChangeReportWorker(
        queue_reader=overrides.get("queue_reader", lambda payload: parse_4359_csv(csv_bytes(row()))),
        policy_search=overrides.get("policy_search", fake_search({"S 2391821": [policy_row("S 2391821")]})),
        discussion_lookup=overrides.get(
            "discussion_lookup",
            lambda applicant_id: [disc("Commercial Auto Policy Change Request", 8, "2026-09-25T10:00:00")],
        ),
        directory_loader=overrides.get("directory_loader", lambda manifest: build_roster_maps(registry())),
        mailer=overrides.get("mailer", fake_mailer),
        sent_store=NotificationStore(tmp_path / "sent.json"),
    )
    return worker, sent


def job():
    return {"action_type": ACTION, "payload": {"manifest_path": "/tmp/manifest.json"}}


def test_worker_sends_one_email_per_csr(tmp_path):
    worker, sent = make_worker(tmp_path)
    result = worker.perform(job(), idempotency_key="k1")
    assert result.succeeded
    assert result.destination["csr_count"] == 1
    assert len(sent) == 1
    email = sent[0]
    assert email["to"] == ["eimy@streetsmart.insurance"]
    assert email["cc"] == ["sandy@streetsmart.insurance", CC_CARLO]
    assert "SAPP Construction Corp" in email["text_body"]
    assert "S 2391821" in email["text_body"]
    assert "41 days ago" in email["text_body"]
    assert "8 notes" in email["text_body"]


def test_worker_rerun_does_not_renag(tmp_path):
    worker, sent = make_worker(tmp_path)
    worker.perform(job(), idempotency_key="k1")
    result = worker.perform(job(), idempotency_key="k2")
    assert result.succeeded
    assert result.destination["due_for_nag"] == 0
    assert len(sent) == 1


def test_worker_excludes_dead_and_holds_noresult(tmp_path):
    rows = parse_4359_csv(csv_bytes(
        row(),
        row(account="Dead Co", policy="OLD1", applicant="111", csr="Eimy Ramos", created="2026-08-01"),
        row(account="Ghost Co", policy="NOPE", applicant="222", csr="Eimy Ramos", created="2026-08-01"),
    ))
    search = fake_search({
        "S 2391821": [policy_row("S 2391821")],
        "OLD1": [policy_row("OLD1", account="111", status="Deleted")],
    })
    worker, sent = make_worker(tmp_path, queue_reader=lambda payload: rows, policy_search=search)
    result = worker.perform(job(), idempotency_key="k1")
    assert result.succeeded
    assert result.destination["overdue_live"] == 1
    assert result.destination["overdue_dead_excluded"] == 1
    assert result.destination["on_hold"] == 1
    assert len(sent) == 1  # only the live item is emailed


def test_worker_marks_unverified_on_discussion_failure(tmp_path):
    def boom(applicant_id):
        raise RuntimeError("socket exploded")

    worker, sent = make_worker(tmp_path, discussion_lookup=boom)
    result = worker.perform(job(), idempotency_key="k1")
    assert result.succeeded  # run continues...
    assert "UNVERIFIED" in sent[0]["text_body"]  # ...but the item is flagged


def test_worker_fails_closed_when_discussion_api_down(tmp_path):
    # default_discussion_lookup converts a DiscussionApi transport/auth
    # failure into the contract error -> the whole run fails closed.
    def api_down(applicant_id):
        raise PolicyChangeReportContractError("DiscussionApi lookup failed: transport failed")

    worker, sent = make_worker(tmp_path, discussion_lookup=api_down)
    result = worker.perform(job(), idempotency_key="k1")
    assert not result.succeeded
    assert result.hold_status == JobStatus.NEEDS_CLARIFICATION
    assert sent == []


def test_worker_fails_closed_on_roster_problem(tmp_path):
    def bad_loader(manifest):
        raise PolicyChangeReportContractError("roster unavailable")

    worker, sent = make_worker(tmp_path, directory_loader=bad_loader)
    result = worker.perform(job(), idempotency_key="k1")
    assert not result.succeeded
    assert sent == []


def test_worker_rejects_wrong_action(tmp_path):
    worker, sent = make_worker(tmp_path)
    result = worker.perform({"action_type": "something.else", "payload": {}}, idempotency_key="k1")
    assert not result.succeeded
    assert result.hold_status == JobStatus.NEEDS_CLARIFICATION
    assert sent == []


def test_worker_requires_manifest(tmp_path):
    worker, sent = make_worker(tmp_path)
    result = worker.perform({"action_type": ACTION, "payload": {}}, idempotency_key="k1")
    assert not result.succeeded
    assert sent == []


def test_email_body_is_plain_and_concise():
    items = [{
        "Account Name": "SAPP Construction Corp", "Policy Number": "S 2391821",
        "Master Company": "Selective Insurance", "Line Of Business": "Commercial Pkg",
        "created_date": "2026-08-17", "age_days": 41,
        "discussion": disc("Commercial Auto Policy Change Request", 8, "2026-09-25T10:00:00"),
    }]
    body = build_csr_report("Eimy Ramos", items, "Sandy Santana", TODAY)
    assert "Hi Eimy," in body
    assert "status update" in body
    assert "CC'ing Sandy Santana" in body
    assert "S 2391821" in body


# -- verifier -----------------------------------------------------------------------


def test_verifier_accepts_matching_receipts():
    verifier = OverduePolicyChangeReportVerifier(
        delivery_readback=lambda receipts: (True, [{"exists_in_sent_mailbox": True} for _ in receipts])
    )
    action = {"destination": {"delivery_receipts": [{"message_id": "m0"}], "csr_count": 1}}
    result = verifier.verify({}, action)
    assert result.verified


def test_verifier_rejects_count_mismatch():
    verifier = OverduePolicyChangeReportVerifier(
        delivery_readback=lambda receipts: (True, [{"exists_in_sent_mailbox": True}])
    )
    action = {"destination": {"delivery_receipts": [{"message_id": "m0"}], "csr_count": 2}}
    result = verifier.verify({}, action)
    assert not result.verified


def test_verifier_zero_case_with_no_receipts():
    verifier = OverduePolicyChangeReportVerifier()
    result = verifier.verify({}, {"destination": {"delivery_receipts": [], "csr_count": 0}})
    assert result.verified
