#!/usr/bin/env python3
"""Mark robie@ unread read; file client-matched emails to EZLynx via note API."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from src.email_outreach.gmail_client import GmailRenewalClient
from src.ezlynx.api_client import EZLynxApiClient

ET = ZoneInfo("America/New_York")
ROBIE = "robie@streetsmart.insurance"

PRIORITY = [
    "Kevin Cardone",
    "Cardone",
    "Yes We Do",
    "Shoreline",
    "Maier",
    "Safe Man",
    "Pross",
]


def mark_all_unread(svc) -> int:
    marked = 0
    page_token = None
    while True:
        kwargs = {"userId": "me", "q": "is:unread", "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token
        res = svc.users().messages().list(**kwargs).execute()
        msgs = res.get("messages") or []
        if not msgs:
            break
        for i in range(0, len(msgs), 100):
            chunk = [m["id"] for m in msgs[i : i + 100]]
            svc.users().messages().batchModify(
                userId="me",
                body={"ids": chunk, "removeLabelIds": ["UNREAD"]},
            ).execute()
            marked += len(chunk)
        page_token = res.get("nextPageToken")
        if not page_token:
            break
    return marked


def headers_of(msg: Dict[str, Any]) -> Dict[str, str]:
    return {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}


def post_note(applicant_id: str, title: str, note: str) -> Dict[str, Any]:
    p = subprocess.run(
        [
            "/opt/renewal-automation-system/venv/bin/python3",
            "scripts/ezlynx_cli.py",
            "note",
            applicant_id,
            title,
            note,
            "--json",
        ],
        cwd="/opt/renewal-automation-system",
        capture_output=True,
        text=True,
        timeout=180,
    )
    out = (p.stdout or "") + "\n" + (p.stderr or "")
    return {"rc": p.returncode, "out": out[-800:]}


def main() -> None:
    client = GmailRenewalClient()
    svc = client.inbox_services[ROBIE]
    api = EZLynxApiClient()

    marked = mark_all_unread(svc)
    print("MARKED_READ", marked)

    res = svc.users().messages().list(
        userId="me",
        q=(
            "in:inbox newer_than:14d "
            "-from:robie@streetsmart.insurance "
            '-subject:"Robie Hourly" '
            '-subject:"Mailbox Audit" '
            '-subject:"Verification Code"'
        ),
        maxResults=40,
    ).execute()
    candidates = res.get("messages") or []
    print("CANDIDATES", len(candidates))

    filed: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    for m in candidates:
        mid = m["id"]
        full = svc.users().messages().get(userId="me", id=mid, format="full").execute()
        h = headers_of(full)
        subj = h.get("subject", "") or ""
        fr = h.get("from", "") or ""
        snip = full.get("snippet", "") or ""
        blob = f"{subj} {snip} {fr}".lower()

        if any(
            x in subj.lower()
            for x in (
                "verification code",
                "password",
                "out of office",
                "hourly inbox",
                "mailbox audit",
                "digest",
            )
        ):
            skipped.append({"id": mid, "reason": "noise", "subject": subj[:80]})
            continue

        match_name: Optional[str] = None
        for name in PRIORITY:
            if name.lower() in blob:
                match_name = "Kevin Cardone" if "cardone" in name.lower() else name
                break
        if not match_name:
            skipped.append({"id": mid, "reason": "no_client_match", "subject": subj[:80]})
            continue

        try:
            hits = api.search_applicants(match_name) or []
        except Exception as e:
            skipped.append({"id": mid, "reason": f"search_fail:{e}", "subject": subj[:80]})
            continue
        if not hits:
            skipped.append({"id": mid, "reason": "no_applicant", "subject": f"{match_name}|{subj[:50]}"})
            continue

        app = hits[0]
        aid = str(app.get("applicant_id") or app.get("id") or "")
        aname = app.get("name") or match_name
        now = datetime.now(ET).strftime("%m/%d/%Y %I:%M %p ET")
        note = (
            f"[{now}] Email filed from robie@ inbox via API.\n"
            f"Gmail ID: {mid}\nFrom: {fr}\nSubject: {subj}\n\n"
            f"Snippet: {snip[:400]}\n\nRobie was here"
        )

        titles = [
            "New Business",
            "Submission Center",
            "Policy Change Request",
            "Renewal",
            "Renewal Manual",
            "Workers Compensation",
        ]
        if "cardone" in match_name.lower():
            titles = ["New Business", "Submission Center", "Policy Change Request"] + titles

        posted = None
        last = None
        for title in titles:
            last = post_note(aid, title, note)
            out_l = (last.get("out") or "").lower()
            if last.get("rc") == 0 and (
                "note" in out_l or '"id"' in out_l or out_l.strip().startswith("{")
            ):
                posted = {"title": title, **last}
                break

        row = {
            "gmail": mid,
            "applicant_id": aid,
            "name": aname,
            "subject": subj,
            "posted": posted,
            "last": last,
        }
        filed.append(row)
        print("FILE", aid, aname, subj[:70], "OK" if posted else "FAIL")

    print("FILED", json.dumps(filed, indent=2)[:5000])
    print("SKIPPED_COUNT", len(skipped))
    for s in skipped[:30]:
        print("SKIP", s)
    left = svc.users().messages().list(userId="me", q="is:unread", maxResults=1).execute()
    print("UNREAD_LEFT", left.get("resultSizeEstimate", 0))


if __name__ == "__main__":
    main()
