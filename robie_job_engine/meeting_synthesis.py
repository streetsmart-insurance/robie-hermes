"""Weekly meeting synthesis job (meeting.synthesis.weekly).

Reads the prior 7 days of "Notes by Gemini" Google Docs, dedupes, classifies
each note's department by title keywords, synthesizes per-department sentiment
/ wins / struggles with the Gemini API, emails the synthesis to Carlo from
robie@streetsmart.insurance, and appends 2-3 social-post drafts (from the wins)
to the "StreetSmart social drafts" Google Doc for Carlo's approval.

Never auto-posts to social media.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .models import JobStatus, VerificationEvidence, VerificationResult, WorkerResult
from . import staff_jobs_common as common

ACTION_TYPE = "meeting.synthesis.weekly"
TASK_NAME = "StreetSmart weekly meeting synthesis"
WORKER_NAME = "meeting-synthesis"

SENDER = "robie@streetsmart.insurance"
RECIPIENT = "carlo@streetsmart.insurance"
SOCIAL_DOC_NAME = "StreetSmart social drafts"
TIMEZONE = "America/New_York"

# Department keyword mapping. Carlo can correct this; matching is
# case-insensitive substring on the meeting title (without the Gemini suffix).
DEPT_KEYWORDS: dict[str, list[str]] = {
    "Commercial Lines": ["cl team", "cl sales", "cl renewal", "commercial lines"],
    "Personal Lines": ["pl huddle", "daily pl", "personal lines", "pl team"],
    "Trucking & Transportation": ["truck"],
    "Operations": ["operations", " ops ", "ops meeting", "ops sync"],
    "Accounting": ["accounting"],
    "Certificates": ["certificates", "cert meeting"],
}
# Anything unmatched (1:1s, AM syncs, meetings with Carlo, ...) lands here.
OTHER_BUCKET = "General / 1:1s"

GEMINI_SUFFIX_RE = re.compile(r"\s*-\s*notes by gemini\s*$", re.IGNORECASE)
# "Meeting Title - 2026/09/24 15:32 EDT - Notes by Gemini"
TITLE_TS_RE = re.compile(
    r"^(?P<title>.*?)\s*-\s*(?P<ts>\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}\s+[A-Z]{2,5})\s*-\s*notes by gemini\s*$",
    re.IGNORECASE,
)


def strip_suffix(name: str) -> str:
    return GEMINI_SUFFIX_RE.sub("", name).strip()


def dedupe_key(name: str) -> str:
    """Normalized (title, timestamp) so duplicate saves of one meeting collapse."""
    m = TITLE_TS_RE.match(name.strip())
    if m:
        title = re.sub(r"\s+", " ", m.group("title")).strip().lower()
        return f"{title}|{m.group('ts').strip()}"
    return re.sub(r"\s+", " ", strip_suffix(name)).strip().lower()


def dedupe_notes(notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for note in notes:
        key = dedupe_key(str(note.get("name", "")))
        if key not in seen:
            seen[key] = note
    return list(seen.values())


def classify_department(name: str) -> str:
    title = f" {strip_suffix(name).lower()} "
    for dept, keywords in DEPT_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in title:
                return dept
    return OTHER_BUCKET


def doc_text(doc: dict[str, Any]) -> str:
    parts: list[str] = []
    for el in doc.get("body", {}).get("content", []):
        para = el.get("paragraph")
        if not para:
            continue
        for pe in para.get("elements", []):
            tr = pe.get("textRun")
            if tr:
                parts.append(tr.get("content", ""))
    return "".join(parts).strip()


def fetch_notes_last_7_days(*, drive=None) -> list[dict[str, Any]]:
    """List Gemini note docs modified in the prior 7 days (deduplicated)."""
    drive = drive or common.drive_client()
    since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    query = f"name contains 'Notes by Gemini' and modifiedTime > '{since}' and trashed=false"
    notes: list[dict[str, Any]] = []
    page_token = None
    while True:
        resp = drive.files().list(
            q=query,
            fields="files(id,name,modifiedTime,mimeType,shortcutDetails/targetId),nextPageToken",
            pageSize=100,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            orderBy="modifiedTime desc",
        ).execute()
        notes.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    # Resolve Drive shortcuts to their target documents.
    for note in notes:
        if note.get("mimeType") == "application/vnd.google-apps.shortcut":
            target = (note.get("shortcutDetails") or {}).get("targetId")
            if target:
                note["id"] = target
    return dedupe_notes(notes)


def read_note_body(doc_id: str, *, docs=None) -> str:
    docs = docs or common.docs_client()
    doc = docs.documents().get(documentId=doc_id).execute()
    return doc_text(doc)


SYNTH_PROMPT = """You are summarizing one department's week at StreetSmart, an insurance agency.
Write in plain English a non-technical reader can understand. Be concise.

For the department below, read the meeting notes and produce:
1. Overall sentiment: one short paragraph on how the team seems (morale, energy).
2. Wins: bullet list of concrete wins (deals closed, problems solved, good client outcomes). Include names and specifics where the notes have them.
3. Struggles / what needs fixing: bullet list of blockers, recurring problems, or things the team is stuck on.

Rules:
- Only use facts from the notes. Do not invent names, numbers, or events.
- Skip anything that is clearly personal or sensitive (health, HR issues).
- If the notes are thin, say so instead of padding.

DEPARTMENT: {dept}

MEETING NOTES:
{notes}
"""


def synthesize_department(dept: str, notes_text: str) -> str:
    return common.gemini_generate(SYNTH_PROMPT.format(dept=dept, notes=notes_text[:12000]))


SOCIAL_PROMPT = """You are writing social media post drafts for StreetSmart, an insurance agency.
Based on the team wins below, draft {n} short social media posts (each under 280 characters).
Rules:
- Plain English, warm, human. No insurance jargon.
- Celebrate the people involved by first name only.
- Never invent facts, names, or numbers not in the wins.
- Number each draft. Do not add hashtags unless one fits naturally.

WINS:
{wins}
"""


def draft_social_posts(wins_text: str, n: int = 3) -> str:
    return common.gemini_generate(SOCIAL_PROMPT.format(n=n, wins=wins_text[:6000]))


def build_email_body(week_label: str, sections: dict[str, str], social_drafts: str) -> str:
    lines = [
        f"StreetSmart weekly meeting synthesis — {week_label}",
        "",
    ]
    for dept, text in sections.items():
        lines += [f"=== {dept} ===", text, ""]
    lines += [
        "=== Social post drafts (from this week's wins) ===",
        "These are saved in the 'StreetSmart social drafts' Google Doc for your approval. Nothing was posted.",
        "",
        social_drafts,
        "",
        "— Robie (StreetSmart automation)",
    ]
    return "\n".join(lines)


def find_or_create_social_doc(*, drive=None, docs=None) -> str:
    drive = drive or common.drive_client()
    docs = docs or common.docs_client()
    resp = drive.files().list(
        q=f"name = '{SOCIAL_DOC_NAME}' and trashed=false",
        fields="files(id)",
        pageSize=5,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = resp.get("files", [])
    if files:
        return files[0]["id"]
    created = docs.documents().create(body={"title": SOCIAL_DOC_NAME}).execute()
    return created["documentId"]


def append_to_doc(doc_id: str, text: str, *, docs=None) -> None:
    docs = docs or common.docs_client()
    docs.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [{"insertText": {"endOfSegmentLocation": {}, "text": text}}]},
    ).execute()


class MeetingSynthesisWorker:
    def perform(self, job: dict[str, Any], *, idempotency_key: str) -> WorkerResult:
        try:
            return self._perform(job)
        except Exception as exc:
            return WorkerResult(
                False, ACTION_TYPE, {"task": TASK_NAME},
                retryable=True,
                error=f"Meeting synthesis failed: {type(exc).__name__}: {exc}",
            )

    def _perform(self, job: dict[str, Any]) -> WorkerResult:
        tz = ZoneInfo(TIMEZONE)
        today = datetime.now(tz).date()
        week_label = f"week of {(today - timedelta(days=7)).strftime('%b %-d')} – {today.strftime('%b %-d, %Y')}"

        notes = fetch_notes_last_7_days()
        by_dept: dict[str, list[str]] = {}
        skipped_empty = 0
        for note in notes:
            name = str(note.get("name", ""))
            body = read_note_body(note["id"])
            if len(body) < 40:
                skipped_empty += 1
                continue
            dept = classify_department(name)
            by_dept.setdefault(dept, []).append(f"--- {strip_suffix(name)} ---\n{body[:4000]}")

        sections: dict[str, str] = {}
        all_wins: list[str] = []
        for dept in sorted(by_dept):
            combined = "\n\n".join(by_dept[dept])
            sections[dept] = synthesize_department(dept, combined)
            all_wins.append(f"[{dept}]\n{sections[dept]}")

        social_drafts = draft_social_posts("\n\n".join(all_wins)) if all_wins else "No wins found this week."
        email_body = build_email_body(week_label, sections, social_drafts)
        subject = f"Weekly meeting synthesis — {week_label}"
        message_id = common.send_gmail(
            sender=SENDER, to=[RECIPIENT], subject=subject, body=email_body
        )

        doc_id = find_or_create_social_doc()
        append_to_doc(
            doc_id,
            f"\n\n===== {week_label} =====\n{social_drafts}\n",
        )

        return WorkerResult(
            True, ACTION_TYPE,
            {"email_message_id": message_id, "social_doc_id": doc_id},
            detail={
                "week": week_label,
                "notes_found": len(notes),
                "notes_skipped_empty": skipped_empty,
                "departments": sorted(sections),
                "subject": subject,
            },
            retryable=True,
        )


class MeetingSynthesisVerifier:
    def verify(self, job: dict[str, Any], action: dict[str, Any]) -> VerificationResult:
        destination = action.get("destination") or {}
        message_id = str(destination.get("email_message_id") or "")
        doc_id = str(destination.get("social_doc_id") or "")
        expected = {"email_message_id": message_id, "social_doc_id": doc_id}
        captured = datetime.now(timezone.utc).isoformat()

        if not message_id:
            return VerificationResult(
                False,
                VerificationEvidence(
                    method="gmail_readback", source="gmail.users.messages.get",
                    expected=expected, observed={"email": "missing message id"},
                    authoritative=True, captured_at=captured, locator=None,
                ),
                retryable=False, error="No email message ID recorded",
            )
        email_ok = common.gmail_message_exists(message_id, sender=SENDER)
        doc_ok = False
        if doc_id:
            try:
                meta = common.drive_client().files().get(
                    fileId=doc_id, fields="id,name", supportsAllDrives=True
                ).execute()
                doc_ok = meta.get("id") == doc_id
            except Exception:
                doc_ok = False
        observed = {"email_from_robie": email_ok, "social_doc_exists": doc_ok}
        verified = bool(email_ok and doc_ok)
        return VerificationResult(
            verified,
            VerificationEvidence(
                method="gmail_and_drive_readback",
                source="gmail.users.messages.get + drive.files.get",
                expected=expected, observed=observed,
                authoritative=True, captured_at=captured,
                locator=f"https://mail.google.com/mail/#all/{message_id}",
            ),
            retryable=not verified,
            error=None if verified else f"Verification failed: {observed}",
            hold_status=None if verified else JobStatus.FAILED,
        )
