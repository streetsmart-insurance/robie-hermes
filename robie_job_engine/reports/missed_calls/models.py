"""Data models for the Missed Call Report pipeline.

The sheet's fixed columns (per the process doc) are:
  A: Department call received
  B: Phone Number
  C: Profile (hyperlinked when matched, plain "No Account" text otherwise)
  D: Was addressed? (Yes/No/blank)
  E: Updated by (always blank)

Columns D and E are HUMAN-ONLY. The pipeline always writes them blank and
never modifies an existing row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class MissedCall:
    """One inbound missed/voicemail call from the RingCentral log."""

    call_id: str
    from_number: str  # raw, as returned by RingCentral (usually E.164)
    from_digits: str  # normalized digits used as the dedupe/match key
    start_time: datetime  # tz-aware (UTC from the API)
    result: str  # "Missed" / "Voicemail" (see MISSED_RESULTS in ringcentral_pull)
    direction: str = "Inbound"
    duration_seconds: int = 0
    extension: str = ""
    employee_name: str = ""
    duplicate_count: int = 1  # how many same-day calls from this number were merged


@dataclass
class PhoneLookup:
    """Outcome of the offline phone-index lookup.

    States mirror the watchdog's build_phone_index contract exactly:
      matched            exactly one applicant for this number
      ambiguous          number matches 2+ applicants -> human review, fail closed
      verified_no_account full book was checked, number not found
      unavailable        index missing/stale/errored -> NOT "no account"
      not_checked        lookup was never attempted (bad input)
    """

    state: str
    applicant_id: str = ""
    account_name: str = ""
    applicant_type: str = ""
    matched_via: list[str] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""


@dataclass
class SheetRow:
    """One row to append to a dated tab. addressed/updated_by stay blank."""

    department: str
    phone_display: str
    phone_digits: str
    profile_text: str  # account name, "No Account", or a human-review note
    profile_url: str  # EZLynx overview URL, or "" for plain text
    lookup_state: str
    first_call_at_et: str = ""
    call_count: int = 1
    # HUMAN-ONLY columns: always blank from automation. Declared here so the
    # sheet writer cannot accidentally fill them.
    addressed: str = ""
    updated_by: str = ""


@dataclass
class RunSummary:
    """Per-date outcome of one pipeline run."""

    target_date: str  # ISO date, e.g. "2026-10-04"
    tab_name: str  # M/D, e.g. "10/4"
    inbound_calls_seen: int = 0
    missed_calls_seen: int = 0
    unique_numbers: int = 0
    duplicates_removed: int = 0
    cross_date_duplicates_removed: int = 0  # M5: number already on an
    # earlier date's tab this run -> dropped (report-wide "only once")
    emitted_numbers: list[str] = field(default_factory=list)  # from_digits
    # emitted on this date's tab; run() threads these across dates (M5)
    unmapped_departments: list[str] = field(default_factory=list)  # M4:
    # distinct extensions whose department cell was left blank this date
    # (the "email Sandeep" signal; surfaced in run output, never auto-sent)
    lookup_states: dict[str, int] = field(default_factory=dict)
    existing_rows_skipped: int = 0
    rows_appended: int = 0
    dry_run: bool = True
    fail_closed: bool = False
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Rows written (or that would be written in dry-run) for this date.
    # Used by the EOD digest poster. Empty when the pull failed.
    rows: list["SheetRow"] = field(default_factory=list)
