import pytest
from pathlib import Path
from src.ezlynx.document_uploader import (
    EZLynxDocumentUploader,
    FOLDER_ROUTING,
    LABEL_ROUTING,
    SUFFIX_ROUTING
)
from src.ezlynx.manual_renewal_gate import (
    RENEWAL_OFFER_FOLDER,
    resolve_renewal_offer_folder,
)

@pytest.mark.asyncio
async def test_uploader_no_valid_files():
    uploader = EZLynxDocumentUploader()
    res = await uploader.upload_document("99055770", Path("/non/existent/file.pdf"), "CUS062900594")
    assert res["success"] is False
    assert "File not found" in res["error"]

def test_routing_maps():
    assert "Loss Runs" in FOLDER_ROUTING["loss runs"]
    assert "Cancellations/NonRenewals/Reinstatements" in FOLDER_ROUTING["non renewal"]
    assert FOLDER_ROUTING["renewal"][0] == RENEWAL_OFFER_FOLDER
    assert "Renewal Offers/Declarations" in FOLDER_ROUTING["renewal"]

    assert LABEL_ROUTING["loss runs"] == "Loss Runs"
    assert LABEL_ROUTING["non renewal"] == "Non Renewal"
    assert LABEL_ROUTING["renewal"] == "Renewal Offer"

    assert SUFFIX_ROUTING["loss runs"] == "Loss Runs.pdf"
    assert SUFFIX_ROUTING["non renewal"] == "Non Renewal.pdf"
    assert SUFFIX_ROUTING["renewal"] == "Renewal Offer.pdf"
    assert "Documents" in FOLDER_ROUTING["correspondence"]
    assert LABEL_ROUTING["correspondence"] == "Correspondence"


def test_renewal_offer_folder_selected_or_created():
    """Carlo: Renewal Offer PDFs go in the Renewal Offer folder, not a policy# folder."""
    only_policy = resolve_renewal_offer_folder(["HONJ2025100027", "Applications"])
    assert only_policy.action == "create"
    assert only_policy.folder == "Renewal Offer"
    assert only_policy.created is True

    existing = resolve_renewal_offer_folder(["Loss Runs", "Renewal Offer", "HONJ2025100027"])
    assert existing.action == "matched"
    assert existing.folder == "Renewal Offer"
    assert existing.created is False

    alias = resolve_renewal_offer_folder(["Renewal Offers/Declarations"])
    assert alias.action == "matched"
    assert alias.folder == "Renewal Offers/Declarations"
