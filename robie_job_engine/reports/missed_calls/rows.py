"""Sheet-row assembly from deduped calls + phone lookups.

Column contract (process doc):
  A Department call received: the RingCentral-resolved employee name when the
     client mapped a real person, else BLANK (M4, locked 2026-10-05: Sandeep —
     department attribution lives on the app sheet, administration >
     employees; unmappable cells stay blank, never guessed).
  B Phone Number: (XXX) XXX-XXXX display.
  C Profile: hyperlinked account name when matched; plain "No Account" text
     when verified; a HUMAN-REVIEW note (plain text, no link) when the lookup
     is ambiguous or unavailable. The link points at the applicant Overview
     page: https://app.ezlynx.com/web/account/{id}/overview (process doc).
  D Was addressed?: ALWAYS BLANK. Human-only judgment (finding 2). This
     module has no heuristic and must never present one as a determination.
  E Updated by: ALWAYS BLANK ("for a human's name", process doc).

Profile name format: the process doc wants "Personal: {Name} & {Co-Applicant}"
or the commercial business name. The phone index only carries account_name,
so the row uses account_name verbatim (assumption; Sandeep was not asked
about co-applicant rendering because the export has no such column).
"""

from __future__ import annotations

from .date_rules import AGENCY_TZ
from .models import MissedCall, PhoneLookup, SheetRow
from .normalize import display_phone

EZLYNX_OVERVIEW_URL = "https://app.ezlynx.com/web/account/{applicant_id}/overview"

# Placeholder-looking employee names the RingCentral client emits when no
# extension->employee mapping resolved a real person.
_UNMAPPED_EMPLOYEE = {"", "Unassigned"}


def department_for(call: MissedCall) -> str:
    """Department cell. M4 (locked 2026-10-05): the resolved employee name,
    else BLANK. Never invents a department, never writes "Unknown"/"Ext N"."""
    name = (call.employee_name or "").strip()
    if name and name not in _UNMAPPED_EMPLOYEE and not name.startswith("Ext "):
        return name
    return ""


def profile_for(lookup: PhoneLookup) -> tuple[str, str]:
    """Return (profile_text, profile_url)."""
    if lookup.state == "matched":
        name = lookup.account_name or f"Applicant {lookup.applicant_id}"
        return name, EZLYNX_OVERVIEW_URL.format(applicant_id=lookup.applicant_id)
    if lookup.state == "verified_no_account":
        return "No Account", ""
    if lookup.state == "ambiguous":
        n = len(lookup.candidates)
        return f"AMBIGUOUS - {n} accounts share this number (human review)", ""
    if lookup.state == "unavailable":
        return "PHONE LOOKUP UNAVAILABLE (human review)", ""
    return "NOT CHECKED (human review)", ""


def build_rows(
    calls: list[MissedCall],
    lookups: dict[str, PhoneLookup],
) -> list[SheetRow]:
    """Assemble sheet rows. `lookups` keyed by from_digits."""
    rows: list[SheetRow] = []
    for call in sorted(calls, key=lambda c: c.start_time):
        lookup = lookups.get(call.from_digits) or PhoneLookup(
            state="not_checked", reason="lookup missing"
        )
        profile_text, profile_url = profile_for(lookup)
        first_et = call.start_time.astimezone(AGENCY_TZ).strftime("%Y-%m-%d %H:%M %Z")
        rows.append(
            SheetRow(
                department=department_for(call),
                phone_display=display_phone(call.from_digits),
                phone_digits=call.from_digits,
                profile_text=profile_text,
                profile_url=profile_url,
                lookup_state=lookup.state,
                first_call_at_et=first_et,
                call_count=call.duplicate_count,
                # addressed / updated_by intentionally left blank: human-only.
            )
        )
    return rows


def hyperlink_formula(url: str, text: str) -> str:
    """Sheets HYPERLINK formula; quotes escaped for the formula string."""
    safe_url = url.replace('"', '""')
    safe_text = text.replace('"', '""')
    return f'=HYPERLINK("{safe_url}","{safe_text}")'


def row_values(row: SheetRow) -> list[str]:
    """The 5 cell values for one sheet row, in column order A..E."""
    profile = (
        hyperlink_formula(row.profile_url, row.profile_text)
        if row.profile_url
        else row.profile_text
    )
    return [
        row.department,
        row.phone_display,
        profile,
        row.addressed,  # always ""
        row.updated_by,  # always ""
    ]
