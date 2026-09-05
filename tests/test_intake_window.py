"""Tests for renewal intake filtering (30-45 day window)."""

import pytest
from datetime import date, timedelta
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database.models import Base, PolicyRenewal, RenewalStatus
from src.intake.base_source import RawRenewalItem
from src.intake.report_ingestor import ReportIngestor

@pytest.fixture
def test_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()

def test_intake_30_45_day_window_filter(test_db, tmp_path):
    today = date(2026, 9, 1)
    ingestor = ReportIngestor(input_dir=tmp_path)

    # 1. Inside window (35 days out)
    item_valid = RawRenewalItem(
        policy_number="POL-IN-WINDOW-01",
        insured_name="John Doe",
        applicant_id="EZL-101",
        carrier_name="Travelers",
        expiration_date=today + timedelta(days=35),
        line_of_business="Homeowners",
        discussion_title="Manual Homeowners Renewal",
        underwriter_email="underwriter@travelers.com"
    )

    # 2. Too soon (15 days out)
    item_too_soon = RawRenewalItem(
        policy_number="POL-TOO-SOON-02",
        insured_name="Jane Smith",
        applicant_id="EZL-102",
        carrier_name="Travelers",
        expiration_date=today + timedelta(days=15),
        line_of_business="Homeowners",
        discussion_title="Manual Homeowners Renewal"
    )

    # 3. Too far (60 days out)
    item_too_far = RawRenewalItem(
        policy_number="POL-TOO-FAR-03",
        insured_name="Bob Wilson",
        applicant_id="EZL-103",
        carrier_name="Travelers",
        expiration_date=today + timedelta(days=60),
        line_of_business="Homeowners",
        discussion_title="Manual Homeowners Renewal"
    )

    # Mock fetch_renewals to return all three
    ingestor.fetch_renewals = lambda target_date=None: [item_valid, item_too_soon, item_too_far]

    new_count, in_win = ingestor.sync_to_database(test_db, reference_date=today)

    assert in_win == 1
    assert new_count == 1

    saved = test_db.query(PolicyRenewal).all()
    assert len(saved) == 1
    assert saved[0].policy_number == "POL-IN-WINDOW-01"
    assert saved[0].discussion_title == "Manual Homeowners Renewal"
    assert saved[0].status == RenewalStatus.PENDING_EVALUATION

def test_ssrs_csv_parsing_and_source_filter(tmp_path):
    csv_file = tmp_path / "BOB_PolicyExpiration_Detail.csv"
    csv_file.write_text(
        'Policy #,Named Insured,Applicant ID,Master Company,Line of Business,Expiration Date,Annualized Premium,Source\n'
        'POL-MANUAL-1,Acme Corp,EZL-555,AmWINS MGA,Commercial Auto,10/15/2026,"$5,200.00",Manual\n'
        'POL-DOWNLOAD-2,Global Logistics,EZL-777,Progressive,Commercial Auto,10/18/2026,"$3,400.00",Download\n'
        'POL-MANUAL-3,Smith Residence,EZL-888,Universal Property,Homeowners,10/20/2026,"$2,100.00",Manual Entry\n'
    )

    ingestor = ReportIngestor(input_dir=tmp_path)
    items = ingestor.parse_csv_file(csv_file)

    # POL-DOWNLOAD-2 should be excluded because Source=Download
    assert len(items) == 2
    pol_nums = [i.policy_number for i in items]
    assert "POL-MANUAL-1" in pol_nums
    assert "POL-MANUAL-3" in pol_nums
    assert "POL-DOWNLOAD-2" not in pol_nums
    assert items[0].expiring_premium == 5200.0
    assert items[0].discussion_title == "Manual Commercial Auto Renewal"

def test_looker_scheduled_csv_parsing(tmp_path):
    looker_file = tmp_path / "Manual_Renewal_Queue_-_ROBIE_2026-09-02T0605.csv"
    looker_file.write_text(
        'Account Name,Applicant ID,Policy Number,Policy Effective Date,Policy Expiration Date,Master Company,Line Of Business,Premium - Annualized,Premium - Written,Branch,Department,Service Team,Assigned Producer,CSR,Preferred Language,Applicant Labels,Policy Labels,Policy Source,Total Policies,Total Customers,Total Annualized Premium,Total Written Premium\n'
        'Alisa & Thomas Casale,177939200,9323154474001,2026-10-25,2026-10-25,Travelers,Event Liability,$565.00,$565.00,Streetsmart Insurance,Commercial Lines (C/L),,Daniela Aguilar,Daniela Aguilar,English,,,Manual,1,1,$565.00,$565.00\n'
        'Tool Time-General Contracting LLC,30910368,WCNJ000391800,2026-01-13,2026-10-31,NJCRIB - Pennsylvania Manufacturers Assoc Ins Co,Workers comp,"$1,698.00","$1,698.00",Streetsmart Insurance,Commercial Lines,,Taylor Cimei,Jackie Arriola,English,,,Manual,1,1,"$1,698.00","$1,698.00"\n'
        'PEPPEP N SONS TRUCKING,169788491,TRK-991200,2026-01-01,2026-10-15,Canal,Commercial Auto,"$9,000.00","$9,000.00",Streetsmart Insurance,Trucking,,Maria Bara,Maria Bara,English,,,Manual,1,1,"$9,000.00","$9,000.00"\n'
    )

    ingestor = ReportIngestor(input_dir=tmp_path)
    items = ingestor.parse_csv_file(looker_file)

    # PEPPEP N SONS should be excluded by ID 169788491
    assert len(items) == 2
    pol_nums = [i.policy_number for i in items]
    assert "9323154474001" in pol_nums
    assert "WCNJ000391800" in pol_nums
    assert "TRK-991200" not in pol_nums
    assert items[0].insured_name == "Alisa & Thomas Casale"
    assert items[0].carrier_name == "Travelers"
    assert items[0].expiring_premium == 565.0
    assert items[0].source == "Manual"

def test_poll_email_reports_downloads_attachment(tmp_path):
    import base64
    from unittest.mock import MagicMock

    ingestor = ReportIngestor(input_dir=tmp_path)
    mock_gmail = MagicMock()
    mock_svc = MagicMock()
    mock_gmail.inbox_services = {"robie@streetsmart.insurance": mock_svc}

    # Mock list messages
    mock_svc.users().messages().list().execute.return_value = {
        "messages": [{"id": "msg_looker_123"}]
    }

    # Mock get message full payload
    mock_svc.users().messages().get().execute.return_value = {
        "id": "msg_looker_123",
        "payload": {
            "headers": [
                {"name": "Subject", "value": "Manual Renewal Queue - ROBIE"},
                {"name": "From", "value": "DoNotReply@appliedsystems.com"}
            ],
            "parts": [
                {
                    "filename": "Manual_Renewal_Queue.csv",
                    "body": {"attachmentId": "att_456"}
                }
            ]
        }
    }

    # Mock get attachment
    sample_csv = b"Policy Number,Account Name,Master Company,Policy Expiration Date,Policy Source\nPOL-1,Test Co,Travelers,2026-10-20,Manual\n"
    encoded = base64.urlsafe_b64encode(sample_csv).decode()
    mock_svc.users().messages().attachments().get().execute.return_value = {
        "data": encoded
    }

    downloaded = ingestor.poll_email_reports(gmail_client=mock_gmail)
    assert len(downloaded) == 1
    assert downloaded[0].exists()
    assert "Manual_Renewal_Queue.csv" in downloaded[0].name

