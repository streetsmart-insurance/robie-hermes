from datetime import datetime, timezone
from pathlib import Path

import pytest

from robie_job_engine.accountability_dashboard import (
    DashboardPublicationError,
    publish_weekly_dashboard,
    verify_weekly_dashboard,
)


REPORT = """STREETSMART WEEKLY EXECUTIVE PERFORMANCE SCORECARD
• Open Submissions Over 30 Days: 4
• Policy Changes Over 7 Days: 3
• Policy Change Blocker Split: carrier 2; agency 1
• COI / Endorsement Exceptions: 5
• Retention Accounts Requiring Review: 6
• Expiration / Renewal Gaps: 2
• Overdue Tasks: 7
• Email Threads >24h Awaiting Employee: 8
• Sales Opportunities With No Recent Touch: 9
• Magellan At-Risk Calls: 1
"""


class _Request:
    def __init__(self, value): self.value = value
    def execute(self): return self.value


class _Values:
    def __init__(self):
        self.cells = {}
        self.labels = [["Reporting period"]] + [[] for _ in range(25)] + [
            ["Submission Center"], ["Policy changes"], ["Certificates / COIs"],
            ["Renewals / retention"], ["Service documentation"], ["Gmail response aging"],
            ["Sales inactivity"], ["Churn risk"],
        ]
    def get(self, **kwargs): return _Request({"values": self.labels})
    def batchUpdate(self, **kwargs):
        for item in kwargs["body"]["data"]:
            self.cells[item["range"].rsplit("!", 1)[-1]] = item["values"][0][0]
        return _Request({})
    def batchGet(self, **kwargs):
        return _Request({"valueRanges": [{"range": item, "values": [[self.cells.get(item.rsplit("!", 1)[-1], "")]]}
                                              for item in kwargs["ranges"]]})


class _Service:
    def __init__(self): self.values_api = _Values()
    def spreadsheets(self): return self
    def values(self): return self.values_api


def test_publishes_only_aggregate_cells_and_freshly_verifies(tmp_path: Path):
    report = tmp_path / "weekly.md"
    report.write_text(REPORT, encoding="utf-8")
    service = _Service()
    receipt = publish_weekly_dashboard(report, run_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
                                       config={"spreadsheet_id": "sheet-test", "sheet_name": "Agency Accountability"},
                                       service=service)
    assert receipt["verified"]
    assert all(cell.startswith(("B", "C", "D")) for cell in receipt["updated_cells"])
    assert verify_weekly_dashboard(receipt, service=service)[0]


def test_refuses_evidence_limited_report(tmp_path: Path):
    report = tmp_path / "weekly.md"
    report.write_text(REPORT + "\n⚠️ *DATA LIMITATIONS*\n", encoding="utf-8")
    with pytest.raises(DashboardPublicationError, match="evidence-limited"):
        publish_weekly_dashboard(report, run_at=datetime.now(timezone.utc),
                                 config={"spreadsheet_id": "x", "sheet_name": "y"}, service=_Service())
