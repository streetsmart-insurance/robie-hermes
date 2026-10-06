"""RingCentral call-log pull and missed-call filtering.

Reuses the production RingCentralClient (robie_job_engine.ringcentral_client):
JWT bearer auth, GET /restapi/v1.0/account/~/call-log?view=Detailed. This
module does NOT build a new client (M15).

Missed-set assumption (M2, pending Sandeep): exactly {"Missed", "Voicemail"}.
The process doc's own check is "zero missed/voicemail calls for the date",
and those are the two result values the report is built around. Abandoned /
No Answer / Busy / Rejected are excluded until Sandeep confirms the set.
There is NO minimum-duration filter (the <5s hang-up question is unanswered).

Direction filter (process doc Phase 1 step 5): Inbound only, extension "All".
Filtering is done client-side so the shared client stays untouched.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .models import MissedCall
from .normalize import normalize_phone

# M2 LOCKED 2026-10-05 (Sandeep): exactly {"Missed", "Voicemail"}.
MISSED_RESULTS = {"missed", "voicemail"}


def _as_aware_utc(value: Any) -> datetime:
    from datetime import timezone

    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def pull_inbound_missed(
    client: Any,
    date_from_utc: datetime,
    date_to_utc: datetime,
) -> tuple[list[MissedCall], int]:
    """Fetch the window and return (missed_calls, inbound_calls_seen).

    ``client`` is anything with fetch_call_logs(date_from, date_to, view)
    returning RingCentralCall-like objects (duck-typed for tests; the real
    RingCentralClient in production).
    """
    records = client.fetch_call_logs(
        date_from=date_from_utc, date_to=date_to_utc, view="Detailed"
    )
    missed: list[MissedCall] = []
    inbound_seen = 0
    for rec in records:
        direction = str(getattr(rec, "direction", "") or "")
        if direction.lower() != "inbound":
            continue
        inbound_seen += 1
        result = str(getattr(rec, "result", "") or "")
        if result.strip().lower() not in MISSED_RESULTS:
            continue
        raw_number = str(getattr(rec, "from_number", "") or "")
        digits = normalize_phone(raw_number)
        if not digits:
            # No usable caller number: cannot dedupe or match. Counted in the
            # summary, never silently dropped into a row.
            continue
        employee = str(getattr(rec, "employee_name", "") or "")
        missed.append(
            MissedCall(
                call_id=str(getattr(rec, "call_id", "") or ""),
                from_number=raw_number,
                from_digits=digits,
                start_time=_as_aware_utc(getattr(rec, "start_time")),
                result=result,
                direction=direction,
                duration_seconds=int(getattr(rec, "duration_seconds", 0) or 0),
                extension=str(getattr(rec, "extension", "") or ""),
                employee_name=employee,
            )
        )
    return missed, inbound_seen
