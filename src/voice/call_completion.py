"""Post-call completion: fetch Bland artifacts and file them into EZLynx.

Used by ``/webhook/voice/call-completed``. Never applies a Robie Call label
(doom-loop guard from PR #8). Assigned CSR remains Carlo Ferrara.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from src.config import BASE_DIR
from src.database.models import PolicyRenewal, RenewalStatus
from src.database.session import SessionLocal
from src.email_outreach.gmail_client import GmailRenewalClient
from src.ezlynx.api_client import EZLynxApiClient
from src.voice.processed_robie_notes import ProcessedRobieCallStore
from src.voice.voice_client import CarrierVoiceClient

logger = logging.getLogger("voice_call_completion")

# EZLynx notes blow up if we dump a multi-hour transcript inline.
TRANSCRIPT_NOTE_CHAR_LIMIT = 6000

# Robie posts notes; the assigned CSR on the account stays Carlo.
DEFAULT_ASSIGNED_CSR_NAME = "Carlo Ferrara"
DEFAULT_ASSIGNED_CSR_EMAIL = "carlo@streetsmart.insurance"

# Never apply this org label on Robie's own completion / transcript posts.
ROBIE_CALL_LABEL = "Robie Call"
SAFE_DOCUMENT_LABEL = "Correspondence"

VOICE_DOWNLOADS_DIR = BASE_DIR / "data" / "downloads" / "voice"


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or value == "N/A"


def fetch_bland_call_details(
    call_id: Optional[str],
    voice_client: Optional[CarrierVoiceClient] = None,
) -> Dict[str, Any]:
    """GET Bland ``/v1/calls/{call_id}`` and unwrap the call object."""
    if not call_id or call_id == "UNKNOWN_CALL":
        return {}
    client = voice_client or CarrierVoiceClient()
    try:
        raw = client.get_call(call_id)
    except Exception as exc:
        logger.warning("Bland get_call failed for %s: %s", call_id, exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    inner = raw.get("call") if isinstance(raw.get("call"), dict) else raw
    return inner if isinstance(inner, dict) else {}


def merge_call_payload(
    webhook_data: Dict[str, Any],
    bland_details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Webhook fields win when present; Bland fills recording/transcript/summary."""
    bland = bland_details or {}
    merged = dict(webhook_data)
    for key in (
        "recording_url",
        "audio_url",
        "concatenated_transcript",
        "transcript",
        "summary",
        "call_summary",
        "metadata",
        "warm_transfer_call",
        "status",
        "disposition",
    ):
        if _is_blank(merged.get(key)) and not _is_blank(bland.get(key)):
            merged[key] = bland[key]
    if isinstance(bland.get("metadata"), dict):
        base_meta = dict(bland["metadata"])
        if isinstance(merged.get("metadata"), dict):
            base_meta.update(merged["metadata"])
        merged["metadata"] = base_meta
    return merged


def extract_transfer_outcome(data: Dict[str, Any]) -> Optional[str]:
    """Return a human line when Bland metadata/webhook exposes a transfer."""
    wtc = data.get("warm_transfer_call")
    if isinstance(wtc, dict) and wtc.get("state"):
        state = str(wtc.get("state")).upper()
        if state == "MERGED":
            return "Warm transfer completed (Bland state: MERGED)"
        return f"Warm transfer attempted (Bland state: {state})"

    status = str(data.get("status") or data.get("queue_status") or "").upper()
    if status == "TRANSFERRED":
        return "Call was transferred"

    disposition = data.get("disposition") or ""
    if "transfer" in str(disposition).lower():
        return f"Transfer disposition: {disposition}"

    analysis = data.get("analysis")
    if isinstance(analysis, dict):
        for key in ("transferred", "transfer_occurred", "warm_transfer"):
            value = analysis.get(key)
            if value in (True, "true", "MERGED", "transferred"):
                return "Transfer recorded in Bland analysis"

    transferred_to = data.get("transferred_to") or data.get("transfer_number")
    if transferred_to:
        return f"Transferred to {transferred_to}"
    return None


def truncate_transcript(transcript: str, limit: int = TRANSCRIPT_NOTE_CHAR_LIMIT) -> Tuple[str, bool]:
    """Return (text_for_note, was_truncated)."""
    text = transcript or ""
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip() + "\n\n[Transcript truncated — full text uploaded to EZLynx Documents]", True


def build_completion_note(
    *,
    policy_number: Optional[str],
    line_of_business: str,
    carrier_name: str,
    call_id: str,
    summary: str,
    recording_url: str,
    transcript: str,
    recording_uploaded: bool = False,
    transcript_attached: bool = False,
    transfer_line: str = "",
) -> str:
    """Structured EZLynx note: Policy header + Robie self-markers + transcript."""
    excerpt, truncated = truncate_transcript(transcript)
    upload_line = "yes" if recording_uploaded else "see URL (upload pending or failed)"
    attach_line = ""
    if transcript_attached or truncated:
        attach_line = "- Full transcript file: uploaded to EZLynx Documents\n"
    xfer = f"{transfer_line}\n" if transfer_line else ""
    note = (
        f"Policy: #{policy_number} ({line_of_business} - {carrier_name})\n"
        f"Autonomous Carrier Phone Outreach Completed:\n"
        f"- Result: Call finished successfully\n"
        f"- Call ID: {call_id}\n"
        f"- Summary: {summary}\n"
        f"- Audio Recording: {recording_url}\n"
        f"- Recording uploaded to EZLynx Documents: {upload_line}\n"
        f"{xfer}"
        f"{attach_line}"
        f"\nTranscript:\n{excerpt or '(no transcript returned)'}\n"
        f"\nRobie was here"
    )
    return note


def _guess_audio_suffix(recording_url: str) -> str:
    path = urlparse(recording_url or "").path.lower()
    for ext in (".mp3", ".wav", ".m4a", ".mpeg"):
        if path.endswith(ext):
            return ".mp3" if ext == ".mpeg" else ext
    return ".mp3"


def _safe_filename_part(value: Optional[str], fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "")).strip("_")
    return cleaned or fallback


def download_recording_file(
    recording_url: str,
    dest_path: Path,
    voice_client: Optional[CarrierVoiceClient] = None,
) -> bool:
    if _is_blank(recording_url) or recording_url == "N/A":
        return False
    client = voice_client or CarrierVoiceClient()
    try:
        return bool(client.download_recording(recording_url, dest_path))
    except Exception as exc:
        logger.warning("Recording download failed from %s: %s", recording_url, exc)
        return False


def upload_call_artifacts(
    *,
    ezlynx: EZLynxApiClient,
    applicant_id: str,
    policy_number: Optional[str],
    call_id: str,
    recording_url: str,
    transcript: str,
    voice_client: Optional[CarrierVoiceClient] = None,
    downloads_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Download the Bland recording and upload audio (+ full transcript if huge).

    Prefers the Classic document API; Playwright is fallback only.
    Never applies the Robie Call org label.
    """
    result: Dict[str, Any] = {
        "recording_uploaded": False,
        "transcript_uploaded": False,
        "recording_path": None,
        "transcript_path": None,
    }
    if not applicant_id:
        return result

    out_dir = Path(downloads_dir) if downloads_dir is not None else VOICE_DOWNLOADS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{_safe_filename_part(policy_number, 'policy')}_robie_call_{_safe_filename_part(call_id, 'call')}"

    if not _is_blank(recording_url) and recording_url != "N/A":
        rec_path = out_dir / f"{stem}{_guess_audio_suffix(recording_url)}"
        if download_recording_file(recording_url, rec_path, voice_client=voice_client):
            result["recording_path"] = str(rec_path)
            try:
                up = ezlynx.upload_document(
                    applicant_id=str(applicant_id),
                    file_path=rec_path,
                    folder_name="Documents",
                    description=f"{policy_number or 'Policy'} Robie Call Recording {call_id}",
                    policy_number=policy_number,
                    doc_type="correspondence",
                    label_to_apply=SAFE_DOCUMENT_LABEL,
                    use_playwright_fallback=True,
                    prefer_api=True,
                )
                if up.get("status") in ("success", "simulated"):
                    result["recording_uploaded"] = True
                    result["recording_upload"] = up
                    if up.get("applied_label") and ROBIE_CALL_LABEL.lower() in str(up.get("applied_label")).lower():
                        logger.error("Refusing to keep Robie Call label on completion recording upload")
                else:
                    logger.warning("Recording upload returned %s", up)
            except Exception as exc:
                logger.error("Failed to upload call recording for %s: %s", call_id, exc)

    if transcript and len(transcript) > TRANSCRIPT_NOTE_CHAR_LIMIT:
        txt_path = out_dir / f"{stem}_transcript.txt"
        txt_path.write_text(transcript, encoding="utf-8")
        result["transcript_path"] = str(txt_path)
        try:
            up = ezlynx.upload_document(
                applicant_id=str(applicant_id),
                file_path=txt_path,
                folder_name="Documents",
                description=f"{policy_number or 'Policy'} Robie Call Transcript {call_id}",
                policy_number=policy_number,
                doc_type="correspondence",
                label_to_apply=SAFE_DOCUMENT_LABEL,
                use_playwright_fallback=True,
                prefer_api=True,
            )
            if up.get("status") in ("success", "simulated"):
                result["transcript_uploaded"] = True
                result["transcript_upload"] = up
        except Exception as exc:
            logger.error("Failed to upload full transcript for %s: %s", call_id, exc)

    return result


def _resolve_policy_context(data: Dict[str, Any]) -> Dict[str, Any]:
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    policy_number = metadata.get("policy_number") or data.get("policy_number")
    carrier_name = metadata.get("carrier_name") or data.get("carrier_name") or "Carrier Underwriting"
    insured_name = metadata.get("insured_name") or data.get("insured_name") or "Insured Account"
    applicant_id = metadata.get("applicant_id") or data.get("applicant_id")
    csr_email = (
        metadata.get("assigned_csr_email")
        or data.get("assigned_csr_email")
        or DEFAULT_ASSIGNED_CSR_EMAIL
    )
    if isinstance(csr_email, str) and "robie@" in csr_email.lower():
        csr_email = DEFAULT_ASSIGNED_CSR_EMAIL
    lob = metadata.get("line_of_business") or data.get("line_of_business") or "Commercial"

    try:
        db = SessionLocal()
        try:
            pol_record = None
            if policy_number:
                pol_record = (
                    db.query(PolicyRenewal)
                    .filter(PolicyRenewal.policy_number.ilike(f"%{str(policy_number).strip()}%"))
                    .first()
                )
            if pol_record:
                if not applicant_id:
                    applicant_id = pol_record.applicant_id
                if not carrier_name or carrier_name == "Carrier Underwriting":
                    carrier_name = pol_record.carrier_name
                if not insured_name or insured_name == "Insured Account":
                    insured_name = pol_record.insured_name
                lob = pol_record.line_of_business or lob
                summary = data.get("summary") or data.get("call_summary") or ""
                if any(
                    k in str(summary).lower()
                    for k in ["quote issued", "terms released", "quoted", "available in portal"]
                ):
                    pol_record.status = RenewalStatus.QUOTE_RECEIVED
                db.commit()
        finally:
            db.close()
    except Exception as exc:
        logger.debug("renewals.db lookup skipped for call completion: %s", exc)

    if not applicant_id and policy_number:
        try:
            client = EZLynxApiClient()
            search_hit = client.search_applicant(str(policy_number).strip())
            if search_hit and search_hit.get("applicant_id"):
                applicant_id = str(search_hit["applicant_id"])
                if not insured_name or insured_name == "Insured Account":
                    insured_name = search_hit.get("applicant_name", insured_name)
                logger.info(
                    "Resolved applicant_id %s for policy %s via EZLynx search API",
                    applicant_id,
                    policy_number,
                )
        except Exception as exc:
            logger.debug("EZLynx direct search fallback failed for %s: %s", policy_number, exc)

    return {
        "policy_number": policy_number,
        "carrier_name": carrier_name,
        "insured_name": insured_name,
        "applicant_id": applicant_id,
        "csr_email": csr_email,
        "line_of_business": lob,
    }


def handle_completed_call(
    data: Dict[str, Any],
    *,
    voice_client: Optional[CarrierVoiceClient] = None,
    ezlynx_client: Optional[EZLynxApiClient] = None,
    processed_store: Optional[ProcessedRobieCallStore] = None,
    skip_email: bool = False,
) -> Dict[str, Any]:
    """Fetch Bland artifacts, post the EZLynx note, upload recording/transcript."""
    call_id = data.get("call_id") or data.get("id") or "UNKNOWN_CALL"
    bland_details = fetch_bland_call_details(str(call_id), voice_client=voice_client)
    merged = merge_call_payload(data, bland_details)

    recording_url = merged.get("recording_url") or merged.get("audio_url") or "N/A"
    summary = merged.get("summary") or merged.get("call_summary") or "Call completed."
    transcript = merged.get("concatenated_transcript") or merged.get("transcript") or ""

    ctx = _resolve_policy_context(merged)
    policy_number = ctx["policy_number"]
    carrier_name = ctx["carrier_name"]
    insured_name = ctx["insured_name"]
    applicant_id = ctx["applicant_id"]
    csr_email = ctx["csr_email"]
    lob = ctx["line_of_business"]

    logger.info("Processing completed call %s for Policy #%s (%s)", call_id, policy_number, insured_name)

    ezlynx = ezlynx_client or EZLynxApiClient()
    upload_info: Dict[str, Any] = {
        "recording_uploaded": False,
        "transcript_uploaded": False,
    }
    if applicant_id:
        upload_info = upload_call_artifacts(
            ezlynx=ezlynx,
            applicant_id=str(applicant_id),
            policy_number=policy_number,
            call_id=str(call_id),
            recording_url=str(recording_url),
            transcript=str(transcript),
            voice_client=voice_client,
        )

    transfer_outcome = extract_transfer_outcome(merged)
    metadata = merged.get("metadata") if isinstance(merged.get("metadata"), dict) else {}
    transfer_to = (
        metadata.get("requestor_name")
        or merged.get("requestor_name")
        or metadata.get("producer_name")
        or merged.get("producer_name")
    )
    transfer_line = ""
    if transfer_outcome:
        if transfer_to:
            transfer_line = f"- Transfer: {transfer_outcome} to {transfer_to}"
        else:
            transfer_line = f"- Transfer: {transfer_outcome}"

    note_body = build_completion_note(
        policy_number=policy_number,
        line_of_business=lob,
        carrier_name=carrier_name,
        call_id=str(call_id),
        summary=str(summary),
        recording_url=str(recording_url),
        transcript=str(transcript),
        recording_uploaded=bool(upload_info.get("recording_uploaded")),
        transcript_attached=bool(upload_info.get("transcript_uploaded")),
        transfer_line=transfer_line,
    )

    ezlynx_posted = False
    note_id = None
    if applicant_id:
        try:
            res = ezlynx.add_note_to_discussion(
                applicant_id=int(applicant_id) if str(applicant_id).isdigit() else applicant_id,
                discussion_title=f"Renewal Manual {lob} | {policy_number} {carrier_name}",
                note_text=note_body,
                policy_number=policy_number,
                line_of_business=lob,
                carrier_name=carrier_name,
            )
            ezlynx_posted = True
            note_id = res.get("note_id")
            logger.info(
                "Successfully posted voice call note %s to EZLynx for Applicant %s",
                note_id,
                applicant_id,
            )
            store = processed_store or ProcessedRobieCallStore()
            if note_id:
                store.mark(
                    str(note_id),
                    applicant_id=str(applicant_id),
                    note_id=str(note_id),
                    status="CALL_COMPLETED",
                )
        except Exception as exc:
            logger.error("Failed to post voice note to EZLynx: %s", exc)

    email_sent = False
    if not skip_email:
        try:
            gmail = GmailRenewalClient()
            email_subj = f"Carrier Call Report: {carrier_name} - Pol #{policy_number} ({insured_name})"
            email_body = f"""Hi there,

Robie has completed the outbound follow-up call with {carrier_name} underwriting.

- **Insured Account:** {insured_name}
- **Policy Number:** #{policy_number} ({lob})
- **Carrier:** {carrier_name}
- **Assigned CSR:** {DEFAULT_ASSIGNED_CSR_NAME}
- **Summary:** {summary}
- **Audio Recording:** {recording_url}

This activity has been posted directly to the EZLynx Discussion Card for this policy.

Transcript excerpt:
{str(transcript)[:600] + ('...' if len(str(transcript)) > 600 else '')}

Best regards,
Robie
StreetSmart Insurance Operations Engine
"""
            gmail.send_email(to_email=csr_email, subject=email_subj, body_text=email_body)
            email_sent = True
        except Exception as exc:
            logger.error("Failed to send CSR email for call %s: %s", call_id, exc)

    return {
        "call_id": call_id,
        "policy_number": policy_number,
        "ezlynx_posted": ezlynx_posted,
        "note_id": note_id,
        "email_sent": email_sent,
        "recording_uploaded": bool(upload_info.get("recording_uploaded")),
        "transcript_uploaded": bool(upload_info.get("transcript_uploaded")),
        "assigned_csr": DEFAULT_ASSIGNED_CSR_NAME,
        "transfer_occurred": bool(transfer_outcome),
        "transfer_outcome": transfer_outcome,
    }
