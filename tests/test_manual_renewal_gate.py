"""Manual renewal note targeting + COMPLETE gate (LOB-agnostic)."""

from decimal import Decimal

import pytest

from src.extractor.quote_parser import extract_policy_total_premium
from src.ezlynx.manual_renewal_gate import (
    DOC_KIND_APPLICATION,
    DOC_KIND_BOUND_QUOTE,
    DOC_KIND_RENEWAL_OFFER,
    RENEWAL_OFFER_FOLDER,
    STATUS_BLOCKED,
    STATUS_COMPLETE,
    STATUS_PARTIAL,
    DoneChecklistEvidence,
    activity_card_matches_exact_title,
    classify_renewal_document,
    evaluate_done_checklist,
    find_exact_titled_discussion,
    is_application_or_bound_quote_document,
    is_automation_discussion_title,
    is_true_renewal_offer_document,
    manual_lob_renewal_title,
    renewal_update_lob_title,
    resolve_existing_lob_renewal_discussion,
    resolve_manual_lob_discussion,
    resolve_manual_renewal_note_targets,
    resolve_renewal_offer_folder,
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
        firmed_pdf_folder="Renewal Offer",
        firmed_pdf_kind="renewal_offer",
        firmed_pdf_name="HONJ2025100027 Renewal Offer.pdf",
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


BENLI_HYUNDAI_DEC_TEXT = (
    "HYUNDAI HOMEOWNERS DECLARATIONS\n"
    "Named Insured: Hanim Benli\n"
    "Policy Number: HONJ2025100027-26\n"
    "COVERAGE SECTION I\n"
    "Coverage A - Dwelling    Limit $250,000    Annual Premium $1,116.00\n"
    "Policy Total Premium                       $1,348.00\n"
    "Total Annual Premium                       $1,348.00\n"
)


def test_done_checklist_partial_on_premium_mismatch_or_wrong_rwl_count():
    mismatch = evaluate_done_checklist(_complete_evidence(pdf_premium=Decimal("10.00")))
    assert mismatch.status == STATUS_PARTIAL
    assert "premium_match" in mismatch.missing
    assert "Policy Total" in " ".join(mismatch.reasons)

    two_rwl = evaluate_done_checklist(_complete_evidence(pending_rwl_count=2))
    assert two_rwl.status == STATUS_PARTIAL
    assert "exactly_one_pending_rwl" in two_rwl.missing

    bound = evaluate_done_checklist(_complete_evidence(bound=True))
    assert bound.status == STATUS_BLOCKED
    assert "bind_must_be_false" in bound.missing


def test_done_checklist_rejects_coverage_a_when_pdf_policy_total_differs():
    """Benli: keyed/stored $1,116 (Coverage A) must not COMPLETE vs Policy Total $1,348."""
    assert extract_policy_total_premium(BENLI_HYUNDAI_DEC_TEXT) == Decimal("1348.00")
    result = evaluate_done_checklist(
        _complete_evidence(
            pdf_premium=Decimal("1116.00"),
            keyed_premium=Decimal("1116.00"),
            firmed_pdf_text=BENLI_HYUNDAI_DEC_TEXT,
        )
    )
    assert result.status == STATUS_PARTIAL
    assert result.complete is False
    assert "premium_match" in result.missing
    reasons = " ".join(result.reasons)
    assert "1,116.00" in reasons
    assert "1,348.00" in reasons
    assert "Policy Total" in reasons
    assert result.status != STATUS_COMPLETE


def test_done_checklist_complete_when_keyed_matches_policy_total():
    result = evaluate_done_checklist(
        _complete_evidence(
            pdf_premium=Decimal("1116.00"),
            keyed_premium=Decimal("1348.00"),
            firmed_pdf_text=BENLI_HYUNDAI_DEC_TEXT,
        )
    )
    assert result.status == STATUS_COMPLETE
    assert result.complete is True


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


EXISTING_LOB_RENEWAL_CASES = [
    (
        "Homeowners",
        "Homeowners Renewal",
        "Homeowners Renewal Offer",
        "Manual Homeowners Renewal",
        "Renewal Update Homeowners",
    ),
    (
        "Commercial Auto",
        "Commercial Auto Renewal",
        "Commercial Auto Renewal Offer",
        "Manual Commercial Auto Renewal",
        "Renewal Update Commercial Auto",
    ),
    (
        "Workers Compensation",
        "Workers Compensation Renewal",
        "Workers Comp Renewal Offer",
        "Manual Workers Comp Renewal",
        "Renewal Update Workers Comp",
    ),
    (
        "BOP",
        "BOP Renewal",
        "Business Owners Renewal Offer",
        "Manual Business Owners Renewal",
        "Renewal Update Business Owners",
    ),
]


@pytest.mark.parametrize(
    "lob,existing_title,variant_title,manual_title,update_title",
    EXISTING_LOB_RENEWAL_CASES,
)
def test_existing_lob_renewal_preferred_over_manual_and_update(
    lob, existing_title, variant_title, manual_title, update_title
):
    discussions = PAULETTE_DISCUSSIONS + [
        {"discussionId": 44, "title": existing_title, "noteCount": 4},
        {"discussionId": 45, "title": manual_title, "noteCount": 1},
        {"discussionId": 46, "title": update_title, "noteCount": 1},
    ]
    existing = resolve_existing_lob_renewal_discussion(discussions, lob)
    assert existing is not None
    assert existing.title == existing_title
    assert existing.kind == "existing_lob_renewal"
    assert existing.created is False

    targets = resolve_manual_renewal_note_targets(discussions, lob, create_if_missing=True)
    assert targets.used_existing_lob_renewal is True
    assert [t.title for t in targets.targets] == [existing_title]
    assert manual_title not in [t.title for t in targets.targets]
    assert update_title not in [t.title for t in targets.targets]
    assert variant_title  # close-variant name is part of the candidate set


@pytest.mark.parametrize(
    "lob,existing_title,variant_title,manual_title,update_title",
    EXISTING_LOB_RENEWAL_CASES,
)
def test_existing_lob_renewal_offer_variant_preferred_when_exact_missing(
    lob, existing_title, variant_title, manual_title, update_title
):
    discussions = PAULETTE_DISCUSSIONS + [
        {"discussionId": 77, "title": variant_title, "noteCount": 2},
        {"discussionId": 78, "title": manual_title},
        {"discussionId": 79, "title": update_title},
    ]
    targets = resolve_manual_renewal_note_targets(discussions, lob)
    assert targets.used_existing_lob_renewal is True
    assert targets.targets[0].title == variant_title
    assert targets.targets[0].title != manual_title


@pytest.mark.parametrize(
    "lob,existing_title,variant_title,manual_title,update_title",
    EXISTING_LOB_RENEWAL_CASES,
)
def test_fallback_creates_manual_and_update_when_no_existing_lob_renewal(
    lob, existing_title, variant_title, manual_title, update_title
):
    targets = resolve_manual_renewal_note_targets(
        PAULETTE_DISCUSSIONS, lob, create_if_missing=True
    )
    assert targets.used_existing_lob_renewal is False
    titles = [t.title for t in targets.targets]
    assert titles == [manual_title, update_title]
    assert existing_title not in titles
    assert all(t.created for t in targets.targets)


def test_done_checklist_complete_on_existing_lob_renewal_without_manual_cards():
    result = evaluate_done_checklist(
        _complete_evidence(
            used_existing_lob_renewal=True,
            existing_lob_renewal_note_posted=True,
            existing_lob_renewal_discussion_id="44",
            existing_lob_renewal_title="Homeowners Renewal",
            existing_lob_renewal_title_expected="Homeowners Renewal",
            manual_lob_note_posted=False,
            manual_lob_discussion_id=None,
            manual_lob_title=None,
            renewal_update_note_posted=False,
            renewal_update_discussion_id=None,
            renewal_update_title=None,
        )
    )
    assert result.status == STATUS_COMPLETE
    assert result.complete is True


def test_done_checklist_requires_renewal_offer_folder_not_policy_number():
    result = evaluate_done_checklist(_complete_evidence(firmed_pdf_folder="HONJ2025100027"))
    assert result.status == STATUS_PARTIAL
    assert "renewal_offer_folder" in result.missing


def test_resolve_renewal_offer_folder_creates_when_only_policy_number_folder():
    created = resolve_renewal_offer_folder(["HONJ2025100027", "Applications"])
    assert created.action == "create"
    assert created.created is True
    assert created.folder == RENEWAL_OFFER_FOLDER

    matched = resolve_renewal_offer_folder(["HONJ2025100027", "Renewal Offer"])
    assert matched.action == "matched"
    assert matched.created is False
    assert matched.folder == "Renewal Offer"


@pytest.mark.parametrize(
    "name,text,expected",
    [
        (
            "QHONJ2026080215 Bound Quote Application.pdf",
            "Bound Quote Application",
            DOC_KIND_BOUND_QUOTE,
        ),
        (
            "HONJ2025100027 Renewal Offer.pdf",
            "Renewal Application QHONJ2026080215 Bound Quote print",
            DOC_KIND_BOUND_QUOTE,
        ),
        (
            "Insured Renewal Application.pdf",
            "ACORD Application for Insurance",
            DOC_KIND_APPLICATION,
        ),
        (
            "HONJ2025100027 Renewal Offer.pdf",
            "Homeowners Renewal Offer / Declaration",
            DOC_KIND_RENEWAL_OFFER,
        ),
        (
            "Fagone - J&J Home Quote Proposal (Firmed).pdf",
            None,
            DOC_KIND_RENEWAL_OFFER,
        ),
    ],
)
def test_classify_rejects_application_and_bound_quote_as_renewal_offer(name, text, expected):
    kind = classify_renewal_document(name=name, text=text)
    assert kind == expected
    if expected == DOC_KIND_RENEWAL_OFFER:
        assert is_true_renewal_offer_document(name=name, text=text) is True
        assert is_application_or_bound_quote_document(name=name, text=text) is False
    else:
        assert is_true_renewal_offer_document(name=name, text=text) is False
        assert is_application_or_bound_quote_document(name=name, text=text) is True


def test_done_checklist_rejects_bound_quote_labeled_renewal_offer():
    """Benli: label said Renewal Offer but print was Bound Quote QHONJ2026080215."""
    result = evaluate_done_checklist(
        _complete_evidence(
            firmed_pdf_label="Renewal Offer",
            firmed_pdf_folder="Renewal Offer",
            firmed_pdf_kind=None,
            firmed_pdf_name="HONJ2025100027 Renewal Offer.pdf",
            firmed_pdf_text="Bound Quote Application QHONJ2026080215",
        )
    )
    assert result.status == STATUS_PARTIAL
    assert result.complete is False
    assert "true_renewal_offer_pdf" in result.missing
    assert result.status != STATUS_COMPLETE


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
