#!/usr/bin/env python3
"""Send the weekly renewal-gap department emails from the server.

Uses domain-wide delegation: the hermes-poc service account impersonates
robie@streetsmart.insurance via the Gmail API.

Reads: /tmp/renewal_deepdive.json (for the per-policy lines)
       ~/workspace/robie-manual-ops/renewal-gaps-weekly.xlsx (attachment)
Sends: one email per department with gaps (Personal, Commercial, Trucking).

Requires: service account key at SA_KEY with domain-wide delegation
configured for the gmail.send scope in the Google Workspace Admin console.

Usage: renewal_gap_send_server.py [--dry-run]
  --dry-run prints what would be sent without sending.
"""
import base64
import json
import os
import sys
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

SA_KEY = os.path.expanduser(
    "~/workspace/robie-manual-ops/hermes-poc-sa-key.json")
DEEPDIVE = "/tmp/renewal_deepdive.json"
XLSX = os.path.expanduser(
    "~/workspace/robie-manual-ops/renewal-gaps-weekly.xlsx")
SHEET_URL = ("https://docs.google.com/spreadsheets/d/"
             "1yn5BBigaZTAfzzcEUKikar5syG4VoJ0nwh52h_Ocbi4/edit")
FROM = "robie@streetsmart.insurance"
SUBJECT = ("Renewals due within 20 days with no renewal in the system "
           "— action needed")

# Department -> (to, cc). Leads own the send; named producer/CSRs are CC'd.
RECIPIENTS = {
    "Personal": (["ashley@streetsmart.insurance"],
                 ["jazmin@streetsmart.insurance", "ana@streetsmart.insurance"]),
    "Commercial": (["sandy@streetsmart.insurance"],
                   ["eimy@streetsmart.insurance"]),
    # Trucking lead: Gabriela Chutin (no rows today; included for completeness)
    "Trucking": (["gabriela@streetsmart.insurance"], []),
}

STATUS_NEEDS_ENTRY = "MISSING \u2014 RENEWAL PAPERWORK IN FILE, NEEDS ENTRY"
STATUS_CHASE = "MISSING \u2014 NO RENEWAL IN SYSTEM, CHASE CARRIER"


def classify(rec):
    rdn = [n for n in (rec.get("renewal_doc_names") or [])
           if n and "non-renewal" not in n.lower()
           and "nonrenewal" not in n.lower()]
    return STATUS_NEEDS_ENTRY if rdn else STATUS_CHASE


def discussion_title(rec):
    discs = sorted(rec.get("discussions", []),
                   key=lambda d: d.get("lastModified", ""), reverse=True)
    if discs:
        return f"{discs[0]['title'][:60]} ({discs[0]['lastModified'][:10]})"
    return "no discussion"


def build_body(dept, records):
    chase = [r for r in records if classify(r) == STATUS_CHASE]
    entry = [r for r in records if classify(r) == STATUS_NEEDS_ENTRY]
    lines = [
        "Hi team,",
        "",
        f"{dept} has {len(records)} policies expiring within 20 days with "
        f"no renewal entered in the system \u2014 they're missing. "
        f"{len(chase)} have no renewal paperwork in the file at all and need "
        f"carrier/client outreach now; {len(entry)} have paperwork in the "
        f"file and need to be keyed in:",
        "",
    ]
    for r in sorted(records, key=lambda x: x.get("expires", "")):
        lines.append(
            f"- {r.get('expires', '')[:10]} | {r.get('policy', '')} | "
            f"{r.get('lob', '')} | {r.get('insured', '')} | "
            f"{classify(r)} \u2014 {discussion_title(r)}")
    lines += [
        "",
        f"Live tracker (always current): {SHEET_URL}",
        "",
        "Full details are in your department's tab of the Sheet and the "
        "attached spreadsheet.",
        "",
        "\u2014 Robie",
    ]
    return "\n".join(lines)


def gmail_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_file(
        SA_KEY,
        scopes=["https://www.googleapis.com/auth/gmail.send"],
        subject=FROM,
    )
    return build("gmail", "v1", credentials=creds)


def send_message(svc, to, cc, subject, body, attachment_path=None):
    msg = MIMEMultipart()
    msg["From"] = FROM
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    if attachment_path and os.path.exists(attachment_path):
        with open(attachment_path, "rb") as f:
            part = MIMEApplication(
                f.read(),
                _subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        part.add_header("Content-Disposition", "attachment",
                        filename=os.path.basename(attachment_path))
        msg.attach(part)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return svc.users().messages().send(
        userId="me", body={"raw": raw}).execute()


def main():
    dry_run = "--dry-run" in sys.argv
    with open(DEEPDIVE) as f:
        deepdive = json.load(f)
    by_dept = {}
    for r in deepdive:
        by_dept.setdefault(r.get("dept", "Commercial"), []).append(r)

    svc = None if dry_run else gmail_service()
    for dept in ["Personal", "Commercial", "Trucking"]:
        records = by_dept.get(dept, [])
        if not records:
            print(f"{dept}: no gaps, skipping")
            continue
        to, cc = RECIPIENTS[dept]
        body = build_body(dept, records)
        if dry_run:
            print(f"--- {dept} -> {to} (cc {cc}) ---")
            print(body[:400], "...\n")
            continue
        res = send_message(svc, to, cc, SUBJECT, body, XLSX)
        print(f"{dept}: sent to {to} (id {res['id']})")


if __name__ == "__main__":
    main()
