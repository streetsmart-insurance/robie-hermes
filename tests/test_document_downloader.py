"""One-shot firmed-quote PDF fetch: 0-byte reject, A-prefix normalize, happy path."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.ezlynx.document_downloader import (
    HITL_DOWNLOAD_MESSAGE,
    MIN_AUTHENTIC_PDF_BYTES,
    FirmedQuoteDownloadError,
    extract_premium_from_pdf,
    fetch_firmed_quote_pdf,
    is_authentic_pdf_bytes,
    known_good_download_path,
    known_good_download_url,
    normalize_ezlynx_download_id,
    select_firmed_quote_document,
)
from src.ezlynx.policy_renewer import (
    ManualPolicyRenewer,
    RenewalJobSpec,
    spec_from_args,
    build_parser,
)


def _authentic_pdf_bytes(payload: bytes = b"quote body") -> bytes:
    return b"%PDF-1.4\n" + payload + b"\n" + b"X" * MIN_AUTHENTIC_PDF_BYTES


def test_normalize_strips_leading_a_prefix():
    assert normalize_ezlynx_download_id("A675732963") == "675732963"
    assert normalize_ezlynx_download_id("675732963") == "675732963"
    assert normalize_ezlynx_download_id("/Download/A675732963") == "675732963"
    assert normalize_ezlynx_download_id("https://app.ezlynx.com/Download/A675732963") == "675732963"
    assert known_good_download_path("A675732963") == "/Download/675732963"
    assert known_good_download_url("A675732963").endswith("/Download/675732963")
    assert "/Download/A" not in known_good_download_url("A675732963")


def test_zero_byte_and_non_pdf_rejected():
    assert is_authentic_pdf_bytes(b"") is False
    assert is_authentic_pdf_bytes(b"%PDF") is False  # below size floor
    assert is_authentic_pdf_bytes(b"%PDF-1.4 stub" + b"\x00" * 100) is False
    assert is_authentic_pdf_bytes(b"<html>preview</html>" + b"Y" * MIN_AUTHENTIC_PDF_BYTES) is False
    assert is_authentic_pdf_bytes(_authentic_pdf_bytes()) is True


def test_happy_path_accepts_real_pdf(tmp_path: Path):
    dest = tmp_path / "fagone.pdf"
    calls: list[str] = []

    def http_get(url: str) -> bytes:
        calls.append(url)
        return _authentic_pdf_bytes(b"Fagone firmed quote")

    path = fetch_firmed_quote_pdf("A675732963", dest, http_get=http_get)
    assert path == dest
    assert dest.is_file()
    assert dest.read_bytes()[:4] == b"%PDF"
    assert dest.stat().st_size > MIN_AUTHENTIC_PDF_BYTES
    assert calls == ["https://app.ezlynx.com/Download/675732963"]


def test_zero_byte_retries_once_with_corrected_url_then_accepts(tmp_path: Path):
    dest = tmp_path / "retry.pdf"
    calls: list[tuple[str, str]] = []
    pdf = _authentic_pdf_bytes()

    class ClassicZero:
        def download_document_bytes(self, document_id: str) -> bytes:
            calls.append(("classic", document_id))
            return b""

    def http_get(url: str) -> bytes:
        calls.append(("portal", url))
        return pdf

    path = fetch_firmed_quote_pdf(
        "A675732963", dest, client=ClassicZero(), http_get=http_get
    )
    assert path.read_bytes()[:4] == b"%PDF"
    assert calls[0] == ("classic", "675732963")
    assert calls[1] == ("portal", "https://app.ezlynx.com/Download/675732963")
    assert all("Download/A" not in target for _, target in calls)


def test_zero_byte_after_one_corrected_retry_is_hitl(tmp_path: Path):
    dest = tmp_path / "fail.pdf"
    calls: list[str] = []

    class ClassicZero:
        def download_document_bytes(self, document_id: str) -> bytes:
            calls.append(f"classic:{document_id}")
            return b""

    def http_get(url: str) -> bytes:
        calls.append(url)
        return b""

    with pytest.raises(FirmedQuoteDownloadError, match="HITL") as excinfo:
        fetch_firmed_quote_pdf("A675732963", dest, client=ClassicZero(), http_get=http_get)
    assert "RadPdf" in str(excinfo.value) or "OCR" in HITL_DOWNLOAD_MESSAGE
    assert not dest.exists()
    assert len(calls) == 2
    assert calls[1] == "https://app.ezlynx.com/Download/675732963"
    assert all("/Download/A" not in c for c in calls)


def test_select_firmed_quote_prefers_firmed_filename():
    picked = select_firmed_quote_document(
        [
            {"Id": 1, "Description": "Applications"},
            {"Id": 675732963, "Description": "Fagone - J&J Home Quote Proposal (Firmed).pdf"},
            {"Id": 9, "Description": "2026-27 Renewal Offer - Other.pdf"},
        ]
    )
    assert picked is not None
    assert picked["Id"] == 675732963


def test_extract_premium_from_pdf_does_not_invent(tmp_path: Path):
    empty = tmp_path / "no-prem.pdf"
    empty.write_bytes(_authentic_pdf_bytes(b"no money fields here"))
    assert extract_premium_from_pdf(empty) is None

    quoted = tmp_path / "quoted.pdf"
    quoted.write_text(
        "HOMEOWNERS RENEWAL PROPOSAL\n"
        "Policy Number: HO-196126698\n"
        "Effective Date: 10/01/2026\n"
        "Expiration Date: 10/01/2027\n"
        "Total Renewal Premium: $1,812.00\n"
    )
    # Text file is not an authentic PDF — refuse rather than invent.
    assert extract_premium_from_pdf(quoted) is None

    real = tmp_path / "real.pdf"
    body = (
        b"HOMEOWNERS RENEWAL PROPOSAL\n"
        b"Total Renewal Premium: $1,812.00\n"
    )
    real.write_bytes(_authentic_pdf_bytes(body))
    assert extract_premium_from_pdf(real) == Decimal("1812.00")


@pytest.mark.asyncio
async def test_renewer_uses_extracted_premium_and_never_binds(tmp_path: Path, monkeypatch):
    pdf = tmp_path / "firmed.pdf"
    pdf.write_bytes(_authentic_pdf_bytes(b"Total Renewal Premium: $1,812.00\n"))

    monkeypatch.setattr(
        "src.ezlynx.document_downloader.resolve_firmed_quote_document_id",
        lambda client, applicant_id, document_id=None: "675732963",
    )
    monkeypatch.setattr(
        "src.ezlynx.document_downloader.fetch_firmed_quote_pdf",
        lambda document_id, dest_path, **kwargs: pdf,
    )
    # extract_premium_from_pdf is imported inside the method — patch the module it imports from
    monkeypatch.setattr(
        "src.ezlynx.document_downloader.extract_premium_from_pdf",
        lambda path: Decimal("1812.00"),
    )

    spec = RenewalJobSpec(
        applicant_id="196126698",
        policy_number="HO-FAGONE",
        line_of_business="Homeowners",
        carrier_name="Johnson & Johnson",
        premium=None,
        writing_company="Johnson & Johnson",
        effective_date="2026-10-01",
        expiration_date="2027-10-01",
        document_id="A675732963",
        dry_run=True,
        env="test",
        proof_json=tmp_path / "proof.json",
    )
    result = await ManualPolicyRenewer(api_client=MagicMock()).run_connected_job(spec)
    assert spec.premium == Decimal("1812.00")
    assert result.premium_source == "firmed_quote_pdf"
    assert result.firmed_quote_document_id == "675732963"
    assert result.bound is False
    assert result.status == "dry_run"
    assert any("firmed-quote" in a.lower() or "Download" in a for a in result.planned_actions)


def test_cli_accepts_document_id_and_fetch_flag():
    parser = build_parser()
    args = parser.parse_args(
        [
            "--applicant-id",
            "196126698",
            "--document-id",
            "A675732963",
            "--fetch-firmed-quote",
            "--dry-run",
            "--env",
            "test",
        ]
    )
    spec = spec_from_args(args)
    assert spec.document_id == "A675732963"
    assert spec.fetch_firmed_quote is True
    assert spec.dry_run is True


@patch("src.ezlynx.api_client.requests.get")
def test_classic_download_normalizes_a_prefix(mock_get):
    from src.ezlynx.api_client import EZLynxApiClient

    client = EZLynxApiClient(
        username="u",
        password="p",
        app_secret="s",
        client_id="c",
        client_secret="sec",
        integration_group_id="159",
    )
    client._classic_token = "tok"
    client._classic_token_time = 9999999999.0
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"Content-Type": "application/pdf"}
    mock_resp.content = _authentic_pdf_bytes()
    mock_get.return_value = mock_resp

    data = client.download_document_bytes("A675732963")
    assert data[:4] == b"%PDF"
    url = mock_get.call_args[0][0]
    assert url.endswith("/document/675732963")
    assert "A675732963" not in url
