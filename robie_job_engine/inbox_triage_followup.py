#!/usr/bin/env python3
"""Inbox triage follow-up enrichment ("inbox triage follow-up").

Turns missed emails and missed calls found by the inbox triage into
*proposed* EZLynx follow-up tasks. The pipeline is approval-gated:

    triage -> enrich (this module) -> pending proposals -> Carlo approves
    the exact task contents -> fire via the agency's Zapier catch hook

Nothing in this module fires a Zap on its own. `preview_fire` only runs
`zap-trigger --dry-run`; a live POST requires an explicit approval record
plus ITFU_ALLOW_LIVE=1 in the environment, and is never exercised in the
trial.

Approved 2026-09-22 (Carlo): build the whole enrichment, dry-run only.
"""

import csv
import json
import os
import re
import subprocess
import sys
from datetime import date, timedelta

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Placeholder/sentinel applicant IDs. Mirrors zap-trigger's
# PLACEHOLDER_APPLICANT_IDS -- keep the two lists in sync.
PLACEHOLDER_APPLICANT_IDS = frozenset({"199000001"})

# Display name -> EZLynx login username. The Zap's Task Assignee field is
# free text and EZLynx rejects anything it cannot resolve as a login
# username (verified 2026-09-15: "Can't create note as assignee not found").
# Names not in this map resolve to None and the proposal is flagged for
# human assignee resolution instead of guessing.
PRODUCER_USERNAMES = {
    "Carlo Ferrara": "Carlo1",
}

# Sonant summaries that describe a call with no caller request at all --
# a follow-up task for these would be pure noise.
NO_REQUEST_PATTERNS = [
    "no caller response or request was made",
    "no service need",
    "no follow-up action was captured",
]

# Words that must never appear in a task body. Task bodies go to EZLynx
# where non-technical agency staff read them (Carlo's standing rule:
# plain English, extremely concise).
JARGON_BLOCKLIST = [
    "ACORD", "API", "webhook", "payload", "idempotent", "JSON",
    "dedupe", "cron", "ETL",
]

MAX_SUMMARY_CHARS = 800  # hard cap on any task body

# Sonant -> task-body mapping: which parsed Sonant fields build the body.
SONANT_BODY_TEMPLATE = (
    "Missed call on {call_date}.\n"
    "{caller_line}\n"
    "{summary_line}"
    "{next_steps_line}"
    "Callback number: {phone}."
)


# ---------------------------------------------------------------------------
# LLM contract: email summarization
# ---------------------------------------------------------------------------

EMAIL_SUMMARY_PROMPT = """You are writing a follow-up task for insurance agency staff. \
Read the email below and write a task summary in extremely concise plain English \
that a non-technical person can understand. No jargon. No insurance acronyms \
unless they appear in the email itself.

Rules:
- 3 to 6 short lines, at most 120 words total.
- Line 1: who wrote and what they want, in one sentence.
- Then: callback phone number if one is given (write "Callback:" and the number).
- Then: any policy number, account name, or deadline mentioned, each on its own line.
- If the email asks for something to be done (cancel, quote, call back, sign), say exactly what.
- Never invent facts. If something is unclear, write "Unclear:" and quote the unclear part.

Email to summarize:
---
{email_body}
---
Task summary:"""


def summarize_email_with_llm(email_body):
    """Placeholder for the LLM step.

    In the daily triage run this step is executed by the triage agent using
    EMAIL_SUMMARY_PROMPT verbatim; this function documents the contract and
    validates the returned summary. It never calls a model itself.
    """
    raise NotImplementedError(
        "Run EMAIL_SUMMARY_PROMPT through the triage agent and pass the "
        "result to validate_summary()."
    )


def validate_summary(summary):
    """Fail-closed validation for a task body (LLM-written or Sonant-based).

    Returns (ok, reason).
    """
    if not isinstance(summary, str) or not summary.strip():
        return False, "empty summary"
    s = summary.strip()
    if len(s) > MAX_SUMMARY_CHARS:
        return False, "summary too long: %d chars (max %d)" % (len(s), MAX_SUMMARY_CHARS)
    lowered = s.lower()
    for word in JARGON_BLOCKLIST:
        if word.lower() in lowered:
            return False, "summary contains jargon: %r" % word
    lines = [l for l in s.splitlines() if l.strip()]
    if len(lines) < 2:
        return False, "summary too short: needs at least 2 lines"
    return True, ""


# ---------------------------------------------------------------------------
# Sonant call-analysis parsing
# ---------------------------------------------------------------------------

def html_to_text(body_html):
    """Convert a Sonant call-analysis email body (HTML-only) to plain text."""
    t = re.sub(r"<style.*?</style>", "", body_html, flags=re.S | re.I)
    t = re.sub(r"<script.*?</script>", "", t, flags=re.S | re.I)
    t = re.sub(r"<br[^>]*>", "\n", t, flags=re.I)
    t = re.sub(r"</(p|div|tr|table|h[1-6]|li)>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    try:
        import html as _html
        t = _html.unescape(t)
    except Exception:
        pass
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = "\n".join(line.strip() for line in t.splitlines())
    t = re.sub(r"\n{2,}", "\n", t)
    return t.strip()


def _field(text, label):
    """Extract the value following a 'Label' line in parsed Sonant text."""
    m = re.search(r"^%s\n(.+?)(?:\n[A-Z][A-Za-z /#&'-]*\n|\Z)" % re.escape(label),
                  text, re.M | re.S)
    # Fallback: single line right after the label.
    if not m:
        m = re.search(r"^%s\n([^\n]+)" % re.escape(label), text, re.M)
    return m.group(1).strip() if m else ""


def parse_sonant_text(text):
    """Parse plain-text Sonant call analysis into structured fields.

    Returns a dict with keys: call_date, direction, client_phone,
    ams_account, account_name, producer, csr, summary, call_class,
    call_type, call_outcome, next_steps, actionable (bool), reason.
    Missing contact details -> actionable False (nothing to follow up).
    """
    out = {
        "call_date": _field(text, "Date, Time"),
        "direction": _field(text, "Direction"),
        "client_phone": _field(text, "Client Phone"),
        "ams_account": _field(text, "AMS Account #"),
        "account_name": _field(text, "Account Name"),
        "producer": _field(text, "Producer"),
        "csr": _field(text, "CSR"),
        "summary": _field(text, "Summary"),
        "call_class": _field(text, "Class Of Call"),
        "call_type": _field(text, "Type Of Call"),
        "call_outcome": _field(text, "Call Outcome"),
        "next_steps": _field(text, "Next Step Actions"),
    }
    phone = normalize_phone(out["client_phone"])
    summary_lower = out["summary"].lower()
    if not phone:
        out["actionable"] = False
        out["reason"] = "no caller phone in Sonant email; nothing to follow up"
    elif not out["summary"]:
        out["actionable"] = False
        out["reason"] = "no call summary in Sonant email"
    elif any(p in summary_lower for p in NO_REQUEST_PATTERNS):
        out["actionable"] = False
        out["reason"] = "caller made no request; follow-up would be noise"
    else:
        out["actionable"] = True
        out["reason"] = ""
    out["client_phone_normalized"] = phone
    return out


def sonant_task_body(parsed):
    """Build the EZLynx task body from parsed Sonant fields (no LLM)."""
    caller = parsed.get("account_name") or "Unknown caller"
    caller_line = "Caller: %s." % caller
    if parsed.get("producer"):
        caller_line += " Producer on file: %s." % parsed["producer"]
    summary_line = "What happened: %s\n" % parsed["summary"] if parsed.get("summary") else ""
    next_steps_line = ""
    if parsed.get("next_steps"):
        next_steps_line = "Next steps from call: %s.\n" % parsed["next_steps"]
    body = SONANT_BODY_TEMPLATE.format(
        call_date=parsed.get("call_date", "unknown date"),
        caller_line=caller_line,
        summary_line=summary_line,
        next_steps_line=next_steps_line,
        phone=parsed.get("client_phone_normalized", ""),
    )
    ok, reason = validate_summary(body)
    if not ok:
        raise ValueError("built Sonant task body failed validation: %s" % reason)
    return body


# ---------------------------------------------------------------------------
# Phone -> EZLynx applicant matching
# ---------------------------------------------------------------------------

def normalize_phone(raw):
    """Normalize a phone number to 10 digits; '' when unusable."""
    if not raw:
        return ""
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


def match_phone_to_applicant(phone, directory_csv):
    """Match a normalized 10-digit phone against the applicant directory.

    directory_csv: the Daily Active Client Export parse (applicant_id,
    account_name, ..., phone_cell, phone_home, phone_work, ...).
    Returns the matching row dict, or None. Never guesses.
    """
    phone = normalize_phone(phone)
    if not phone:
        return None
    with open(directory_csv, newline="") as f:
        for row in csv.DictReader(f):
            for col in ("phone_cell", "phone_home", "phone_work"):
                if normalize_phone(row.get(col)) == phone:
                    return row
    return None


# ---------------------------------------------------------------------------
# Task payload assembly (Zapier catch-hook payload)
# ---------------------------------------------------------------------------

def default_due_date(from_day=None):
    """Next business day, ISO YYYY-MM-DD (Carlo's rule: always a due date)."""
    d = from_day or date.today()
    d += timedelta(days=1)
    while d.weekday() >= 5:  # Sat/Sun -> Monday
        d += timedelta(days=1)
    return d.isoformat()


def assemble_task_payload(applicant_id, task_title, task_body, assignee,
                          email_subject, due_date, source="inbox-triage"):
    """Assemble the Zapier catch-hook payload. Fail-closed validation.

    Keys: applicant_id, task_title, task_notes, assignee, source,
    email_subject, due_date.

    NOTE: `task_notes` is a NEW field the EZLynx follow-up-task Zap does not
    read today. The Zap's Create-Task step must be updated to map it before
    go-live (Carlo's approval to edit the Zap).
    """
    if not task_title or not str(task_title).strip():
        return None, "missing task_title"
    if not due_date or not re.match(r"^\d{4}-\d{2}-\d{2}$", str(due_date)):
        return None, "due_date is required in ISO YYYY-MM-DD (Carlo's standing rule)"
    ok, reason = validate_summary(task_body)
    if not ok:
        return None, "task_body invalid: %s" % reason
    raw = "" if applicant_id is None else str(applicant_id).strip()
    if not raw:
        return None, "missing applicant_id"
    if not raw.isdigit():
        return None, "applicant_id %r is not numeric" % raw
    if raw in PLACEHOLDER_APPLICANT_IDS:
        return None, ("applicant_id %r is a known placeholder/sentinel "
                      "(poisoned five Zap runs on 2026-09-20)" % raw)
    payload = {
        "applicant_id": raw,
        "task_title": str(task_title).strip(),
        "task_notes": task_body.strip(),
        "assignee": assignee,
        "source": source,
        "email_subject": email_subject,
        "due_date": due_date,
    }
    return payload, ""


# ---------------------------------------------------------------------------
# Approval gate: pending proposals, propose-then-fire
# ---------------------------------------------------------------------------

def write_pending_proposals(path, items):
    """Write proposed follow-up tasks to a pending file. Statuses: pending."""
    for it in items:
        it.setdefault("status", "pending")
    with open(path, "w") as f:
        json.dump(items, f, indent=1)
    return path


def load_pending(path):
    with open(path) as f:
        return json.load(f)


def render_proposal(item):
    """Human-readable proposal text for Carlo's approval."""
    return (
        "PROPOSED FOLLOW-UP TASK\n"
        "  task_id:      %(task_id)s\n"
        "  source:       %(source)s (gmail %(gmail_id)s)\n"
        "  applicant_id: %(applicant_id)s\n"
        "  title:        %(task_title)s\n"
        "  assignee:     %(assignee)s\n"
        "  due_date:     %(due_date)s\n"
        "  email:        %(email_subject)s\n"
        "  body:\n%(task_notes)s\n"
        "  status:       %(status)s\n"
    ) % {k: item.get(k, "") for k in
         ("task_id", "source", "gmail_id", "applicant_id", "task_title",
          "assignee", "due_date", "email_subject", "task_notes", "status")}


def preview_fire(pending_path, task_id, approved_by, zap_trigger=None):
    """Approval gate for firing a pending follow-up task.

    Refuses unless:
      - the task exists in the pending file with status "pending"
      - approved_by names the person who approved the EXACT task contents
    On approval it runs `zap-trigger --dry-run` (validation only) and
    returns the exact shell command that would fire the task live.
    It NEVER performs the live POST itself; live firing is a separate,
    explicitly-approved manual step.
    """
    items = load_pending(pending_path)
    item = next((i for i in items if i.get("task_id") == task_id), None)
    if item is None:
        return {"ok": False, "error": "task_id %r not found in %s" % (task_id, pending_path)}
    if item.get("status") != "pending":
        return {"ok": False, "error": "task %r status is %r, not pending" % (task_id, item.get("status"))}
    if not approved_by or not str(approved_by).strip():
        return {"ok": False, "error": "approval required: Carlo must approve the exact task contents before firing"}
    payload = {
        "applicant_id": item["applicant_id"],
        "task_title": item["task_title"],
        "task_notes": item["task_notes"],
        "assignee": item["assignee"],
        "source": item.get("source", "inbox-triage"),
        "email_subject": item.get("email_subject", ""),
        "due_date": item["due_date"],
    }
    zap_trigger = zap_trigger or os.path.expanduser("~/workspace/skills/zapier/bin/zap-trigger")
    # --dry-run also enforces zap-trigger's own applicant gate, so the
    # preview carries --applicant-verified; it still only validates.
    cmd = [sys.executable, zap_trigger, "--payload", json.dumps(payload),
           "--dry-run", "--applicant-verified"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    try:
        result = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:
        result = {"ok": False, "error": "zap-trigger output unparseable: %s" % proc.stdout[:200]}
    live_cmd = " ".join([sys.executable, zap_trigger,
                         "--payload", "'%s'" % json.dumps(payload),
                         "--applicant-verified"])
    return {"ok": bool(result.get("ok")), "approved_by": approved_by,
            "dry_run": result, "live_command": live_cmd,
            "note": "LIVE FIRE NOT PERFORMED. Run live_command only with Carlo's "
                    "explicit approval of this exact payload."}


# ---------------------------------------------------------------------------
# CLI (dry-run / demo)
# ---------------------------------------------------------------------------

def main(argv):
    if len(argv) < 2 or argv[1] not in ("parse-sonant", "match-phone"):
        print("usage: inbox_triage_followup.py parse-sonant <html-file> | match-phone <phone> <directory-csv>")
        return 2
    if argv[1] == "parse-sonant":
        parsed = parse_sonant_text(html_to_text(open(argv[2]).read()))
        print(json.dumps(parsed, indent=1))
        if parsed["actionable"]:
            print("\n--- task body ---\n" + sonant_task_body(parsed))
        return 0
    row = match_phone_to_applicant(argv[2], argv[3])
    print(json.dumps(row, indent=1) if row else "NO MATCH")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
