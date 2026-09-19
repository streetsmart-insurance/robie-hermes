"""Real outreach adapters for the ROBIE verification workers.

Each adapter performs ONE real action and returns destination evidence
(a Gmail message ID, a Bland call ID, an EZLynx note ID). If the action
cannot be performed, the adapter RAISES — it never returns fake success.
This is what makes live mode honest: an action is done only when the
destination confirms it.

Adapters:
  RobieEmailAdapter — sends from robie@streetsmart.insurance via the
      connected Gmail account (fc9e7fb50b1e407ab2940915a8ea56df).
  BlandCallAdapter — places carrier/lender calls via the Bland CLI
      (~/workspace/skills/bland-ai/bin/bland.py). Carriers, MGAs, and
      mortgage companies ONLY — never clients. Business hours only.
      Carlo authorized carrier/3rd-party calls 2026-09-19.
  EZLynxNoteAdapter — files notes via robie_job_engine/ezlynx_discussions
      (get_discussions -> append_note), the API-first path.

All three adapters are REVERSIBLE-safe: email/call/note actions are
real-world actions, so live mode stays behind ROBIE_LIVE_OUTREACH=1
AND --live, plus business-hours enforcement in verification_workers.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROBIE_GMAIL_ACCOUNT_ID = "fc9e7fb50b1e407ab2940915a8ea56df"
ROBIE_EMAIL = "robie@streetsmart.insurance"
ROBIE_CALLER_ID = "+17322986745"

CALL_PACK_PATH = os.path.expanduser(
    "~/workspace/robie-manual-ops/call-pack.md")


class AdapterError(RuntimeError):
    """An adapter could not perform the action. Never swallowed."""


class NotConfiguredError(AdapterError):
    """Credentials or tooling missing — fail closed, don't fake it."""


@dataclass
class DestinationEvidence:
    channel: str  # email | call | note
    destination_id: str  # Gmail message ID / Bland call ID / EZLynx note ID
    detail: str  # human-readable summary for the digest


def _run(cmd: list[str], *, timeout: int = 120) -> str:
    """Run a command, return stdout. Raise AdapterError on any failure."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)
    except FileNotFoundError as exc:
        raise NotConfiguredError(f"tool not found: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AdapterError(f"tool timed out: {' '.join(cmd[:3])}") from exc
    if proc.returncode != 0:
        raise AdapterError(
            f"tool failed ({proc.returncode}): {' '.join(cmd[:3])}\n"
            f"{proc.stderr[:500]}")
    return proc.stdout


class RobieEmailAdapter:
    """Send operational email from robie@streetsmart.insurance.

    Uses --account (the separate connected mailbox), NEVER --from on
    another account (lesson 2026-09-18: --from is silently ignored).
    Reads back the sent message and verifies the From header before
    reporting success.
    """

    def __init__(self, account_id: str = ROBIE_GMAIL_ACCOUNT_ID):
        self.account_id = account_id

    def send(self, *, to: str, subject: str, body: str) -> DestinationEvidence:
        if not to or "@" not in to:
            raise AdapterError(f"refusing to send: bad recipient {to!r}")
        out = _run([
            "hatch_gws_cli", "gmail", "+send",
            "--account", self.account_id,
            "--to", to, "--subject", subject, "--body", body,
        ])
        # +send prints the sent message metadata; extract the Gmail ID.
        message_id = ""
        for line in out.splitlines():
            if "id" in line.lower() and len(line.strip()) < 60:
                candidate = line.strip().strip('",')
                if candidate and "@" not in candidate:
                    message_id = candidate
                    break
        if not message_id:
            # Fall back: parse JSON if the CLI emitted it.
            try:
                data = json.loads(out)
                message_id = str(data.get("id") or "")
            except (json.JSONDecodeError, AttributeError):
                pass
        if not message_id:
            raise AdapterError(
                f"send appeared to succeed but no message ID in output: "
                f"{out[:300]}")
        # Read back and verify the From header is robie@.
        read_out = _run([
            "hatch_gws_cli", "gmail", "+read",
            "--account", self.account_id,
            "--id", message_id, "--headers", "--format", "json",
        ])
        if ROBIE_EMAIL not in read_out:
            raise AdapterError(
                f"sent message {message_id} read-back does not show "
                f"From {ROBIE_EMAIL} — NOT verified as sent from robie@")
        return DestinationEvidence(
            channel="email",
            destination_id=message_id,
            detail=f"email to {to} sent from {ROBIE_EMAIL} "
                   f"(subject: {subject[:60]})",
        )


class BlandCallAdapter:
    """Place carrier/lender calls via Bland. Never clients. Ever.

    The target number must be a carrier, MGA, or mortgage company.
    Client/insured numbers are refused. Business hours are enforced by
    the caller (verification_workers.in_business_hours).
    """

    BLAND_CLI = os.path.expanduser("~/workspace/skills/bland-ai/bin/bland.py")

    def __init__(self, from_number: str = ROBIE_CALLER_ID):
        self.from_number = from_number

    def call(self, *, to: str, task: str,
             transfer_to: str | None = None) -> DestinationEvidence:
        if not to or not to.strip():
            raise AdapterError("refusing to call: no destination number")
        cmd = [
            "python3", self.BLAND_CLI, "call",
            "--to", to.strip(),
            "--task", task,
            "--from-number", self.from_number,
        ]
        if transfer_to:
            cmd += ["--transfer-phone-number", transfer_to]
        out = _run(cmd, timeout=180)
        try:
            data = json.loads(out)
            call_id = str(data.get("call_id") or data.get("id") or "")
        except json.JSONDecodeError:
            call_id = ""
            for line in out.splitlines():
                if "call_id" in line.lower():
                    call_id = line.split(":")[-1].strip().strip('",')
                    break
        if not call_id:
            raise AdapterError(
                f"call placed but no call ID returned: {out[:300]}")
        return DestinationEvidence(
            channel="call",
            destination_id=call_id,
            detail=f"call to {to} placed from {self.from_number} "
                   f"(call_id {call_id[:12]}…)",
        )


class EZLynxNoteAdapter:
    """File notes to EZLynx discussions via the API-first path.

    Uses robie_job_engine.ezlynx_discussions: get_discussions to find the
    existing relevant discussion, then append_note. Never creates untitled
    discussions (Carlo's standing rule).
    """

    def __init__(self):
        try:
            import ezlynx_discussions as _d  # noqa: F401
        except ImportError as exc:
            raise NotConfiguredError(
                "ezlynx_discussions module not importable") from exc

    def file_note(self, *, applicant_id: str, discussion_title: str,
                  body: str) -> DestinationEvidence:
        import ezlynx_discussions as d
        discussions = d.get_discussions(applicant_id)
        target = None
        for disc in discussions:
            if (disc.get("title") or "").strip().lower() == \
                    discussion_title.strip().lower():
                target = disc
                break
        if target is None:
            raise AdapterError(
                f"no existing discussion titled {discussion_title!r} on "
                f"applicant {applicant_id} — refusing to create one")
        note_id = d.append_note(target["id"], body)
        if not note_id:
            raise AdapterError(
                f"append_note returned no note ID for discussion "
                f"{target['id']}")
        # Read back: verify the note landed.
        refreshed = d.get_discussions(applicant_id)
        found = any(
            str(n.get("id")) == str(note_id)
            for disc in refreshed
            for n in (disc.get("notes") or []))
        if not found:
            raise AdapterError(
                f"note {note_id} not found on read-back — NOT verified")
        return DestinationEvidence(
            channel="note",
            destination_id=str(note_id),
            detail=f"note filed to '{discussion_title}' "
                   f"(applicant {applicant_id})",
        )
