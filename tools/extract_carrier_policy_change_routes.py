#!/usr/bin/env python3
"""Extract carrier policy-change routing candidates from the 2026-09-27
carrier-directory full-sweep markdown.

Source: ~/workspace/worker-test-2026-09-26/carrier-directory-full-sweep-20260927.md
(deep read of all 206 EZLynx company-directory records).

Output: JSON list of {record_id, name, view_id, candidates:[{text, emails,
urls, phones, route_hint}]}. This is a FIRST PASS for human curation into
robie_job_engine/data/carrier_policy_change_routes.json — the curated file
is what the worker ships. Re-run this script when Nicole's directory
fill-in work adds new policy-change contacts, then re-curate the diff.

Heuristic: within each record section, any contact row or note mentioning
policy changes / endorsements is a candidate. Route hints:
  email  - an email address present on the candidate
  portal - "done on website" / "do online" / "via system" wording
  phone  - phone present but no email
  ambiguous - "put uw email", blank rows, video-only links
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
URL_RE = re.compile(r"https?://[^\s)\"']+")
PHONE_RE = re.compile(r"\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}(?:\s*(?:x|ext\.?)\s*\d+)?")

SECTION_RE = re.compile(r"^### #(\d+)\s+(.+)$", re.MULTILINE)
NAME_CLEAN_RE = re.compile(r"\s+[—–]\s+.*$|\s+-\s+.*$|\s*\(view/[^)]*\)\s*$")
VIEW_RE = re.compile(r"\(view/([^)]*)\)")

POLICY_CHANGE_RES = [
    re.compile(r"policy[\s-]?changes?", re.IGNORECASE),
    re.compile(r"endorsements?", re.IGNORECASE),
]

PORTAL_HINTS = (
    "done on website", "do online", "via system", "done online",
    "do on website", "online via",
)
AMBIGUOUS_HINTS = (
    "put uw email", "put underwriter", "tbd", "video link", "youtu",
    "how-to", "how to",
)


def _clean_name(raw: str) -> tuple[str, str]:
    view = ""
    m = VIEW_RE.search(raw)
    if m:
        view = m.group(1)
    name = NAME_CLEAN_RE.sub("", raw).strip()
    return name, view


def _chunk_text(section: str) -> list[str]:
    """Split a record section into contact-row-ish chunks.

    Contact rows are separated by "; " — but only at paren depth 0, so
    "POLICY CHANGES (Email and fax; karen.costello@farmersinsurance.com)"
    stays one chunk. Entry-field lines are split by newline.
    """
    parts: list[str] = []
    for line in section.splitlines():
        line = line.strip("- ").strip()
        if not line:
            continue
        chunks: list[str] = []
        depth = 0
        current: list[str] = []
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(0, depth - 1)
            if ch == ";" and depth == 0 and line[i:i + 2] == "; ":
                chunks.append("".join(current).strip())
                current = []
                i += 2
                continue
            current.append(ch)
            i += 1
        tail = "".join(current).strip()
        if tail:
            chunks.append(tail)
        parts.extend(c for c in chunks if c)
    return parts


def _label(text: str) -> str:
    """The contact-row label: text before the first '(' or '|' or '←'."""
    head = re.split(r"[\(|←]", text, maxsplit=1)[0]
    return head.strip(" -:").strip()


def _is_routing_row(text: str) -> bool:
    """A contact row is a policy-changes ROUTE (not a how-to link or stray
    mention) when the sweep annotated it as such or its own label says so."""
    lowered = text.casefold()
    if "← polic" in lowered and "change" in lowered:
        return True  # sweep's own "← POLICY CHANGES routing:" verdict
    if "policy-changes hit" in lowered:
        return True  # sweep's own verdict
    label = _label(text).casefold()
    if "policy change" in label:
        return True
    # Named-person rows whose contact TYPE is policy changes:
    # "Blake Hohlbein (Policy Changes; bhohlbein@americanspecialty.com; ...)".
    if re.search(r"\(\s*policy changes?\b", text, re.IGNORECASE):
        return True
    # "Endorsements/Policy Changes"-style labels: the word "policy" (not
    # "policies" as in "direct bill policies go through Brian S").
    if "endorsement" in label and re.search(r"\bpolicy\b", label):
        return True
    return False


def _mentions_policy_change(text: str) -> bool:
    return _is_routing_row(text)


def extract(sweep_path: str) -> list[dict]:
    text = Path(sweep_path).read_text(encoding="utf-8")
    matches = list(SECTION_RE.finditer(text))
    records: list[dict] = []
    for i, match in enumerate(matches):
        record_id = match.group(1)
        name, view = _clean_name(match.group(2))
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        section = text[match.end():end]
        candidates = []
        for chunk in _chunk_text(section):
            if not _mentions_policy_change(chunk):
                continue
            lowered = chunk.casefold()
            emails = sorted(set(EMAIL_RE.findall(chunk)))
            # Drop obvious non-emails the regex over-matches.
            emails = [e for e in emails if not e.lower().endswith((".png", ".jpg"))]
            urls = sorted(set(URL_RE.findall(chunk)))
            phones = sorted(set(PHONE_RE.findall(chunk)))
            if any(h in lowered for h in PORTAL_HINTS):
                hint = "portal"
            elif any(h in lowered for h in AMBIGUOUS_HINTS):
                hint = "ambiguous"
            elif emails:
                hint = "email"
            elif urls:
                hint = "portal"
            elif phones:
                hint = "phone"
            else:
                hint = "none"
            candidates.append({
                "text": chunk[:400],
                "emails": emails,
                "urls": urls,
                "phones": phones,
                "route_hint": hint,
            })
        records.append({
            "record_id": record_id,
            "name": name,
            "view_id": view,
            "candidates": candidates,
        })
    return records


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: {argv[0]} <sweep.md> <out.json>", file=sys.stderr)
        return 2
    records = extract(argv[1])
    with_route = sum(1 for r in records if r["candidates"])
    Path(argv[2]).write_text(json.dumps(records, indent=2), encoding="utf-8")
    print(f"{len(records)} records, {with_route} with policy-change candidates -> {argv[2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
