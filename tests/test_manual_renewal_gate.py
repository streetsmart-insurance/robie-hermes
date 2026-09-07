"""Paulette Fagone HO (2026-09-07): exact Manual {LOB} resolver + COMPLETE gate."""

from decimal import Decimal

from src.ezlynx.manual_renewal_gate import (
    STATUS_BLOCKED,
    STATUS_COMPLETE,
    STATUS_PARTIAL,
    DoneChecklistEvidence,
    activity_card_matches_exact_title,
    evaluate_done_checklist,
    find_exact_titled_discussion,
    is_automation_discussion_title,
    manual_lob_renewal_title,
    renewal_update_lob_title,
    resolve_manual_lob_discussion,
    resolve_renewal_update_discussion,
    verify_discussion_id_and_title,
)
from src.ezlynx.policy_renewer import (
    ManualPolicyRenewer,
    RenewalJobResult,
    RenewalJobSpec,
)


PAULETTE_DISCUSSIONS = [
    {
        "discussionId": 1123385828,
        "title": "Email Automation",
        "noteCount": 40,
        "discussionNote": {"policyNumber": "HO-1", "noteId": 1123385828},
    },
    {
        "discussionId": 88,
        "title": "Automation Center",
        "noteCount": 12,
    },
    {
        "id": 715880503,
        "title": "Untitled",
        "noteCount": 1,
    },
]


def _complete_evidence(**overrides) -> DoneChecklistEvidence:
    data = dict(
        firmed_pdf_uploaded=True,
        firmed_pdf_label="Renewal Offer",
        pdf_premium=Decimal("1842.00"),
        keyed_premium=Decimal("1842.00"),
        pending_rwl_count=1,
        bound=False,
        manual_lob_note_posted=True,
        manual_lob_discussion_id="9001",
        manual_lob_title="Manual Homeowners Renewal",
        manual_lob_title_expected="Manual Homeowners Renewal",
        renewal_update_note_posted=True,
        renewal_update_discussion_id="9002",
        renewal_update_title="Renewal Update Homeowners",
        renewal_update_title_expected="Renewal Update Homeowners",
    )
    data.update(overrides)
    return DoneChecklistEvidence(**data)


def test_resolver_rejects_email_automation_and_automation_center():
    assert is_automation_discussion_title("Email Automation")
    assert is_automation_discussion_title("Automation Center")
    assert is_automation_discussion_title("Email sent by Automation Center")
    assert not is_automation_discussion_title("Manual Homeowners Renewal")

    assert find_exact_titled_discussion(PAULETTE_DISCUSSIONS, "Email Automation") is None
    assert find_exact_titled_discussion(PAULETTE_DISCUSSIONS, "Automation Center") is None
    assert activity_card_matches_exact_title("Email Automation", "Manual Homeowners Renewal") is False
    assert activity_card_matches_exact_title("Email Automation", "Email Automation") is False


def test_resolver_creates_missing_manual_homeowners_renewal():
    result = resolve_manual_lob_discussion(
        PAULETTE_DISCUSSIONS, "Homeowners", create_if_missing=True
    )
    assert result.action == "create"
    assert result.created is True
    assert result.title == "Manual Homeowners Renewal"
    assert result.discussion_id is None
    assert result.title != "Email Automation"


def test_resolver_matches_existing_exact_manual_lob_title():
    discussions = PAULETTE_DISCUSSIONS + [
        {"discussionId": 1961001, "title": "Manual Homeowners Renewal", "noteCount": 2}
    ]
    result = resolve_manual_lob_discussion(discussions, "HO", create_if_missing=True)
    assert result.action == "matched"
    assert result.created is False
    assert result.title == "Manual Homeowners Renewal"
    assert result.discussion_id == "1961001"


def test_renewal_update_title_and_create_if_missing():
    assert renewal_update_lob_title("Homeowners") == "Renewal Update Homeowners"
    assert manual_lob_renewal_title("ho") == "Manual Homeowners Renewal"
    created = resolve_renewal_update_discussion(
        PAULETTE_DISCUSSIONS, "Homeowners", create_if_missing=True
    )
    assert created.title == "Renewal Update Homeowners"
    assert created.created is True


def test_verify_discussion_id_and_exact_title():
    discussions = [
        {"discussionId": 1961001, "title": "Manual Homeowners Renewal"},
        {"discussionId": 1123385828, "title": "Email Automation"},
    ]
    ok = verify_discussion_id_and_title(
        discussions, "Manual Homeowners Renewal", expected_discussion_id="1961001"
    )
    assert ok["verified"] is True
    assert ok["discussion_id"] == "1961001"

    bad = verify_discussion_id_and_title(discussions, "Email Automation")
    assert bad["verified"] is False


def test_done_checklist_complete_when_all_items_present():
    result = evaluate_done_checklist(_complete_evidence())
    assert result.status == STATUS_COMPLETE
    assert result.complete is True
    assert result.missing == []
    assert result.status != "SUCCESS"


def test_done_checklist_fails_when_manual_lob_note_missing():
    result = evaluate_done_checklist(_complete_evidence(manual_lob_note_posted=False))
    assert result.status == STATUS_PARTIAL
    assert result.complete is False
    assert "manual_lob_note" in result.missing
    assert result.status not in {"COMPLETE", "SUCCESS", "success"}


def test_done_checklist_fails_when_docs_only():
    result = evaluate_done_checklist(
        _complete_evidence(
            firmed_pdf_uploaded=True,
            firmed_pdf_label="Renewal Offer",
            manual_lob_note_posted=False,
            manual_lob_discussion_id=None,
            manual_lob_title=None,
            renewal_update_note_posted=False,
            renewal_update_discussion_id=None,
            renewal_update_title=None,
        )
    )
    assert result.status == STATUS_PARTIAL
    assert result.complete is False
    assert "manual_lob_note" in result.missing
    assert "renewal_update_note" in result.missing
    assert result.status != STATUS_COMPLETE


def test_done_checklist_blocks_email_automation_misfire():
    result = evaluate_done_checklist(
        _complete_evidence(
            manual_lob_title="Email Automation",
            manual_lob_discussion_id="1123385828",
        )
    )
    assert result.status == STATUS_BLOCKED
    assert result.complete is False
    assert "automation_discussion" in result.missing


def test_done_checklist_partial_on_premium_mismatch_or_wrong_rwl_count():
    mismatch = evaluate_done_checklist(_complete_evidence(pdf_premium=Decimal("10.00")))
    assert mismatch.status == STATUS_PARTIAL
    assert "premium_match" in mismatch.missing

    two_rwl = evaluate_done_checklist(_complete_evidence(pending_rwl_count=2))
    assert two_rwl.status == STATUS_PARTIAL
    assert "exactly_one_pending_rwl" in two_rwl.missing

    bound = evaluate_done_checklist(_complete_evidence(bound=True))
    assert bound.status == STATUS_BLOCKED
    assert "bind_must_be_false" in bound.missing


def test_renewer_apply_done_checklist_docs_only_is_partial_not_success():
    spec = RenewalJobSpec(
        applicant_id="196126698",
        line_of_business="Homeowners",
        premium=Decimal("1842.00"),
        pdf_extracted_premium=Decimal("1842.00"),
        dry_run=False,
        env="test",
    )
    result = RenewalJobResult(
        status="keyed",
        applicant_id="196126698",
        document_uploaded=True,
        document_label="Renewal Offer",
        pending_shells=[{"transaction_type": "RWL", "status": "Pending"}],
        bound=False,
        note_posted=False,
    )
    ManualPolicyRenewer(api_client=None)._apply_done_checklist(spec, result)
    assert result.status == "partial"
    assert result.status not in {"complete", "success", "SUCCESS", "COMPLETE"}
    assert result.done_checklist["status"] == STATUS_PARTIAL
    assert "manual_lob_note" in result.done_checklist["missing"]


def test_renewer_apply_done_checklist_missing_manual_note_not_complete():
    spec = RenewalJobSpec(
        applicant_id="196126698",
        line_of_business="Homeowners",
        premium=Decimal("1842.00"),
        pdf_extracted_premium=Decimal("1842.00"),
        dry_run=False,
        env="test",
    )
    result = RenewalJobResult(
        status="keyed",
        applicant_id="196126698",
        document_uploaded=True,
        document_label="Renewal Offer",
        pending_shells=[{"transaction_type": "RWL", "status": "Pending"}],
        bound=False,
        note_posted=True,
        renewal_update_note_posted=True,
        renewal_update_discussion_id="9002",
        renewal_update_title="Renewal Update Homeowners",
        manual_lob_note_posted=False,
        manual_lob_title="Manual Homeowners Renewal",
    )
    ManualPolicyRenewer(api_client=None)._apply_done_checklist(spec, result)
    assert result.status == "partial"
    assert result.done_checklist["complete"] is False
    assert "manual_lob_note" in result.done_checklist["missing"]


def test_renewer_apply_done_checklist_does_not_override_login_blocked():
    spec = RenewalJobSpec(
        applicant_id="196126698",
        line_of_business="Homeowners",
        premium=Decimal("1"),
        dry_run=False,
        env="test",
    )
    result = RenewalJobResult(
        status="blocked",
        applicant_id="196126698",
        proof_source="live_cdp_preflight",
        preflight={"reason": "login_url", "ok": False},
        bound=False,
    )
    ManualPolicyRenewer(api_client=None)._apply_done_checklist(spec, result)
    assert result.status == "blocked"
    assert result.done_checklist is None
