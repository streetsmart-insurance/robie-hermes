"""Unit tests for hardened Audit Email Dispatcher, Subject Line Enforcement, and Attachment Gates."""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from src.ezlynx.audit_email_dispatcher import (
    AuditEmailDispatcher,
    AuditAttachmentRequiredError,
    AuditTemplateMismatchError,
    AuditSenderSecurityError,
    AuditSubjectMissingMetadataError,
)


def test_validate_template_for_status():
    # Completed audit must NOT allow request template
    with pytest.raises(AuditTemplateMismatchError) as exc_info:
        AuditEmailDispatcher.validate_template_for_status("completed", "Audit Request to Client")
    assert "Cannot send request template" in str(exc_info.value)

    # Pending client audit must NOT allow completed template
    with pytest.raises(AuditTemplateMismatchError) as exc_info:
        AuditEmailDispatcher.validate_template_for_status("pending_client", "Audit Worker Comp Audit Results")
    assert "Cannot send completion template" in str(exc_info.value)

    # Valid combinations should succeed without error
    AuditEmailDispatcher.validate_template_for_status("completed", "Audit Worker Comp Audit Results")
    AuditEmailDispatcher.validate_template_for_status("completed", "Audit Results (Completed Statement)")
    AuditEmailDispatcher.validate_template_for_status("pending_client", "Audit Request to Client")


def test_format_subject_with_insured_and_policy():
    # Case 1: Generic EZLynx template subject -> appends both insured name and policy
    raw_subj = "Action Required: Audit Update"
    formatted = AuditEmailDispatcher.format_subject_with_insured_and_policy(
        subject=raw_subj,
        insured_name="Acme Landscaping LLC",
        policy_number="0100327142-1",
    )
    assert formatted == "Action Required: Audit Update - Acme Landscaping LLC - Policy #0100327142-1"

    # Case 2: Subject already contains both -> untouched (no duplicate appending)
    already_complete = "Final Payroll Audit Request - Acme Landscaping LLC - Policy #0100327142-1"
    formatted = AuditEmailDispatcher.format_subject_with_insured_and_policy(
        subject=already_complete,
        insured_name="Acme Landscaping LLC",
        policy_number="0100327142-1",
    )
    assert formatted == already_complete

    # Case 3: Subject contains insured only -> appends policy number
    insured_only = "Action Required: Audit Update - Acme Landscaping LLC"
    formatted = AuditEmailDispatcher.format_subject_with_insured_and_policy(
        subject=insured_only,
        insured_name="Acme Landscaping LLC",
        policy_number="0100327142-1",
    )
    assert formatted == "Action Required: Audit Update - Acme Landscaping LLC - Policy #0100327142-1"

    # Case 4: Subject contains policy only -> appends insured name
    policy_only = "Action Required: Audit Update - Policy #0100327142-1"
    formatted = AuditEmailDispatcher.format_subject_with_insured_and_policy(
        subject=policy_only,
        insured_name="Acme Landscaping LLC",
        policy_number="0100327142-1",
    )
    assert formatted == "Action Required: Audit Update - Policy #0100327142-1 - Acme Landscaping LLC"

    # Case 5: Policy number already has prefix 'Pol #' or '#' -> avoids duplicate 'Policy #Policy #'
    formatted_pol = AuditEmailDispatcher.format_subject_with_insured_and_policy(
        subject="Audit Request to Client",
        insured_name="Acme Landscaping LLC",
        policy_number="Pol #0100327142-1",
    )
    assert formatted_pol == "Audit Request to Client - Acme Landscaping LLC - Pol #0100327142-1"

    # Case 6: Empty subject -> creates subject directly from identifiers
    formatted_empty = AuditEmailDispatcher.format_subject_with_insured_and_policy(
        subject="",
        insured_name="Acme Landscaping LLC",
        policy_number="0100327142-1",
    )
    assert formatted_empty == "Acme Landscaping LLC - Policy #0100327142-1"

    # Case 7: Punctuation normalization (comma in LLC)
    formatted_norm = AuditEmailDispatcher.format_subject_with_insured_and_policy(
        subject="Action Required: Audit Update - Acme Landscaping LLC - Policy #0100327142-1",
        insured_name="Acme Landscaping, LLC",
        policy_number="0100327142-1",
    )
    assert formatted_norm == "Action Required: Audit Update - Acme Landscaping LLC - Policy #0100327142-1"


@pytest.mark.asyncio
async def test_missing_subject_metadata_raises():
    dispatcher = AuditEmailDispatcher()
    mock_page = MagicMock()

    mock_page.goto = AsyncMock()
    mock_page.wait_for_timeout = AsyncMock()
    mock_page.keyboard.press = AsyncMock()
    mock_subject = AsyncMock()
    mock_subject.wait_for = AsyncMock()
    mock_subject.input_value.return_value = "Action Required: Audit Update"
    mock_from = AsyncMock()
    mock_from.count.return_value = 1
    mock_from.input_value.return_value = "Robie AI [robie@streetsmart.insurance]"

    def locator_side_effect(selector, **kwargs):
        if selector == "#subject":
            return mock_subject
        if selector == "#mat-input-1":
            return mock_from
        loc = AsyncMock()
        loc.count.return_value = 0
        return loc

    mock_page.locator.side_effect = locator_side_effect
    mock_page.evaluate = AsyncMock(return_value={})

    dispatcher.select_template = AsyncMock(return_value="Action Required: Audit Update")
    dispatcher.set_to_email = AsyncMock()
    dispatcher.set_cc_emails = AsyncMock()

    # Neither insured_name nor policy_number provided and DB/API/DOM empty
    with patch.object(dispatcher, "resolve_insured_and_policy", return_value=("", "")):
        with pytest.raises(AuditSubjectMissingMetadataError) as exc:
            await dispatcher.send_audit_email(
                page=mock_page,
                applicant_id="99999",
                to_email="test@example.com",
                cc_emails=[],
                template_name="Audit Request to Client",
                carrier_status="pending_client",
                search_terms=["Audit Notice"],
                insured_name="",
                policy_number="",
            )
        assert "CRITICAL SAFETY GATE: Refusing to send outbound audit email" in str(exc.value)
        assert "Named Insured" in str(exc.value)
        assert "Policy Number" in str(exc.value)


@pytest.mark.asyncio
async def test_ensure_from_robie_security():
    mock_page = MagicMock()
    mock_input = AsyncMock()

    # Case 1: From is Carlo -> Must raise AuditSenderSecurityError
    mock_input.count.return_value = 1
    mock_input.input_value.return_value = "Carlo Ferrara [carlo@streetsmart.insurance]"
    mock_page.locator.return_value = mock_input
    with pytest.raises(AuditSenderSecurityError) as exc:
        await AuditEmailDispatcher.ensure_from_robie(mock_page)
    assert "human Carlo Ferrara" in str(exc.value)

    # Case 2: From is another human / unknown -> Must raise
    mock_input.input_value.return_value = "Erika Palacios [erika@streetsmart.insurance]"
    with pytest.raises(AuditSenderSecurityError):
        await AuditEmailDispatcher.ensure_from_robie(mock_page)

    # Case 3: From is Robie -> Must succeed
    mock_input.input_value.return_value = "Robie AI [robie@streetsmart.insurance]"
    val = await AuditEmailDispatcher.ensure_from_robie(mock_page)
    assert val == "Robie AI [robie@streetsmart.insurance]"


@pytest.mark.asyncio
async def test_attachment_enforcement_gate_raises():
    dispatcher = AuditEmailDispatcher()
    mock_page = MagicMock()

    # Setup mock page behaviors
    mock_page.goto = AsyncMock()
    mock_page.wait_for_timeout = AsyncMock()
    mock_page.keyboard.press = AsyncMock()
    mock_page.screenshot = AsyncMock()

    # Locator mocks
    mock_subject = AsyncMock()
    mock_subject.wait_for = AsyncMock()
    mock_subject.input_value.return_value = "Test Subject"
    mock_subject.fill = AsyncMock()

    mock_from = AsyncMock()
    mock_from.count.return_value = 1
    mock_from.input_value.return_value = "Robie AI [robie@streetsmart.insurance]"

    def locator_side_effect(selector, **kwargs):
        if selector == "#subject":
            return mock_subject
        if selector == "#mat-input-1":
            return mock_from
        loc = AsyncMock()
        loc.count.return_value = 0
        return loc

    mock_page.locator.side_effect = locator_side_effect
    mock_page.evaluate = AsyncMock(return_value=[])

    # Mock helper methods
    dispatcher.select_template = AsyncMock(return_value="Audit Request to Client")
    dispatcher.set_to_email = AsyncMock()
    dispatcher.set_cc_emails = AsyncMock()
    dispatcher.attach_documents = AsyncMock(return_value=[])  # 0 attached
    dispatcher.count_attached_documents = AsyncMock(return_value=0)  # 0 in DOM

    # When enforce_attachments is True, it MUST raise AuditAttachmentRequiredError
    with pytest.raises(AuditAttachmentRequiredError) as exc:
        await dispatcher.send_audit_email(
            page=mock_page,
            applicant_id="12345",
            to_email="test@example.com",
            cc_emails=[],
            template_name="Audit Request to Client",
            carrier_status="pending_client",
            search_terms=["Audit Notice"],
            enforce_attachments=True,
            insured_name="Test Insured LLC",
            policy_number="POL12345",
        )
    assert "CRITICAL SAFETY GATE: Refusing to send audit email" in str(exc.value)
    assert "0 documents were attached" in str(exc.value)


@pytest.mark.asyncio
async def test_send_audit_email_success_with_attachment_and_hardened_subject():
    dispatcher = AuditEmailDispatcher()
    mock_page = MagicMock()

    mock_page.goto = AsyncMock()
    mock_page.wait_for_timeout = AsyncMock()
    mock_page.keyboard.press = AsyncMock()
    mock_page.screenshot = AsyncMock()

    mock_subject = AsyncMock()
    mock_subject.wait_for = AsyncMock()
    mock_subject.input_value.return_value = "Audit Request to Client"
    mock_subject.fill = AsyncMock()

    mock_from = AsyncMock()
    mock_from.count.return_value = 1
    mock_from.input_value.return_value = "Robie AI [robie@streetsmart.insurance]"

    mock_btn_send = AsyncMock()
    mock_btn_send.count.return_value = 1
    mock_btn_send.nth.return_value.inner_text = AsyncMock(return_value="Send")
    mock_btn_send.nth.return_value.click = AsyncMock()

    def locator_side_effect(selector, **kwargs):
        if selector == "#subject":
            return mock_subject
        if selector == "#mat-input-1":
            return mock_from
        if "#btnSend" in selector:
            return mock_btn_send
        loc = AsyncMock()
        loc.count.return_value = 0
        return loc

    mock_page.locator.side_effect = locator_side_effect
    mock_page.evaluate = AsyncMock(return_value=[])

    dispatcher.select_template = AsyncMock(return_value="Audit Request to Client")
    dispatcher.set_to_email = AsyncMock()
    dispatcher.set_cc_emails = AsyncMock()
    dispatcher.attach_documents = AsyncMock(return_value=["Audit_Letter.pdf"])
    dispatcher.count_attached_documents = AsyncMock(return_value=1)

    result = await dispatcher.send_audit_email(
        page=mock_page,
        applicant_id="12345",
        to_email="client@example.com",
        cc_emails=["csr@streetsmart.insurance"],
        template_name="Audit Request to Client",
        carrier_status="pending_client",
        search_terms=["Audit_Letter"],
        enforce_attachments=True,
        insured_name="Test Insured LLC",
        policy_number="POL12345",
    )

    expected_subject = "Audit Request to Client - Test Insured LLC - Policy #POL12345"
    assert result["sent"] is True
    assert result["verified_attachment_count"] == 1
    assert "Audit_Letter.pdf" in result["attached_documents"]
    assert result["subject"] == expected_subject
    assert result["insured_name"] == "Test Insured LLC"
    assert result["policy_number"] == "POL12345"
    # Verify subject input field in EZLynx compose UI was filled with hardened subject
    mock_subject.fill.assert_any_call(expected_subject)
    assert mock_btn_send.nth.return_value.click.called
