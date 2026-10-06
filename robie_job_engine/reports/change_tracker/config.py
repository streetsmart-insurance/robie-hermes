"""Configuration for the weekly Policy Change Request Tracker automation.

P1–P12 are LOCKED per Sandeep's 2026-10-05 answers; X1–X6 remain open.
Locked answers live here as CONFIG — nothing is hardcoded in the pipeline
code, so they plug in by editing this file only. Each setting cites the
question number and the assumption recorded in ASSUMPTIONS.md.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


# --- Looker report 4659 -------------------------------------------------------
# EZLynx report id -> look id is registered in robie_job_engine.report_registry
# (LOOK_ID_BY_REPORT["4659"] = "4659"); the dashboard URL needs ?isDashboard=true.
LOOKER_4659_URL = "https://app.ezlynx.com/web/looker-reports/report/4659?isDashboard=true"
LOOKER_4659_TITLE = "Policy Change Request Summary"

# P2: the Created-Date filter chip is changed from the 30-day default to the
# 1-year filter. The automation applies the filter in the UI and fails closed
# if the chip does not read back as the 1-year filter.
CREATED_DATE_FILTER_TEXT = "is in the last 1 year"
CREATED_DATE_FILTER_FIELD = "Change Request Created Date"

# P11: schema fingerprint — the exact expected column names AND ORDER of the
# 4659 detail-table CSV export. A rename, reorder, add, or drop fails closed.
EXPECTED_4659_COLUMNS = (
    "Applicant ID",
    "Account Name",
    "Assigned Producer",
    "Branch",
    "Policy Effective Date",
    "Expiration Date",
    "Line of Business",
    "Master Company",
    "Policy Number",
    "Written Premium",
    "Change Request Created By",
    "Change Request Created Date",
    "Request Status",
    "Policy Change Request ID",
    "Days Open",
)

# Summary tiles on the 4659 page (their values are the count cross-check source).
TILE_TOTAL_REQUESTS = "Total Policy Change Requests"
TILE_TOTAL_OPEN = "Total Open Change Requests"


# --- Destination sheet ---------------------------------------------------------
# 2026-10-06 (Carlo): new workbook created and shared with the production
# service account. The old workbook 403'd and the owner never shared it.
# Env var CHANGE_TRACKER_SPREADSHEET_ID still overrides this default.
CHANGE_TRACKER_SPREADSHEET_ID = os.environ.get(
    "CHANGE_TRACKER_SPREADSHEET_ID", ""
).strip() or "1soqTCgWtgxMmhRDRwZzk8UpMnPGPXEv5ScF633tNrCg"

# X2: timezone for tab-date math and Days Open reference date.
TIMEZONE = "America/New_York"

# Destination table headers, sheet columns A..Q (row 1), per the spec.
DEST_HEADERS = (
    "Action",                      # A (no raw source; filled by the team)
    "Change Request Created Date", # B
    "Days Open",                   # C
    "Account Name",                # D
    "Assigned Producer",            # E
    "Branch",                      # F
    "Effective Date",              # G (from raw "Policy Effective Date")
    "Line of Business",            # H
    "Master Company",              # I
    "Policy Number",               # J (PLAIN TEXT — precision-loss bug)
    "Written Premium",             # K
    "Change Request Created By",   # L
    "Request Status",              # M
    "Policy Change Request ID",    # N (NO plain-text format — P9 LOCKED: J only)
    "Expiration Date",             # O
    "Total Policy Change Requests",# P (summary tile value, repeated per row)
    "Total Open Change Requests",  # Q (summary tile value, repeated per row)
)

# Raw column -> destination column letter.
RAW_TO_DEST = {
    "Change Request Created Date": "B",
    "Days Open": "C",
    "Account Name": "D",
    "Assigned Producer": "E",
    "Branch": "F",
    "Policy Effective Date": "G",
    "Line of Business": "H",
    "Master Company": "I",
    "Policy Number": "J",
    "Written Premium": "K",
    "Change Request Created By": "L",
    "Request Status": "M",
    "Policy Change Request ID": "N",
    "Expiration Date": "O",
    # "Applicant ID" is intentionally dropped (not in the destination table).
}


# --- Teams ---------------------------------------------------------------------
# P6: fixed producer teams from the spec. Source of truth for roster changes is
# PENDING — the automation fails closed on unmapped producers instead of guessing.
TEAM_ROSTER: dict[str, tuple[str, ...]] = {
    "Personal": ("Jazmin Molina", "Daniela Aguilar"),
    "Commercial": ("Taylor Cimei", "Zeus Quezada", "Sandy Santana", "Carlo Ferrara", "Andrea Illanes"),
    "Trucking": ("Angie Valladarez", "Ricardo Aguilar", "Jake Ferrara", "Mike Sosa", "Maria Bara"),
}
TEAM_ORDER = ("Personal", "Commercial", "Trucking")
# Header-row text, e.g. "Personal - Jazmin, Daniela" in column B.
TEAM_HEADER_FILL = "#cfe2f3"  # Sheets palette "light blue 3"; pending reference-tab match


# --- Stale-flag rule -------------------------------------------------------------
# P5 LOCKED (Sandeep 2026-10-05): stale = Days Open > 30 for ALL teams and
# LOBs. STALE_DAYS_OPEN_BY_TEAM remains as an (empty) override extension
# point but is unused under the locked single-threshold rule.
STALE_DAYS_OPEN = 30
STALE_DAYS_OPEN_BY_TEAM: dict[str, int] = {}  # team name -> override threshold
# Sheets palette "light red 3" (the lightest red tint).
STALE_FILL = "#f4cccc"
STALE_FORMULA = "=$C2>30"


# --- Semantics -------------------------------------------------------------------
# P1 LOCKED (Sandeep 2026-10-05): "Task Created", "ongoing", "Error" = open;
# "Done" = closed. Rows with any other Request Status value FAIL CLOSED
# (never guessed). "Done" rows are filtered out and counted in
# stats.dropped_not_open; they still count toward the P10 tile check BEFORE
# filtering, since the tile counts all rows. NOTE: whether "Done" rows
# actually appear in the 4659 export is UNCONFIRMED — the code handles them
# either way.
OPEN_REQUEST_STATUSES: tuple[str, ...] | None = ("Task Created", "ongoing", "Error")
# Exact status value that means closed (P1 LOCKED; matched case-insensitively).
CLOSED_REQUEST_STATUS = "Done"

# P4: days-open rule — "calendar" = Created Date -> today in calendar days.
# The 4659 export already supplies Days Open; we use the report's value and
# recompute only if the column is missing.
DAYS_OPEN_RULE = "calendar"

# P12 LOCKED (Sandeep 2026-10-05): the tab covers Monday–Sunday of the week
# just closed, REGARDLESS of run day or holidays. week_ending = the most
# recent Sunday on or before the run date; the tab spans week_ending − 6d
# through week_ending (e.g. a Sunday 10/04 run builds "9/28-10/4", a run on
# Saturday 10/03 builds "9/21-9/27"). Unnamed-week overrides via --week-tab.
TAB_WEEK_RULE = "week-ending-sunday"

# X3: failure semantics — fail closed (raise, ship nothing partial). The weekly
# tab is written atomically (clear + full rewrite + formats); an existing
# non-empty tab refuses a second write unless --force is given (idempotency).
FAIL_CLOSED_ON_PARTIAL = True

# X6: assumed run day/time (no timer may be installed until the freeze lifts).
ASSUMED_RUN_SCHEDULE = "weekly Monday 06:30 America/New_York (assumed; pending X6)"


@dataclass(frozen=True)
class ChangeTrackerConfig:
    """Snapshot of the effective config, for logging / provenance."""

    spreadsheet_id: str = CHANGE_TRACKER_SPREADSHEET_ID
    timezone: str = TIMEZONE
    looker_url: str = LOOKER_4659_URL
    created_date_filter_text: str = CREATED_DATE_FILTER_TEXT
    expected_columns: tuple[str, ...] = EXPECTED_4659_COLUMNS
    dest_headers: tuple[str, ...] = DEST_HEADERS
    team_roster: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(TEAM_ROSTER)
    )
    team_order: tuple[str, ...] = TEAM_ORDER
    stale_days_open: int = STALE_DAYS_OPEN
    stale_days_open_by_team: dict[str, int] = field(
        default_factory=lambda: dict(STALE_DAYS_OPEN_BY_TEAM)
    )
    stale_fill: str = STALE_FILL
    team_header_fill: str = TEAM_HEADER_FILL
    open_request_statuses: tuple[str, ...] | None = OPEN_REQUEST_STATUSES
    closed_request_status: str = CLOSED_REQUEST_STATUS
    days_open_rule: str = DAYS_OPEN_RULE
    tab_week_rule: str = TAB_WEEK_RULE

    def stale_threshold_for(self, team: str) -> int:
        return self.stale_days_open_by_team.get(team, self.stale_days_open)


DEFAULT_CONFIG = ChangeTrackerConfig()
