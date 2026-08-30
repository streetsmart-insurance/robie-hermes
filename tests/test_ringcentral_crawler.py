#!/usr/bin/env python3
from unittest.mock import MagicMock

from robie_job_engine.ringcentral_reports_crawler import RingCentralReportsCrawler


def test_ringcentral_reports_crawler_mock(tmp_path):
    crawler = RingCentralReportsCrawler(output_dir=tmp_path)
    
    mock_page = MagicMock()
    mock_page.url = "https://service.ringcentral.com/application/company/callLog"
    
    mock_locator = MagicMock()
    mock_locator.count.return_value = 1
    mock_page.locator.return_value = mock_locator
    
    mock_download = MagicMock()
    mock_download_context = MagicMock()
    mock_download_context.__enter__.return_value = MagicMock(value=mock_download)
    mock_page.expect_download.return_value = mock_download_context
    
    out_file = crawler.export_call_log_csv(mock_page)
    assert out_file is not None
    assert "RingCentral_CallLog" in str(out_file)
    assert mock_download.save_as.called
