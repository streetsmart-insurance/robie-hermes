#!/usr/bin/env python3
"""Sandbox dry-run for the staff automation jobs.

- synthesis: reads the real last-7-days Gemini notes from Drive (via
  hatch_gws_cli), dedupes, classifies, synthesizes ONE department with Gemini,
  and prints what the email body would look like. No email is sent, no doc
  is modified.
- fun: generates this month's staff-fun chat post with Gemini and prints it.
  Nothing is posted to chat.
- holiday: reads the Holiday Schedule Google Doc (HTML export, or --fixture)
  and prints the alerts that would go out. Never sends email or Chat, and
  does not write the send ledger.

Usage: python3 scripts/dry_run_staff_jobs.py --job synthesis|fun|holiday
       python3 scripts/dry_run_staff_jobs.py --job holiday --fixture tests/fixtures/holiday_schedule_2026_2027.html --today 2026-10-05
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Sandbox ADC for Secret Manager (Gemini key). Box jobs use ambient ADC.
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.expanduser("~/.config/gcp/hermes-poc-key.json"),
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from robie_job_engine import meeting_synthesis as ms
from robie_job_engine import staff_fun as sf
from robie_job_engine import staff_jobs_common as common

GWS = "/opt/hatch/bin/hatch_gws_cli"


def gws_drive_list(query: str) -> list[dict]:
    out = subprocess.run(
        [GWS, "drive", "files", "list", "--params", json.dumps({
            "q": query, "pageSize": 100, "supportsAllDrives": True,
            "includeItemsFromAllDrives": True,
            "fields": "files(id,name,modifiedTime,mimeType,shortcutDetails/targetId)",
            "orderBy": "modifiedTime desc",
        })],
        capture_output=True, text=True, check=True,
    )
    files = json.loads(out.stdout).get("files", [])
    for f in files:
        if f.get("mimeType") == "application/vnd.google-apps.shortcut":
            target = (f.get("shortcutDetails") or {}).get("targetId")
            if target:
                f["id"] = target
    return files


def gws_doc_text(doc_id: str) -> str:
    out = subprocess.run(
        [GWS, "docs", "documents", "get", "--params", json.dumps({"documentId": doc_id})],
        capture_output=True, text=True, check=True,
    )
    return ms.doc_text(json.loads(out.stdout))


def dry_synthesis() -> None:
    since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    raw = gws_drive_list(
        f"name contains 'Notes by Gemini' and modifiedTime > '{since}' and trashed=false"
    )
    notes = ms.dedupe_notes(raw)
    print(f"raw notes: {len(raw)} -> unique after dedupe: {len(notes)}")

    by_dept: dict[str, list[str]] = {}
    skipped = 0
    for note in notes:
        name = note["name"]
        try:
            body = gws_doc_text(note["id"])
        except subprocess.CalledProcessError as exc:
            print(f"  (could not read {name[:50]}: {exc})")
            continue
        if len(body) < 40:
            skipped += 1
            continue
        by_dept.setdefault(ms.classify_department(name), []).append((name, body))

    print(f"skipped empty: {skipped}")
    for dept in sorted(by_dept):
        print(f"  {dept}: {len(by_dept[dept])} notes")

    # Synthesize the department with the most notes to keep the dry run fast.
    dept = max(by_dept, key=lambda d: len(by_dept[d]))
    combined = "\n\n".join(f"--- {ms.strip_suffix(n)} ---\n{b[:4000]}" for n, b in by_dept[dept])
    print(f"\n=== synthesizing department: {dept} ({len(combined)} chars of notes) ===")
    section = ms.synthesize_department(dept, combined)
    print(section)
    print("\n=== sample email subject/body (NOT sent) ===")
    print(f"Subject: Weekly meeting synthesis — week of ... (dry run)")
    body = ms.build_email_body("week of ... (dry run)", {dept: section},
                               "1. sample draft\n2. sample draft")
    print(body[:1500])
    print("\nDRY-RUN OK: no email sent, no doc modified.")


def _fetch_holiday_html() -> str:
    """HTML export keeps smart-chip dates. Docs API text runs drop them."""
    from robie_job_engine import staff_holiday_alert as holiday

    if os.environ.get("ROBIE_GOOGLE_TOKEN_FILE", "").strip():
        return holiday.fetch_holiday_doc_html()
    gws = os.environ.get("HATCH_GWS_CLI", GWS)
    proc = subprocess.run(
        [gws, "drive", "files", "export", "--params", json.dumps({
            "fileId": holiday.HOLIDAY_DOC_ID,
            "mimeType": "text/html",
        })],
        capture_output=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or b"").decode("utf-8", errors="replace")[:400]
        raise SystemExit(
            "Could not export the Holiday Schedule Doc. Pass --fixture, or set "
            f"ROBIE_GOOGLE_TOKEN_FILE. gws said: {detail}"
        )
    text = proc.stdout.decode("utf-8", errors="replace")
    if "<table" not in text.lower():
        raise SystemExit(
            "Holiday Doc export did not contain an HTML table. Pass --fixture "
            "or check the gws drive files export command."
        )
    return text


def dry_holiday(fixture: str, today_text: str) -> None:
    from robie_job_engine import staff_holiday_alert as holiday

    document = Path(fixture).read_text(encoding="utf-8") if fixture else _fetch_holiday_html()
    rows, alerts = holiday.load_schedule_alerts(document)
    today = (
        date.fromisoformat(today_text)
        if today_text
        else datetime.now(ZoneInfo(holiday.TIMEZONE)).date()
    )
    heads_up_days = holiday.LEAD_DAYS_HEADS_UP
    nudge_days = holiday.LEAD_DAYS_NUDGE
    due = holiday.select_due(alerts, today, heads_up_days=heads_up_days, nudge_days=nudge_days)
    due_by_key = {alert.ledger_key(): alert for alert in due}
    print(f"doc: {holiday.DOC_TITLE}")
    print(f"source: {holiday.DOC_URL}")
    print(f"parsed rows: {len(rows)}  events: {len(alerts)}")
    print(
        f"today: {today.isoformat()} {holiday.TIMEZONE}  "
        f"heads_up=T-{heads_up_days}  nudge=T-{nudge_days}"
    )
    print("\n=== both phases (past dates are not alerted) ===")
    for alert in alerts:
        delta = (alert.event_date - today).days
        when = alert.close_time or alert.status_kind
        heads_day = alert.event_date - timedelta(days=heads_up_days)
        nudge_day = alert.event_date - timedelta(days=nudge_days)
        print(
            f"  {alert.event_date.isoformat()} {alert.status_kind:11} {when:8} {alert.holiday}"
        )
        for phase, opens in (
            (holiday.PHASE_HEADS_UP, heads_day),
            (holiday.PHASE_NUDGE, nudge_day),
        ):
            phased = alert.with_phase(phase)
            if phased.ledger_key() in due_by_key:
                mark = "WOULD SEND"
            elif delta < 0:
                mark = "past"
            elif phase == holiday.PHASE_HEADS_UP and delta <= nudge_days:
                mark = "window closed"
            else:
                mark = "later"
            print(f"      {phase:8} opens {opens.isoformat()}  {mark}")
    for phase in (holiday.PHASE_HEADS_UP, holiday.PHASE_NUDGE):
        phase_due = [alert for alert in due if alert.phase == phase]
        print(f"\n=== WOULD SEND {phase} ({len(phase_due)}) ===")
        if not phase_due:
            print("(none)")
        for alert in phase_due:
            print(f"\n--- {alert.ledger_key()} ---")
            print(f"Subject: {holiday.email_subject(alert)}")
            print(holiday.email_body(alert))
            print("\nChat:")
            print(holiday.chat_text(alert))
    print("\nDRY-RUN OK: no email sent, no chat posted, ledger unchanged.")


def dry_fun() -> None:
    from datetime import date
    today = date.today()
    text, plan = sf.generate_post(today.month, today.year)
    print(f"activity: {plan['name']} | scope: {plan['scope']} | entry: {plan['entry_method']}")
    print("\n=== chat post (NOT posted) ===")
    print(text)
    print("\nDRY-RUN OK: nothing posted to chat, no email sent.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True, choices=["synthesis", "fun", "holiday"])
    parser.add_argument(
        "--fixture",
        default="",
        help="Holiday job only: local HTML or markdown export. Skips the live Doc.",
    )
    parser.add_argument(
        "--today",
        default="",
        help="Holiday job only: YYYY-MM-DD in place of America/New_York today.",
    )
    args = parser.parse_args()
    if args.job == "synthesis":
        dry_synthesis()
    elif args.job == "fun":
        dry_fun()
    else:
        dry_holiday(args.fixture, args.today)


if __name__ == "__main__":
    main()
