"""Certificates chunk 3: Zapier client for certificate review tasks.

Fires the agency's EZLynx follow-up-task Zap through the Webhooks-by-Zapier
catch hook (see the zapier skill). Proof comes from the nonce-guarded
callback (see :mod:`robie_job_engine.cert_callback`): every fire mints a
``filing_id`` the Zap must echo back with EZLynx's own create-task
response. Until the validated callback arrives the filing stays
UNVERIFIED and re-drives HOLD instead of risking a duplicate fire.

Task open/closed lookup needs a lookup hook that does not exist yet.
Until Carlo adds it, ``get_task_state`` returns ``unknown`` and the
registry's last-known state rules (fail-closed toward REUSE, never CREATE).

Nothing here invents a hook path: a missing ``~/.config/zapier/hook_path``
(or the skill script) raises instead of firing into the void.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

#: Steffany Canales' EZLynx login — the certificates reviewer.
ASSIGNEE_SCANALES = "SCanales"

TASK_STATE_OPEN = "open"
TASK_STATE_CLOSED = "closed"
TASK_STATE_UNKNOWN = "unknown"


@dataclass
class ZapResult:
    fired: bool = False
    reason: str = ""
    stdout: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    #: Unique per-fire nonce. The Zap's callback step must echo it back so
    #: the worker can tie the callback to THIS fire (see cert_callback).
    filing_id: str = ""


def certificate_due_date(days: int = 3) -> str:
    """ISO due date for a certificate request. Carlo's rule: always a date."""
    return (date.today() + timedelta(days=days)).isoformat()


class CertZapierClient:
    """Thin wrapper around the zapier skill's ``bin/zap-trigger``."""

    def __init__(self, trigger_script: str = "",
                 assignee: str = ASSIGNEE_SCANALES,
                 dry_run: bool = False) -> None:
        self.trigger_script = (
            trigger_script
            or os.path.expanduser("~/workspace/skills/zapier/bin/zap-trigger"))
        self.assignee = assignee
        self.dry_run = dry_run

    def _fire(self, payload: dict[str, Any],
              applicant_verified: bool) -> ZapResult:
        if not os.path.isfile(self.trigger_script):
            raise RuntimeError(
                f"zap-trigger not found at {self.trigger_script} — refusing "
                "to fire blind")
        if not applicant_verified:
            raise RuntimeError(
                "applicant_id is not independently verified — refusing to fire")
        cmd = [self.trigger_script, "--payload", json.dumps(payload)]
        if self.dry_run:
            cmd.append("--dry-run")
        if payload.get("note_text"):
            cmd += ["--note-text", payload["note_text"]]
        cmd += ["--applicant-verified"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return ZapResult(
            fired=(proc.returncode == 0 and not self.dry_run),
            reason="dry-run" if self.dry_run else f"exit={proc.returncode}",
            stdout=(proc.stdout + proc.stderr)[-2000:],
            payload=payload,
        )

    def create_task(self, *, applicant_id: int, title: str,
                    email_subject: str, note_text: str,
                    due_date: str = "") -> ZapResult:
        """Fire the EZLynx follow-up-task Zap for a new certificate request.

        Mints a ``filing_id`` nonce and includes it in the payload: the
        Zap's callback step must echo it back (see
        :mod:`robie_job_engine.cert_callback`). The created task's ID comes
        back via that callback; this call alone never invents one.
        """
        from .cert_callback import new_filing_id

        filing_id = new_filing_id()
        payload = {
            "applicant_id": str(applicant_id),
            "task_title": title,
            "assignee": self.assignee,
            "source": "certificates-intake",
            "email_subject": email_subject,
            "due_date": due_date or certificate_due_date(),
            "note_text": note_text,
            "filing_id": filing_id,
        }
        result = self._fire(payload, applicant_verified=True)
        result.filing_id = filing_id
        # Delayed verification (2026-09-27): when the Zap actually fires, queue
        # the expected task for the verifier. Creation is NOT delayed — only
        # the completion-status check waits. Never let this break the firing.
        if result.fired:
            try:
                from .task_verifier import TaskVerificationStore

                TaskVerificationStore().record_pending(
                    producer="certificates",
                    applicant_id=str(applicant_id),
                    title=title,
                    assignee=self.assignee,
                )
            except Exception as exc:  # noqa: BLE001 — verification is best-effort here
                import logging
                logging.getLogger(__name__).warning(
                    "task_verifier record_pending failed: %s", exc
                )
        return result

    def reopen_task(self, *, task_id: str, applicant_id: int, title: str,
                    note_text: str) -> ZapResult:
        """Ask the Zap to reopen a closed task. Separate hook when Carlo
        adds it; until then this raises instead of pretending."""
        raise RuntimeError(
            "no Zapier reopen hook is configured — holding for human instead "
            "of inventing a reopen path")

    def get_task_state(self, task_id: str) -> str:
        """Open/closed lookup for a task the worker created.

        Needs the lookup hook (not yet built). Returns unknown until then —
        callers must fail closed on unknown.
        """
        return TASK_STATE_UNKNOWN

    @staticmethod
    def record_task_callback(registry: Any, applicant_id: int,
                             policy_key: str, holder_key: str,
                             task_id: str) -> None:
        """Zap callback webhook handler: store the created task ID."""
        entry = registry.get(applicant_id, policy_key, holder_key)
        if entry is None:
            from .cert_task_registry import TaskEntry, TASK_OPEN
            entry = TaskEntry(applicant_id=applicant_id,
                              policy_key=policy_key, holder_key=holder_key,
                              task_status=TASK_OPEN)
        entry.task_id = task_id
        entry.task_status = "open"
        registry.put(entry)
