#!/usr/bin/env python3
"""Build the DRAFT carrier policy-change routing table from extractor output.

Input: /tmp/route_candidates.json (from extract_carrier_policy_change_routes.py)
Output: draft JSON. A human then curates the draft into
robie_job_engine/data/carrier_policy_change_routes.json.

Draft rules (conservative — when in doubt the record ships with NO route):
- A record ships a route only when a candidate names a concrete email,
  portal URL, or phone AND the sweep's own wording supports it.
- Portal-preferred wording ("do online", "done on website", "they don't
  like email", "agents do their own endorsements in the portal") ->
  type "portal" (worker cannot email these; reported for agent action).
- Fax-only rows, video-only rows, "put UW email", blank rows -> no route.
- Emails that are obviously fax aliases (*fax@*) -> no route.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

PORTAL_PREFERRED_RES = [
    re.compile(r"they don'?t like email", re.I),
    re.compile(r"\bdo online\b", re.I),
    re.compile(r"done on website", re.I),
    re.compile(r"done online via system", re.I),
    re.compile(r"agents? do their own endorsements? in the portal", re.I),
]
PORTAL_OK_RES = [
    re.compile(r"email or online", re.I),
    re.compile(r"do on website send via email", re.I),
]
FAX_ALIAS_RE = re.compile(r"fax@", re.I)
VIDEO_RE = re.compile(r"youtu\.?be|youtube", re.I)


def _route_type(texts: list[str], emails: list[str], urls: list[str],
                phones: list[str]) -> str | None:
    blob = " ".join(texts)
    if VIDEO_RE.search(blob) and not emails:
        return None
    if FAX_ALIAS_RE.search(" ".join(emails)):
        return None
    if any(r.search(blob) for r in PORTAL_PREFERRED_RES):
        return "portal"
    if emails:
        return "email"
    if urls:
        return "portal"
    if phones:
        return "phone"
    return None


def build(candidates_path: str) -> dict:
    recs = json.loads(Path(candidates_path).read_text(encoding="utf-8"))
    carriers = []
    for r in recs:
        cands = r["candidates"]
        emails: list[str] = []
        urls: list[str] = []
        phones: list[str] = []
        texts: list[str] = []
        labels: list[str] = []
        for c in cands:
            texts.append(c["text"])
            for e in c["emails"]:
                if e.lower() not in [x.lower() for x in emails]:
                    emails.append(e)
            for u in c["urls"]:
                if u not in urls:
                    urls.append(u)
            for p in c["phones"]:
                if p not in phones:
                    phones.append(p)
            label = c["text"].split("(")[0].split("|")[0].strip(" -0123456789.)")
            if label and label.lower() not in [x.lower() for x in labels]:
                labels.append(label)
        # Drop fax-alias emails; if nothing emailable remains, no email route.
        real_emails = [e for e in emails if not FAX_ALIAS_RE.search(e)]
        rtype = _route_type(texts, real_emails, urls, phones)
        routes = []
        if rtype == "email":
            routes.append({
                "label": "; ".join(labels[:3]) or "Policy Changes",
                "type": "email",
                "email": real_emails[0],
                "cc_emails": real_emails[1:],
                "phone": phones[0] if phones else None,
                "url": None,
                "notes": "",
            })
        elif rtype == "portal":
            routes.append({
                "label": "; ".join(labels[:3]) or "Policy Changes",
                "type": "portal",
                "email": real_emails[0] if real_emails else None,
                "cc_emails": real_emails[1:],
                "phone": phones[0] if phones else None,
                "url": urls[0] if urls else None,
                "notes": "directory prefers portal; email listed only if present",
            })
        elif rtype == "phone":
            routes.append({
                "label": "; ".join(labels[:3]) or "Policy Changes",
                "type": "phone",
                "email": None,
                "cc_emails": [],
                "phone": phones[0],
                "url": None,
                "notes": "phone-only route in directory",
            })
        carriers.append({
            "record_id": r["record_id"],
            "name": r["name"],
            "route_status": "ok" if routes else "missing",
            "routes": routes,
            "_source_texts": texts,
        })
    return {"carriers": carriers}


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: {argv[0]} <candidates.json> <draft.json>", file=sys.stderr)
        return 2
    data = build(argv[1])
    ok = sum(1 for c in data["carriers"] if c["route_status"] == "ok")
    print(f"{len(data['carriers'])} carriers, {ok} with draft routes")
    Path(argv[2]).write_text(json.dumps(data, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
