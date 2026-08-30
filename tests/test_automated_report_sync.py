#!/usr/bin/env python3
import base64
from pathlib import Path
from unittest.mock import MagicMock

from robie_job_engine.ezlynx_reports_crawler import EZLynxReportsCrawler
from robie_job_engine.ringcentral_email_sync import RingCentralEmailSync


def test_ezlynx_reports_crawler_mock(tmp_path):
    crawler = EZLynxReportsCrawler(output_dir=tmp_path)
    
    mock_page = MagicMock()
    mock_locator = MagicMock()
    mock_locator.count.return_value = 1
    mock_page.locator.return_value = mock_locator
    
    mock_download = MagicMock()
    mock_download_context = MagicMock()
    mock_download_context.__enter__.return_value = MagicMock(value=mock_download)
    mock_page.expect_download.return_value = mock_download_context
    
    task_res = crawler.export_task_aging_report(mock_page)
    assert task_res is not None
    assert "EZLynx_Task_Aging_Report" in str(task_res)
    assert mock_download.save_as.called


def test_ringcentral_email_sync_mock(tmp_path):
    sync = RingCentralEmailSync(output_dir=tmp_path)
    
    mock_service = MagicMock()
    mock_messages = mock_service.users.return_value.messages.return_value
    
    mock_messages.list.return_value.execute.return_value = {
        "messages": [{"id": "msg_123"}]
    }
    
    csv_payload = "Date,Time,Direction,From,To,Action Result\n08/30/2026,10:00 AM,Inbound,5551234567,5559876543,Missed\n"
    encoded_bytes = base64.urlsafe_b64encode(csv_payload.encode("utf-8")).decode("utf-8")
    
    mock_messages.get.return_value.execute.return_value = {
        "id": "msg_123",
        "payload": {
            "parts": [
                {
                    "filename": "CallLog_Daily.csv",
                    "body": {"data": encoded_bytes}
                }
            ]
        }
    }
    
    out_file = sync.fetch_latest_call_log_attachment(mock_service)
    assert out_file is not None
    assert out_file.exists()
    content = out_file.read_text()
    assert "5551234567" in content
