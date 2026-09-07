"""Coverage for hardened manual renewal shell keying (dedupe, button, titles)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.ezlynx.policy_renewer import (
    DEFAULT_PRODUCER_CSR,
    FORBIDDEN_RENEW_BUTTON_TEXTS,
    LIVE_ALLOW_TOKEN,
    MIN_AUTHENTIC_PDF_BYTES,
    RENEW_POLICY_BTN_SELECTOR,
    LiveKeyingBlocked,
    ManualPolicyRenewer,
    PendingShell,
    RenewalGuardError,
    RenewalJobSpec,
    assert_live_keying_allowed,
    assert_producer_is_carlo,
    build_parser,
    build_shell_note,
    choose_renew_submit_button,
    classic_api_cannot_prove_absence,
    extract_pending_rwl_shells,
    find_duplicate_pending_shell,
    is_forbidden_renew_button,
    is_generic_lob_title,
    is_preferred_renew_button,
    is_stub_document,
    is_untitled_discussion,
    missing_required_fields,
    premiums_match,
    reject_quote_id_add_policy,
    resolve_exact_discussion_title,
    should_skip_docs_and_notes,
    spec_from_args,
    terms_match,
    verification_sources_are_sufficient,
    write_proof_json,
)


YES_WE_DO_TITLED = "Renewal Manual Workers comp | PWC1239278 Associated Specialty Insurance"
YES_WE_DO_GENERIC = "Workers Compensation Renewal"

YES_WE_DO_DISCUSSIONS = [
    {
        "discussionId": 1,
        "title": YES_WE_DO_TITLED,
        "noteCount": 6,
        "lastModifiedByName": "Eimy Ramos",
    },
    {
        "discussionId": 2,
        "title": YES_WE_DO_GENERIC,
        "noteCount": 99,
        "lastModifiedByName": "Robie AI",
    },
    {
        "discussionId": 3,
        "title": "General Liability Renewal",
        "noteCount": 4,
    },
    {
        "discussionId": 4,
        "title": "",
        "noteCount": 1,
    },
]


def test_classic_api_cannot_prove_absence():
    assert classic_api_cannot_prove_absence({"policies": []}) is True
    assert classic_api_cannot_prove_absence(None) is True


def test_verification_sources_classic_only_is_insufficient():
    assert verification_sources_are_sufficient(["classic_api"]) is False
    assert verification_sources_are_sufficient(["get_applicant_policies"]) is False
    assert verification_sources_are_sufficient(["policy_summary_history"]) is True
    assert verification_sources_are_sufficient(["in_page_policy_api"]) is True
    assert verification_sources_are_sufficient(["policy_api_cards", "classic_api"]) is True


def test_extract_and_dedupe_pending_rwl_same_term_premium():
    rows = [
        {
            "source": "policy_summary_history",
            "status": "Pending",
            "transactionType": "RWL",
            "text": "RENEWAL (10/15/2026 - 10/15/2027) Pending $5,182.00",
            "dates": ["10/15/2026", "10/15/2027"],
            "money": ["$5,182.00"],
        },
        {
            "source": "policy_summary_history",
            "status": "Active",
            "transactionType": "NBS",
            "text": "NEW BUSINESS (10/15/2025 - 10/15/2026) $4,900.00",
        },
    ]
    shells = extract_pending_rwl_shells(rows)
    assert len(shells) == 1
    assert shells[0].source == "policy_summary_history"
    assert terms_match(shells[0].effective_date, shells[0].expiration_date, "2026-10-15", "2027-10-15")
    assert premiums_match(shells[0].premium, "5182.00")

    dup = find_duplicate_pending_shell(
        shells,
        effective_date="10/15/2026",
        expiration_date="10/15/2027",
        premium=Decimal("5182.00"),
    )
    assert dup is not None

    other_term = find_duplicate_pending_shell(
        shells,
        effective_date="2026-11-01",
        expiration_date="2027-11-01",
        premium=Decimal("5182.00"),
    )
    assert other_term is None

    other_prem = find_duplicate_pending_shell(
        shells,
        effective_date="2026-10-15",
        expiration_date="2027-10-15",
        premium=Decimal("100.00"),
    )
    assert other_prem is None


def test_maier_style_five_pending_shells_still_dedupes():
    shells = [
        PendingShell(
            transaction_type="RWL",
            status="Pending",
            effective_date="2026-10-01",
            expiration_date="2027-10-01",
            premium=Decimal("24000.00"),
            source="policy_summary_history",
        )
        for _ in range(5)
    ]
    dup = find_duplicate_pending_shell(
        shells,
        effective_date="2026-10-01",
        expiration_date="2027-10-01",
        premium="24000",
    )
    assert dup is not None


def test_renew_policy_btn_preferred_never_renew_and_edit():
    candidates = [
        {"id": "RenewAndEditBtn", "text": "Renew & Edit Policy"},
        {"id": "RenewPolicyBtn", "text": "Renew Policy"},
        {"id": "BindBtn", "text": "Bind"},
    ]
    chosen = choose_renew_submit_button(candidates)
    assert chosen is not None
    assert chosen["id"] == "RenewPolicyBtn"
    assert chosen["text"] == "Renew Policy"
    assert is_preferred_renew_button("Renew Policy", "RenewPolicyBtn")
    assert is_forbidden_renew_button("Renew & Edit Policy", "RenewAndEditBtn")
    assert is_forbidden_renew_button("Bind", "BindBtn")
    assert "Renew & Edit Policy" in FORBIDDEN_RENEW_BUTTON_TEXTS
    assert RENEW_POLICY_BTN_SELECTOR == "#RenewPolicyBtn"
    assert choose_renew_submit_button([{"id": "x", "text": "Renew & Edit Policy"}]) is None


def test_writing_company_required_before_submit():
    assert "writing_company" in missing_required_fields(
        premium="1200",
        writing_company="",
        effective_date="2026-10-15",
        expiration_date="2027-10-15",
    )
    assert missing_required_fields(
        premium="1200",
        writing_company="Associated Specialty",
        effective_date="2026-10-15",
        expiration_date="2027-10-15",
    ) == []


def test_yes_we_do_discussion_uses_original_titled_card_not_generic_lob():
    title = resolve_exact_discussion_title(
        YES_WE_DO_DISCUSSIONS,
        requested_title=YES_WE_DO_GENERIC,
        policy_numbers=["PWC1239278"],
        line_of_business="Workers comp",
    )
    assert title == YES_WE_DO_TITLED

    exact = resolve_exact_discussion_title(
        YES_WE_DO_DISCUSSIONS,
        requested_title=YES_WE_DO_TITLED,
        policy_numbers=["PWC1239278"],
        line_of_business="Workers Compensation",
    )
    assert exact == YES_WE_DO_TITLED


def test_discussion_refuses_untitled_and_wrong_lob():
    with pytest.raises(RenewalGuardError, match="wrong LOB|does not match LOB|No exact"):
        resolve_exact_discussion_title(
            YES_WE_DO_DISCUSSIONS,
            requested_title="General Liability Renewal",
            policy_numbers=["PWC1239278"],
            line_of_business="Workers comp",
        )
    with pytest.raises(RenewalGuardError, match="No exact original titled"):
        resolve_exact_discussion_title(
            [{"title": ""}, {"title": "Untitled"}],
            requested_title=None,
            policy_numbers=["PWC1239278"],
            line_of_business="Workers comp",
        )
    assert is_untitled_discussion("")
    assert is_generic_lob_title(YES_WE_DO_GENERIC)
    assert not is_generic_lob_title(YES_WE_DO_TITLED)


def test_skip_docs_and_notes_when_already_in():
    assert should_skip_docs_and_notes(True) is True
    assert should_skip_docs_and_notes(False) is False


def test_stub_pdf_under_10kb(tmp_path: Path):
    stub = tmp_path / "stub.pdf"
    stub.write_bytes(b"%PDF-1.4 stub" + b"\x00" * 100)
    assert stub.stat().st_size < MIN_AUTHENTIC_PDF_BYTES
    assert is_stub_document(stub) is True

    authentic = tmp_path / "real.pdf"
    authentic.write_bytes(b"%PDF-1.4 " + b"A" * MIN_AUTHENTIC_PDF_BYTES)
    assert is_stub_document(authentic) is False
    assert is_stub_document(tmp_path / "missing.pdf") is True


def test_producer_carlo_never_robie():
    assert assert_producer_is_carlo("Carlo Ferrara") == DEFAULT_PRODUCER_CSR
    with pytest.raises(RenewalGuardError, match="never Robie"):
        assert_producer_is_carlo("Robie AI")
    with pytest.raises(RenewalGuardError, match="Carlo"):
        assert_producer_is_carlo("Someone Else")


def test_allow_live_carlo_and_production_allowlist():
    dry = RenewalJobSpec(applicant_id="999", dry_run=True, env="prod")
    assert_live_keying_allowed(dry)

    test_env = RenewalJobSpec(
        applicant_id="999",
        dry_run=False,
        env="test",
        producer=DEFAULT_PRODUCER_CSR,
    )
    assert_live_keying_allowed(test_env)

    with pytest.raises(LiveKeyingBlocked, match="allow-live Carlo"):
        assert_live_keying_allowed(
            RenewalJobSpec(
                applicant_id="25156187",
                dry_run=False,
                env="prod",
                allow_live=None,
                producer=DEFAULT_PRODUCER_CSR,
            )
        )

    with pytest.raises(LiveKeyingBlocked, match="allow-list"):
        assert_live_keying_allowed(
            RenewalJobSpec(
                applicant_id="000000",
                dry_run=False,
                env="prod",
                allow_live=LIVE_ALLOW_TOKEN,
                producer=DEFAULT_PRODUCER_CSR,
            )
        )

    assert_live_keying_allowed(
        RenewalJobSpec(
            applicant_id="25156187",
            dry_run=False,
            env="prod",
            allow_live="Carlo",
            producer=DEFAULT_PRODUCER_CSR,
        )
    )


def test_never_add_policy_from_quote_id():
    with pytest.raises(RenewalGuardError, match="Quote ID"):
        reject_quote_id_add_policy("Q-123")
    reject_quote_id_add_policy(None)


def test_shell_note_has_policy_header_and_robie_signature():
    spec = RenewalJobSpec(
        applicant_id="21588091",
        policy_number="PWC1239278",
        line_of_business="Workers comp",
        carrier_name="Associated Specialty Insurance",
        premium=Decimal("1200"),
        effective_date="2026-10-15",
        expiration_date="2027-10-15",
        writing_company="Associated Specialty",
    )
    note = build_shell_note(spec)
    assert note.startswith("Policy: #PWC1239278 (Workers comp - Associated Specialty Insurance)")
    assert note.rstrip().endswith("Robie was here")
    assert "Carlo Ferrara" in note
    assert "No bind" in note or "no bind" in note.lower()


def test_cli_parser_dry_run_and_discussion_title():
    parser = build_parser()
    args = parser.parse_args(
        [
            "--applicant-id",
            "21588091",
            "--policy-id",
            "555",
            "--policy-number",
            "PWC1239278",
            "--lob",
            "Workers comp",
            "--discussion-title",
            YES_WE_DO_TITLED,
            "--writing-company",
            "Associated Specialty",
            "--premium",
            "1200",
            "--effective-date",
            "2026-10-15",
            "--expiration-date",
            "2027-10-15",
            "--dry-run",
            "--env",
            "test",
            "--allow-live",
            "Carlo",
        ]
    )
    spec = spec_from_args(args)
    assert spec.dry_run is True
    assert spec.discussion_title == YES_WE_DO_TITLED
    assert spec.allow_live == "Carlo"
    assert spec.writing_company == "Associated Specialty"
    assert spec.producer == DEFAULT_PRODUCER_CSR


@pytest.mark.asyncio
async def test_connected_job_dry_run_already_in_skips_docs_and_notes(tmp_path: Path):
    proof = tmp_path / "proof.json"
    spec = RenewalJobSpec(
        applicant_id="25156187",
        policy_number="IM-1",
        line_of_business="Inland Marine",
        premium=Decimal("100"),
        effective_date="2026-10-01",
        expiration_date="2027-10-01",
        writing_company="AmWINS",
        discussion_title="Renewal Manual Inland Marine | IM-1 AmWINS",
        dry_run=True,
        already_in=True,
        env="test",
        proof_json=proof,
        upload_path=tmp_path / "missing.pdf",
    )
    result = await ManualPolicyRenewer(api_client=MagicMock()).run_connected_job(spec)
    assert result.status == "already_in"
    assert result.already_in is True
    assert result.note_posted is False
    assert result.document_uploaded is False
    assert result.note_skipped_reason == "already_in"
    assert result.document_skipped_reason == "already_in"
    assert result.bound is False
    assert result.renew_button == RENEW_POLICY_BTN_SELECTOR
    assert result.producer == DEFAULT_PRODUCER_CSR
    assert proof.is_file()
    payload = json.loads(proof.read_text())
    assert payload["already_in"] is True
    assert payload["bound"] is False


@pytest.mark.asyncio
async def test_connected_job_dry_run_plans_renew_policy_btn(tmp_path: Path):
    spec = RenewalJobSpec(
        applicant_id="21588091",
        policy_id="99",
        policy_number="PWC1239278",
        line_of_business="Workers comp",
        premium=Decimal("1200"),
        effective_date="2026-10-15",
        expiration_date="2027-10-15",
        writing_company="Associated Specialty",
        discussion_title=YES_WE_DO_TITLED,
        dry_run=True,
        env="test",
        proof_json=tmp_path / "plan.json",
    )
    result = await ManualPolicyRenewer(api_client=MagicMock()).run_connected_job(spec)
    assert result.status == "dry_run"
    assert any("#RenewPolicyBtn" in a for a in result.planned_actions)
    assert any("Writing Company" in a for a in result.planned_actions)
    assert result.bound is False


def test_write_proof_json(tmp_path: Path):
    from src.ezlynx.policy_renewer import RenewalJobResult

    result = RenewalJobResult(
        status="already_in",
        applicant_id="1",
        already_in=True,
        pending_shells=[{"transaction_type": "RWL", "premium": "10"}],
    )
    path = write_proof_json(result, tmp_path / "out.json")
    data = json.loads(path.read_text())
    assert data["status"] == "already_in"
    assert data["renew_button"] == RENEW_POLICY_BTN_SELECTOR or data["renew_button"] is None


def test_run_manual_renewal_script_imports():
    import importlib.util

    script = Path(__file__).resolve().parents[1] / "scripts" / "run_manual_renewal.py"
    spec = importlib.util.spec_from_file_location("run_manual_renewal", script)
    assert spec and spec.loader
    # Do not exec main — just confirm the file is a thin wrapper.
    text = script.read_text()
    assert "src.ezlynx.policy_renewer" in text
    assert "One connected CDP job" in text


def test_prod_live_without_carlo_blocked_via_renewer():
    spec = RenewalJobSpec(
        applicant_id="25156187",
        dry_run=False,
        env="prod",
        allow_live=None,
        producer=DEFAULT_PRODUCER_CSR,
        writing_company="X",
        premium=Decimal("1"),
        effective_date="2026-01-01",
        expiration_date="2027-01-01",
    )
    # run_connected_job is async; use asyncio via pytest mark below if needed.
    # Synchronous guard is the contract for production.
    with pytest.raises(LiveKeyingBlocked):
        assert_live_keying_allowed(spec)
