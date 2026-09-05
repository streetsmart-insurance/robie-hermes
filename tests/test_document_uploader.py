import pytest
from pathlib import Path
from src.ezlynx.document_uploader import (
    EZLynxDocumentUploader,
    FOLDER_ROUTING,
    LABEL_ROUTING,
    SUFFIX_ROUTING
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
    assert "Renewal Offers/Declarations" in FOLDER_ROUTING["renewal"]

    assert LABEL_ROUTING["loss runs"] == "Loss Runs"
    assert LABEL_ROUTING["non renewal"] == "Non Renewal"
    assert LABEL_ROUTING["renewal"] == "Renewal Offer"

    assert SUFFIX_ROUTING["loss runs"] == "Loss Runs.pdf"
    assert SUFFIX_ROUTING["non renewal"] == "Non Renewal.pdf"
    assert SUFFIX_ROUTING["renewal"] == "Renewal Offer.pdf"
    assert "Documents" in FOLDER_ROUTING["correspondence"]
    assert LABEL_ROUTING["correspondence"] == "Correspondence"
