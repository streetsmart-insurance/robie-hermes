import copy

import pytest

from robie_job_engine.lost_customer_retention import records
from robie_job_engine.lost_customer_sheet import (
    REVIEW_TAB, publish_retention_sheet, review_rows_to_delete, share_plan,
    verify_published_values,
)
from test_lost_customer_retention import HEADERS


RECIPIENTS = {"commercial": "sandy@x.com", "personal": "ashley@x.com", "trucking": "gabby@x.com",
              "carlo": "carlo@x.com", "jake": "jake@x.com"}


def _row(month, applicant):
    return [month, applicant, f"Acct {applicant}", "1", "Auto (Personal)", "P", "Personal Lines", "X", "Y",
            "$10", "Lost", "Unknown", "", "", "", "Low", "No matched record", "Not available", "", "Review", ""]


def test_share_plan_defaults_and_fail_closed():
    plan = share_plan(RECIPIENTS)
    assert plan == {"sandy@x.com": "reader", "ashley@x.com": "reader", "gabby@x.com": "reader",
                    "carlo@x.com": "writer", "jake@x.com": "writer"}
    with pytest.raises(ValueError):
        share_plan(RECIPIENTS, {"commercial": "owner"})
    with pytest.raises(ValueError):
        share_plan(RECIPIENTS, {"commercial": "reader"})


def test_review_rows_to_delete_keeps_header_blank_and_target_month():
    values = [HEADERS, _row("August 2026", "1"), _row("August 2026", "2"), _row("September 2026", "3"),
              [], _row("July 2026", "4"), _row("September 2026", "5")]
    assert review_rows_to_delete(values, ["September 2026"]) == [(5, 6), (1, 3)]


def test_verify_detects_drift():
    monthly = {"September 2026": [["ApplicantID"], ["1", "A"]]}
    review = records([HEADERS, _row("September 2026", "3")])
    ok = verify_published_values(monthly_expected=monthly, monthly_actual=copy.deepcopy(monthly),
                                 review_expected=review, review_actual_values=[HEADERS, _row("September 2026", "3")],
                                 months=["September 2026"])
    assert ok["review_rows"] == 1
    with pytest.raises(RuntimeError, match="does not match"):
        verify_published_values(monthly_expected=monthly, monthly_actual={"September 2026": [["ApplicantID"], ["1", "B"]]},
                                review_expected=review, review_actual_values=[HEADERS, _row("September 2026", "3")],
                                months=["September 2026"])
    with pytest.raises(RuntimeError, match="other months"):
        verify_published_values(monthly_expected=monthly, monthly_actual=monthly, review_expected=review,
                                review_actual_values=[HEADERS, _row("September 2026", "3"), _row("August 2026", "1")],
                                months=["September 2026"])


class _Exec:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeGoogle:
    """Minimal in-memory Sheets + Drive pair covering the calls we make."""

    def __init__(self, source_tabs):
        self.books = {"SRC": dict(source_tabs)}
        self.gids = {"SRC": {t: i + 1 for i, t in enumerate(source_tabs)}}
        self.perms = {}
        self.files = {}
        self.next_gid = 100
        self.created = 0

    # --- Sheets ---
    def spreadsheets(self):
        return self

    def sheets(self):
        return self

    def values(self):
        return self

    def get(self, spreadsheetId, range=None, majorDimension=None, fields=None):
        if range is None:
            return _Exec(lambda: {"sheets": [{"properties": {"title": t, "sheetId": g}}
                                             for t, g in self.gids[spreadsheetId].items()]})
        title = range.split("'")[1]
        return _Exec(lambda: {"values": copy.deepcopy(self.books[spreadsheetId][title])})

    def copyTo(self, spreadsheetId, sheetId, body):
        def run():
            dest = body["destinationSpreadsheetId"]
            title = next(t for t, g in self.gids[spreadsheetId].items() if g == sheetId)
            self.next_gid += 1
            new = f"Copy of {title}"
            self.books[dest][new] = copy.deepcopy(self.books[spreadsheetId][title])
            self.gids[dest][new] = self.next_gid
            return {"sheetId": self.next_gid, "title": new}
        return _Exec(run)

    def batchUpdate(self, spreadsheetId, body):
        def run():
            gids, book = self.gids[spreadsheetId], self.books[spreadsheetId]
            for req in body["requests"]:
                if "deleteSheet" in req:
                    title = next(t for t, g in gids.items() if g == req["deleteSheet"]["sheetId"])
                    del gids[title], book[title]
                elif "updateSheetProperties" in req:
                    props = req["updateSheetProperties"]["properties"]
                    old = next(t for t, g in gids.items() if g == props["sheetId"])
                    assert props["title"] not in gids or props["title"] == old
                    gids[props["title"]] = gids.pop(old)
                    book[props["title"]] = book.pop(old)
                elif "deleteDimension" in req:
                    rng = req["deleteDimension"]["range"]
                    title = next(t for t, g in gids.items() if g == rng["sheetId"])
                    del book[title][rng["startIndex"]:rng["endIndex"]]
            return {}
        return _Exec(run)

    # --- Drive ---
    def list(self, **kwargs):
        if "fileId" in kwargs:
            return _Exec(lambda: {"permissions": list(self.perms.get(kwargs["fileId"], []))})
        return _Exec(lambda: {"files": [{"id": i} for i, f in self.files.items() if f["mime"].endswith("folder")]})

    def create(self, body=None, fields=None, supportsAllDrives=None, fileId=None, sendNotificationEmail=None):
        def run():
            if fileId:
                assert sendNotificationEmail is False
                self.perms.setdefault(fileId, []).append(
                    {"id": f"p{len(self.perms[fileId])}", "emailAddress": body["emailAddress"],
                     "role": body["role"], "type": "user"})
                return {"id": "p"}
            self.created += 1
            new_id = f"F{self.created}"
            self.files[new_id] = {"mime": body["mimeType"], "name": body["name"], "trashed": False}
            if body["mimeType"].endswith("spreadsheet"):
                self.books[new_id] = {"Sheet1": []}
                self.gids[new_id] = {"Sheet1": 0}
                self.perms[new_id] = [{"id": "o", "emailAddress": "robie@x.com", "role": "owner", "type": "user"}]
            return {"id": new_id}
        return _Exec(run)

    def files_get(self, fileId, fields=None, supportsAllDrives=None):
        f = self.files[fileId]
        return _Exec(lambda: {"id": fileId, "trashed": f["trashed"], "mimeType": f["mime"]})

    def update(self, fileId, body, supportsAllDrives=None, permissionId=None):
        return _Exec(lambda: {})

    def permissions(self):
        return self


class FakeDrive:
    def __init__(self, google):
        self.g = google

    def files(self):
        g = self.g

        class Files:
            list = staticmethod(g.list)
            create = staticmethod(g.create)
            get = staticmethod(g.files_get)
            update = staticmethod(g.update)
        return Files()

    def permissions(self):
        return self.g


def _publish(fake, existing=None):
    monthly = {"September 2026": [["ApplicantID", "Name"], ["3", "Acct 3"]]}
    review_values = [HEADERS, _row("August 2026", "1"), _row("September 2026", "3")]
    fake.books["SRC"]["September 2026"] = monthly["September 2026"]
    fake.books["SRC"]["3-Month Account Review"] = review_values
    review = [r for r in records(review_values) if r["Month"] == "September 2026"]
    return publish_retention_sheet(
        sheets=fake, drive=FakeDrive(fake), source_spreadsheet_id="SRC", months=("September 2026",),
        period_label="September 2026", monthly_expected=monthly, review_expected=review,
        share=share_plan(RECIPIENTS), digest="d1", existing=existing, parent_folder_id="FOLDER",
    )


def test_publish_creates_verified_shared_copy_and_reuses_it():
    fake = FakeGoogle({"September 2026": [], "3-Month Account Review": []})
    record = _publish(fake)
    sid = record["spreadsheet_id"]
    assert record["created"] and record["url"] == f"https://docs.google.com/spreadsheets/d/{sid}/edit"
    assert list(fake.gids[sid]) == ["September 2026", REVIEW_TAB]
    assert [r[1] for r in fake.books[sid][REVIEW_TAB][1:]] == ["3"]
    granted = {p["emailAddress"]: p["role"] for p in fake.perms[sid]}
    assert granted["carlo@x.com"] == "writer" and granted["sandy@x.com"] == "reader"
    again = _publish(fake, existing=record)
    assert again["spreadsheet_id"] == sid and not again["created"] and not again["resynced"]
    assert fake.created == 1
    assert len(fake.perms[sid]) == 6  # owner + 5, no duplicates
