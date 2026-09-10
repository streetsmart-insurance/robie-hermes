"""
EZLynx Discussion Poster & Audit Logger for Robie Lead Sequences.

Ensures every call transcript, voicemail notification, warm-transfer event,
and stopping trigger is logged to the client's EZLynx file with the mandatory
'ROBIE was here' signature.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("discussion_poster")

ROBIE_SIGNATURE = "\n\nROBIE was here"


class EZLynxDiscussionPoster:
    def __init__(self, ezlynx_api_client: Optional[Any] = None):
        self.client = ezlynx_api_client

    @classmethod
    def format_call_completion_note(
        cls,
        cadence_type: str,
        touch_number: int,
        phone: str,
        call_id: str,
        disposition: str,
        transcript: Optional[str] = None,
        recording_url: Optional[str] = None,
        summary: Optional[str] = None,
        transfer_outcome: Optional[str] = None,
    ) -> str:
        lines = [
            f"📞 ROBIE OUTBOUND CALL COMPLETED — Touch {touch_number} ({cadence_type})",
            f"Target Phone: {phone}",
            f"Call ID: {call_id}",
            f"Outcome: {disposition.upper()}",
        ]
        if transfer_outcome:
            lines.append(f"Transfer: {transfer_outcome}")
        if summary:
            lines.append(f"\nExecutive Summary:\n{summary.strip()}")
        if recording_url:
            lines.append(f"\nAudio Recording: {recording_url}")
        if transcript:
            snippet = transcript.strip()
            if len(snippet) > 3000:
                snippet = snippet[:3000] + " ... [transcript truncated]"
            lines.append(f"\nTranscript:\n{snippet}")

        note = "\n".join(lines) + ROBIE_SIGNATURE
        return note

    def post_note(
        self,
        applicant_id: str,
        note_text: str,
        discussion_title: Optional[str] = None,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        title = discussion_title or "Robie Autonomous Lead Outreach"
        if dry_run:
            logger.info(
                "[DRY RUN] Would post note to EZLynx Applicant %s (Title: '%s'):\n%s",
                applicant_id,
                title,
                note_text,
            )
            return {"success": True, "mode": "DRY_RUN", "applicant_id": applicant_id}

        if self.client:
            try:
                res = self.client.add_note_to_discussion(
                    applicant_id=applicant_id,
                    discussion_title=title,
                    note_text=note_text,
                )
                return {"success": True, "mode": "LIVE", "result": res}
            except Exception as e:
                logger.error("Failed to post note to EZLynx: %s", e)
                return {"success": False, "error": str(e)}

        return {"success": False, "error": "NO_CLIENT_CONFIGURED"}
