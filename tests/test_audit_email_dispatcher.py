"""Unit tests for hardened Audit Email Dispatcher and Attachment Gates."""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from src.ezlynx.audit_email_dispatcher import (
    AuditEmailDispatcher,
    AuditAttachmentRequiredError,
    AuditTemplateMismatchError,
    AuditSenderSecurityError,
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
        )
    assert "CRITICAL SAFETY GATE: Refusing to send audit email" in str(exc.value)
    assert "0 documents were attached" in str(exc.value)


@pytest.mark.asyncio
async def test_send_audit_email_success_with_attachment():
    dispatcher = AuditEmailDispatcher()
    mock_page = MagicMock()

    mock_page.goto = AsyncMock()
    mock_page.wait_for_timeout = AsyncMock()
    mock_page.keyboard.press = AsyncMock()
    mock_page.screenshot = AsyncMock()

    mock_subject = AsyncMock()
    mock_subject.wait_for = AsyncMock()
    mock_subject.input_value.return_value = "Audit Request to Client"

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
    )

    assert result["sent"] is True
    assert result["verified_attachment_count"] == 1
    assert "Audit_Letter.pdf" in result["attached_documents"]
    assert mock_btn_send.nth.return_value.click.called
