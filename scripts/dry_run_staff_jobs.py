#!/usr/bin/env python3
"""Sandbox dry-run for the staff automation jobs.

- synthesis: reads the real last-7-days Gemini notes from Drive (via
  hatch_gws_cli), dedupes, classifies, synthesizes ONE department with Gemini,
  and prints what the email body would look like. No email is sent, no doc
  is modified.
- fun: generates this month's staff-fun chat post with Gemini and prints it.
  Nothing is posted to chat.

Usage: python3 scripts/dry_run_staff_jobs.py --job synthesis|fun
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    parser.add_argument("--job", required=True, choices=["synthesis", "fun"])
    args = parser.parse_args()
    if args.job == "synthesis":
        dry_synthesis()
    else:
        dry_fun()


if __name__ == "__main__":
    main()
