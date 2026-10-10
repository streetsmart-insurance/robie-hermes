"""EZLynx label poller — detect labeled notes and fire the Bland webhook.

Replaces the 11 Zapier Zaps, which cannot work: EZLynx's "New Note"
Zapier trigger delivers an empty ``Labels`` field, so each Zap's Code
step never matches and the Filter kills every run (proven 2026-10-03
with 11 labeled test discussions on Jake's account producing zero
webhook hits).

Poll loop (runs on a box with the persistent EZLynx browser session):

1. List discussions for each watched applicant via DiscussionApi (OAuth —
   this read path works).
2. For each discussion, fetch its notes.
3. For each unprocessed note, GET its org labels via the Portal API
   using session cookies from CDP. OAuth Bearer on
   ``Notes/{id}/OrganizationLabels`` is a proven HTTP 403
   (see :mod:`robie_job_engine.ezlynx_org_labels`); it is never attempted.
4. When a note carries one of the 11 Bland campaign labels, POST the
   trigger to the Bland webhook.
5. Record the note id as processed (caller-supplied store) so a re-poll
   never double-fires. Idempotency is on note id, not on label.

Fail-closed throughout: a note whose labels cannot be read is skipped,
never treated as a match; a webhook POST that fails raises and the note
stays unprocessed so the next poll retries it.

``urlopen`` and the CDP cookie loader are injectable for tests.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable
from urllib import error, parse, request

from .ezlynx_discussions import (
    DiscussionApiClient,
    DiscussionApiError,
    discussion_id_of,
    discussion_title_of,
)
from .ezlynx_org_labels import (
    NOTE_LABELS_PATH,
    label_name_of,
    normalize_org_label_rows,
)
from .ezlynx_portal_session import (
    format_cookie_header,
    is_ezlynx_cookie_domain,
    portal_session_headers,
)

# Label name -> Bland campaign id. Character-for-character; the Zap Code
# steps used the same exact strings.
BLAND_CAMPAIGN_LABELS: dict[str, str] = {
    "Robie Call": "robie-call",
    "Robie lead follow-up": "robie-lead-followup",
    "Robie client outreach": "robie-client-outreach",
    "Robie cancellation": "robie-cancellation",
    "Robie audit": "robie-audit",
    "Robie returned mail": "robie-returned-mail",
    "Robie e-sign": "robie-e-sign",
    "Robie additional info": "robie-additional-info",
    "Robie recommendations": "robie-recommendations",
    "Robie unresponsive": "robie-unresponsive",
    "Robie renewal reach-out": "robie-renewal-reachout",
}

DEFAULT_WEBHOOK_URL = (
    "https://zapier-bland-webhook-751771086524.us-east1.run.app/webhook"
)
DEFAULT_TIMEOUT_SECONDS = 30


class LabelPollerError(RuntimeError):
    """Poller failure. Messages never carry secrets or cookie values."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


POLL_OK = "ok"
LABEL_READ_FAILED = "LABEL_READ_FAILED"
WEBHOOK_FAILED = "WEBHOOK_FAILED"


def campaign_for_labels(label_rows: list[dict[str, Any]] | None) -> str | None:
    """Bland campaign id for the first matching label, or None.

    Comparison is character-for-character against BLAND_CAMPAIGN_LABELS.
    """
    for row in label_rows or []:
        if not isinstance(row, dict):
            continue
        campaign = BLAND_CAMPAIGN_LABELS.get(label_name_of(row))
        if campaign:
            return campaign
    return None


def note_id_of(note: dict[str, Any]) -> str:
    for key in ("noteId", "NoteId", "id", "Id"):
        value = str(note.get(key) or "").strip()
        if value:
            return value
    return ""


def _default_urlopen(url: str, *, data: bytes | None, headers: dict[str, str], timeout: int):
    req = request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    return request.urlopen(req, timeout=timeout)


class LabelPoller:
    """Polls EZLynx notes for Bland campaign labels and fires the webhook."""

    def __init__(
        self,
        discussion_client: DiscussionApiClient,
        *,
        portal_base_url: str,
        portal_origin: str,
        webhook_url: str = DEFAULT_WEBHOOK_URL,
        urlopen: Callable[..., Any] | None = None,
        load_cookies: Callable[[], list[dict[str, Any]]] | None = None,
        clock: Callable[[], float] | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._discussion_client = discussion_client
        self._portal_base = portal_base_url.rstrip("/") + "/"
        self._portal_origin = portal_origin
        self._webhook_url = webhook_url
        self._urlopen = urlopen or _default_urlopen
        self._load_cookies = load_cookies
        self._clock = clock or time.time
        self._timeout = timeout

    # -- label read (session cookies, never OAuth) ------------------------

    def _portal_headers(self) -> dict[str, str]:
        if self._load_cookies is None:
            raise LabelPollerError(
                LABEL_READ_FAILED,
                "no CDP cookie loader configured; label read is fail-closed",
            )
        cookies = self._load_cookies()
        header = format_cookie_header(cookies)
        if not header:
            raise LabelPollerError(
                LABEL_READ_FAILED,
                "CDP EZLynx session has no cookies; label read is fail-closed",
            )
        return portal_session_headers(header, self._portal_origin, cookies)

    def read_note_labels(self, note_id: str) -> list[dict[str, Any]]:
        """GET org labels on one note via Portal session cookies.

        Never sends OAuth Bearer (proven HTTP 403 on this endpoint).
        """
        note = str(note_id or "").strip()
        if not note:
            raise LabelPollerError(LABEL_READ_FAILED, "note id is required")
        headers = self._portal_headers()
        url = self._portal_base + NOTE_LABELS_PATH.format(
            note_id=parse.quote(note, safe="")
        ).lstrip("/")
        try:
            req = request.Request(url, data=None, headers=headers, method="GET")
            resp = self._urlopen(url, data=None, headers=headers, timeout=self._timeout)
            raw = resp.read()
        except error.HTTPError as exc:
            raise LabelPollerError(
                LABEL_READ_FAILED,
                f"Portal note-labels GET failed: HTTP {exc.code}",
            ) from exc
        except (error.URLError, TimeoutError, OSError) as exc:
            raise LabelPollerError(
                LABEL_READ_FAILED, "Portal note-labels GET transport failed"
            ) from exc
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise LabelPollerError(
                LABEL_READ_FAILED, "Portal note-labels returned non-JSON"
            ) from exc
        return normalize_org_label_rows(parsed)

    # -- webhook ----------------------------------------------------------

    def fire_webhook(
        self, *, applicant_id: str, note_id: str, label: str, campaign_id: str
    ) -> dict[str, Any]:
        """POST the trigger to the Bland webhook. Raises on failure.

        The note stays unprocessed when this raises, so the next poll
        retries it — no silent drops.
        """
        payload = {
            "applicant_id": str(applicant_id),
            "note_id": str(note_id),
            "label": str(label),
            "campaign_id": str(campaign_id),
            "source": "ezlynx-label-poller",
            "polled_at": self._clock(),
        }
        data = json.dumps(payload).encode("utf-8")
        try:
            resp = self._urlopen(
                self._webhook_url,
                data=data,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=self._timeout,
            )
            raw = resp.read()
        except error.HTTPError as exc:
            raise LabelPollerError(
                WEBHOOK_FAILED, f"webhook POST failed: HTTP {exc.code}"
            ) from exc
        except (error.URLError, TimeoutError, OSError) as exc:
            raise LabelPollerError(
                WEBHOOK_FAILED, "webhook POST transport failed"
            ) from exc
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = {"raw": raw.decode("utf-8", errors="replace")[:200]}
        return {"status": POLL_OK, "webhook_response": parsed, "payload": payload}

    # -- poll --------------------------------------------------------------

    def poll_applicant(
        self,
        applicant_id: str,
        *,
        is_processed: Callable[[str], bool],
        mark_processed: Callable[[str], None],
    ) -> dict[str, Any]:
        """One poll pass over an applicant's discussions.

        Returns a summary: discussions scanned, notes seen, labels matched,
        webhooks fired, and per-note outcomes. A note is marked processed
        only after its webhook POST succeeds; failures stay unprocessed
        for retry on the next poll.
        """
        applicant = str(applicant_id or "").strip()
        if not applicant:
            raise LabelPollerError(LABEL_READ_FAILED, "applicant id is required")
        summary: dict[str, Any] = {
            "status": POLL_OK,
            "applicant_id": applicant,
            "discussions_scanned": 0,
            "notes_seen": 0,
            "labels_matched": 0,
            "webhooks_fired": 0,
            "outcomes": [],
        }
        try:
            discussions = self._discussion_client.get_discussions(applicant)
        except DiscussionApiError as exc:
            summary["status"] = LABEL_READ_FAILED
            summary["error"] = str(exc)
            return summary

        for record in discussions or []:
            if not isinstance(record, dict):
                continue
            discussion_id = discussion_id_of(record)
            if not discussion_id:
                continue
            summary["discussions_scanned"] += 1
            try:
                detail = self._discussion_client.get_discussion(discussion_id)
            except DiscussionApiError as exc:
                summary["outcomes"].append(
                    {
                        "discussion_id": discussion_id,
                        "discussion_title": discussion_title_of(record),
                        "status": "discussion_read_failed",
                        "error": str(exc),
                    }
                )
                continue
            notes = detail.get("notes") or detail.get("Notes") or []
            if not isinstance(notes, list):
                notes = []
            for note in notes:
                if not isinstance(note, dict):
                    continue
                note_id = note_id_of(note)
                if not note_id:
                    continue
                summary["notes_seen"] += 1
                if is_processed(note_id):
                    continue
                try:
                    label_rows = self.read_note_labels(note_id)
                except LabelPollerError as exc:
                    # Fail-closed: unreadable labels are skipped, never matched.
                    summary["outcomes"].append(
                        {
                            "discussion_id": discussion_id,
                            "note_id": note_id,
                            "status": "label_read_failed",
                            "error": str(exc),
                        }
                    )
                    continue
                campaign_id = campaign_for_labels(label_rows)
                if not campaign_id:
                    mark_processed(note_id)
                    summary["outcomes"].append(
                        {
                            "discussion_id": discussion_id,
                            "note_id": note_id,
                            "status": "no_label_match",
                        }
                    )
                    continue
                matched_label = next(
                    (
                        label_name_of(row)
                        for row in label_rows
                        if BLAND_CAMPAIGN_LABELS.get(label_name_of(row)) == campaign_id
                    ),
                    "",
                )
                summary["labels_matched"] += 1
                try:
                    fired = self.fire_webhook(
                        applicant_id=applicant,
                        note_id=note_id,
                        label=matched_label,
                        campaign_id=campaign_id,
                    )
                except LabelPollerError as exc:
                    # Stays unprocessed: the next poll retries the webhook.
                    summary["outcomes"].append(
                        {
                            "discussion_id": discussion_id,
                            "note_id": note_id,
                            "status": "webhook_failed",
                            "label": matched_label,
                            "campaign_id": campaign_id,
                            "error": str(exc),
                        }
                    )
                    continue
                mark_processed(note_id)
                summary["webhooks_fired"] += 1
                summary["outcomes"].append(
                    {
                        "discussion_id": discussion_id,
                        "note_id": note_id,
                        "status": "fired",
                        "label": matched_label,
                        "campaign_id": campaign_id,
                        "webhook_response": fired["webhook_response"],
                    }
                )
        return summary
