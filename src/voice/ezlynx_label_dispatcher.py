"""
EZLynx Label & Note Call Dispatcher.

Enables CSRs and Account Managers to trigger autonomous calls directly from EZLynx:
1. CSR applies the org label ``Robie Call`` and/or writes that phrase in the note/title
   (carrier path by default; client only with ``Call type: client`` / who-to-call insured).
2. Or CSR applies ``Robie lead follow-up`` (and close variants) to force the client
   follow-up path without requiring ``Call type: client`` in the note body.
3. Or CSR applies ``Robie client outreach`` or a pathway WF label
   (``Robie cancellation``, ``Robie audit``, ``Robie returned mail``,
   ``Robie e-sign`` / ``Robie esign``, ``Robie additional info``,
   ``Robie recommendations``, ``Robie unresponsive``,
   ``Robie renewal reach-out`` / ``Robie renewal reachout`` — close
   variants: spaces / hyphens / underscores, optional ``robie `` prefix,
   brackets) to force ``call_type=client_outreach`` — primary then
   secondary applicant phones, no Sales Center producer greeting.
   Pathway is inferred from the same label (``Robie client outreach``
   stays generic unless the note body matches a pathway).
4. Note specifies 'Who to call' (phone number/carrier) and 'What to say' (instructions).
5. Robie parses the instructions, dispatches the call via Bland AI from +1 (732) 298-6745.
6. When complete, Robie automatically posts the call transcript, recording link, and summary
   back into the applicant's EZLynx discussion card.
"""

import logging
import os
import re
from typing import Optional, Dict, Any, List, Iterable

from src.ezlynx.api_client import EZLynxApiClient
from src.voice.call_directory import (
    HARDCODED_CARRIER_PHONES,
    lookup_carrier_phone,
)
from src.voice.context_hydrator import (
    CALL_TYPE_CARRIER,
    CALL_TYPE_CLIENT_FOLLOWUP,
    CALL_TYPE_CLIENT_OUTREACH,
    CallingDossier,
    ContextHydrator,
    extract_client_first_name,
    extract_producer_name,
    extract_sales_center_producer_name,
    is_client_call_type,
    match_policy_record,
    normalize_call_type,
    resolve_client_outreach_targets,
    unwrap_policy_list,
)
from src.voice.outreach_pathways import (
    EZLYNX_ADMIN_OUTREACH_LABELS,
    infer_outreach_pathway,
    is_client_outreach_dispatch_label,
    strip_client_outreach_trigger_phrases,
    text_has_client_outreach_trigger,
)
from src.voice.processed_robie_notes import ProcessedRobieCallStore
from src.voice.voice_client import CarrierVoiceClient

logger = logging.getLogger("ezlynx_label_dispatcher")

ROBIE_LABEL_TRIGGERS = ["robie call", "robie_call", "call robie", "robie: call", "[robie call]"]
# Org label + title/note phrases. Matching is case-insensitive; hyphen / space /
# underscore variants collapse to the same token (see `_text_matches_lead_followup_trigger`).
ROBIE_LEAD_FOLLOWUP_TRIGGERS = [
    "robie lead follow-up",
    "robie lead follow up",
    "robie lead followup",
    "robie_lead_followup",
    "robie_lead_follow_up",
    "[robie lead follow-up]",
    "[robie lead followup]",
]
_LEAD_FOLLOWUP_COLLAPSED = "robieleadfollowup"


def _build_client_outreach_trigger_phrases() -> List[str]:
    """Admin names plus space / hyphen / underscore / bracket close variants."""
    phrases: List[str] = []
    seen = set()
    for name in EZLYNX_ADMIN_OUTREACH_LABELS:
        lower = name.lower()
        for candidate in (lower, lower.replace(" ", "_"), lower.replace(" ", "-"), f"[{lower}]"):
            if candidate not in seen:
                seen.add(candidate)
                phrases.append(candidate)
    return phrases


# Org label + title/note phrases. Pathway WF labels (``Robie audit``, …)
# dispatch the same client_outreach path as ``Robie client outreach``.
# ``robie cancellation`` remains the cancellation alias (Carlo 2026-09-05).
ROBIE_CLIENT_OUTREACH_TRIGGERS = _build_client_outreach_trigger_phrases()
CLIENT_WHO_TOKENS = {
    "insured",
    "the insured",
    "client",
    "the client",
    "customer",
    "the customer",
    "applicant",
    "the applicant",
    "policyholder",
    "the policyholder",
}

# Live GetPagedDiscussions page size observed on hermes-poc-01.
PORTAL_DISCUSSIONS_PAGE_SIZE = 50

# Markers that identify Robie's own acknowledgement / clarification / transcript posts.
# Do not treat a CSR trigger phrase like "[ROBIE CALL]" as self-authored.
ROBIE_SELF_BODY_MARKERS = (
    "[robie autonomous",
    "robie autonomous",
    "robie call -",
    "[robie call initiated",
    "robie was here",
    "autonomous call dispatched",
    "call dispatched",
    "autonomous carrier phone outreach",
    "transcript will be posted",
    "audio recording and transcript",
    "📞 [robie",
    "🤖 [robie",
    "⚠️ [robie",
)


def _text_matches_robie_trigger(text: Optional[str]) -> bool:
    """True when any Robie Call phrase appears in text (case-insensitive)."""
    if not text:
        return False
    lower = text.lower()
    return any(trigger in lower for trigger in ROBIE_LABEL_TRIGGERS)


def _collapse_trigger_text(text: str) -> str:
    """Lowercase and strip separators so hyphen/space/underscore variants match."""
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _text_matches_lead_followup_trigger(text: Optional[str]) -> bool:
    """True when Robie lead follow-up (or a close variant) appears, case-insensitive."""
    if not text:
        return False
    lower = text.lower()
    if any(trigger in lower for trigger in ROBIE_LEAD_FOLLOWUP_TRIGGERS):
        return True
    return _LEAD_FOLLOWUP_COLLAPSED in _collapse_trigger_text(text)


def _text_matches_client_outreach_trigger(
    text: Optional[str], *, whole_value: bool = False
) -> bool:
    """True when a client-outreach dispatch phrase or org label appears.

    Free text (title / note body) requires the ``robie `` prefix so a
    carrier note that mentions ``audit`` does not dispatch. Standalone
    org labels accept the optional ``robie `` prefix (``audit`` ==
    ``Robie audit``) plus space / hyphen / underscore / bracket variants.
    """
    if whole_value:
        return is_client_outreach_dispatch_label(text)
    return text_has_client_outreach_trigger(text)


def _text_matches_any_robie_dispatch_trigger(text: Optional[str]) -> bool:
    """True when Robie Call, lead follow-up, or client outreach should dispatch."""
    return (
        _text_matches_robie_trigger(text)
        or _text_matches_lead_followup_trigger(text)
        or _text_matches_client_outreach_trigger(text)
    )


def extract_discussion_note_text(discussion: Dict[str, Any]) -> str:
    """Read the CSR note body from a GetPagedDiscussions card.

    Live portal payload uses ``discussionNote.note``. Older mocks / REST
    envelopes used ``noteText``. Prefer the live field, then fall back.
    """
    note_obj = discussion.get("discussionNote") if isinstance(discussion, dict) else None
    if not isinstance(note_obj, dict):
        note_obj = {}
    for candidate in (note_obj.get("note"), note_obj.get("noteText"), discussion.get("description") if isinstance(discussion, dict) else None):
        if isinstance(candidate, str) and candidate.strip():
            return candidate
        if candidate and not isinstance(candidate, str):
            return str(candidate)
    return ""


def extract_discussion_note_labels(discussion: Dict[str, Any]) -> List[str]:
    """Return labelName values from discussionNote.noteLabels (live portal shape)."""
    note_obj = discussion.get("discussionNote") if isinstance(discussion, dict) else None
    if not isinstance(note_obj, dict):
        return []
    raw_labels = note_obj.get("noteLabels") or []
    names: List[str] = []
    if not isinstance(raw_labels, list):
        return names
    for label in raw_labels:
        if isinstance(label, dict):
            name = label.get("labelName") or ""
            if name:
                names.append(str(name))
        elif isinstance(label, str) and label.strip():
            names.append(label)
    return names


def _discussion_matches_trigger(
    discussion: Dict[str, Any],
    matcher,
    note_text: Optional[str] = None,
) -> bool:
    """True when title, note body, or noteLabels[].labelName match ``matcher``."""
    title = ""
    if isinstance(discussion, dict):
        title = discussion.get("title") or ""
    if note_text is None:
        note_text = extract_discussion_note_text(discussion)
    if matcher(title) or matcher(note_text):
        return True
    for label_name in extract_discussion_note_labels(discussion):
        if matcher(label_name):
            return True
    return False


def discussion_is_lead_followup(discussion: Dict[str, Any], note_text: Optional[str] = None) -> bool:
    """Trigger when an org label or title/note text matches Robie lead follow-up.

    Accepts close variants (``Robie Lead Follow-up``, ``robie lead follow up``,
    ``Robie lead followup``) case-insensitively. Forces ``client_followup``.
    """
    return _discussion_matches_trigger(
        discussion, _text_matches_lead_followup_trigger, note_text=note_text
    )


def discussion_is_client_outreach(discussion: Dict[str, Any], note_text: Optional[str] = None) -> bool:
    """Trigger when an org label or title/note text matches client outreach.

    Accepts ``Robie client outreach``, pathway WF labels (``Robie audit``,
    ``Robie cancellation``, ``Robie returned mail``, ``Robie e-sign`` /
    ``Robie esign``, ``Robie additional info``, ``Robie recommendations``,
    ``Robie unresponsive``, ``Robie renewal reach-out`` /
    ``Robie renewal reachout``), and close variants (spaces / hyphens /
    underscores, optional ``robie `` prefix on labels, brackets).
    Forces ``client_outreach``.
    """
    title = ""
    if isinstance(discussion, dict):
        title = discussion.get("title") or ""
    if note_text is None:
        note_text = extract_discussion_note_text(discussion)
    if _text_matches_client_outreach_trigger(title) or _text_matches_client_outreach_trigger(
        note_text
    ):
        return True
    for label_name in extract_discussion_note_labels(discussion):
        if _text_matches_client_outreach_trigger(label_name, whole_value=True):
            return True
    return False


def discussion_is_robie_call(discussion: Dict[str, Any], note_text: Optional[str] = None) -> bool:
    """Trigger when an org label or title/note text matches a Robie dispatch phrase.

    CSRs should apply ``Robie Call``, ``Robie lead follow-up``,
    ``Robie client outreach``, or a pathway WF label (``Robie audit``, …)
    and/or write that phrase in the note.
    Instruction-style notes without the label or phrase do not fire.
    """
    if _discussion_matches_trigger(discussion, _text_matches_robie_trigger, note_text=note_text):
        return True
    if discussion_is_lead_followup(discussion, note_text=note_text):
        return True
    return discussion_is_client_outreach(discussion, note_text=note_text)


def _requestor_string(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _requestor_from_user_object(node: Any) -> Dict[str, Optional[str]]:
    """Pull name/email from a createdBy / user object or scalar."""
    result: Dict[str, Optional[str]] = {"name": None, "email": None}
    if isinstance(node, dict):
        for key in (
            "createdByName",
            "Name",
            "FullName",
            "DisplayName",
            "name",
            "fullName",
            "displayName",
            "userName",
            "UserName",
            "userFullName",
        ):
            name = _requestor_string(node.get(key))
            if name and "@" not in name:
                result["name"] = name
                break
        for key in ("createdByEmail", "Email", "email", "EMail", "userEmail", "UserEmail"):
            email = _requestor_string(node.get(key))
            if email and "@" in email:
                result["email"] = email
                break
        return result
    text = _requestor_string(node)
    if not text:
        return result
    if "@" in text:
        result["email"] = text
    else:
        result["name"] = text
    return result


def extract_discussion_requestor(discussion: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Resolve the Robie Call invoker from Portal GetPagedDiscussions metadata.

    Verified in-repo fields (fixtures + ``latest_activity_is_from_robie``):
    - ``discussionNote.createdByName`` (primary author on live-shaped cards)
    - ``discussionNote.createdBy`` (string or user object)
    - ``discussion.lastModifiedByName`` (card-level fallback)

    Also accepted if Portal adds them on ``discussionNote``:
    ``userName``, ``createdByEmail``, ``userEmail``, ``email``.
    """
    name: Optional[str] = None
    email: Optional[str] = None
    note_obj = discussion.get("discussionNote") if isinstance(discussion, dict) else None
    if not isinstance(note_obj, dict):
        note_obj = {}

    for source in (note_obj, note_obj.get("createdBy"), note_obj.get("user"), note_obj.get("author")):
        parsed = _requestor_from_user_object(source) if source is not note_obj else {
            "name": (
                _requestor_string(note_obj.get("createdByName"))
                or _requestor_string(note_obj.get("userName"))
                or _requestor_string(note_obj.get("createdByUserName"))
                or _requestor_string(note_obj.get("authorName"))
            ),
            "email": (
                _requestor_string(note_obj.get("createdByEmail"))
                or _requestor_string(note_obj.get("userEmail"))
                or _requestor_string(note_obj.get("email"))
                or _requestor_string(note_obj.get("authorEmail"))
            ),
        }
        if parsed.get("name") and not name:
            name = parsed["name"]
        if parsed.get("email") and not email:
            email = parsed["email"]

    if not name or not email:
        card_parsed = _requestor_from_user_object(
            {
                "name": (
                    (discussion.get("createdByName") if isinstance(discussion, dict) else None)
                    or (discussion.get("lastModifiedByName") if isinstance(discussion, dict) else None)
                ),
                "email": (
                    (discussion.get("createdByEmail") if isinstance(discussion, dict) else None)
                    or (discussion.get("lastModifiedByEmail") if isinstance(discussion, dict) else None)
                ),
            }
        )
        name = name or card_parsed.get("name")
        email = email or card_parsed.get("email")

    if name and "robie" in name.lower():
        return {"name": None, "email": None}
    if email and str(email).lower().startswith("robie@"):
        return {"name": None, "email": None}
    return {"name": name, "email": email}


def extract_discussion_note_id(discussion: Dict[str, Any]) -> Optional[str]:
    """Return discussionNote.noteId when present."""
    note_obj = discussion.get("discussionNote") if isinstance(discussion, dict) else None
    if not isinstance(note_obj, dict):
        return None
    note_id = note_obj.get("noteId")
    if note_id is None or str(note_id).strip() == "":
        return None
    return str(note_id)


def discussion_note_identity(discussion: Dict[str, Any]) -> Optional[str]:
    """Stable identity: noteId, or discussionId:noteId when both exist."""
    note_id = extract_discussion_note_id(discussion)
    if not note_id:
        return None
    discussion_id = discussion.get("discussionId") if isinstance(discussion, dict) else None
    if discussion_id is not None and str(discussion_id).strip() != "":
        return f"{discussion_id}:{note_id}"
    return note_id


def _note_body_has_robie_self_marker(note_text: Optional[str]) -> bool:
    """True when the note body looks like a Robie ack/clarification/transcript."""
    if not note_text:
        return False
    lower = note_text.lower()
    if any(marker in lower for marker in ROBIE_SELF_BODY_MARKERS):
        return True
    stripped = strip_client_outreach_trigger_phrases(lower)
    for trigger in sorted(
        ROBIE_LABEL_TRIGGERS + ROBIE_LEAD_FOLLOWUP_TRIGGERS + ROBIE_CLIENT_OUTREACH_TRIGGERS,
        key=len,
        reverse=True,
    ):
        stripped = stripped.replace(trigger, " ")
    return "[robie" in stripped


def latest_activity_is_from_robie(
    discussion: Dict[str, Any],
    note_text: Optional[str] = None,
) -> bool:
    """Ignore cards whose latest note/activity is already Robie's.

    Checks author name and Robie post markers in the note body only (not title),
    so a CSR title like ``Robie Call - Follow up`` is still allowed.
    """
    note_obj = discussion.get("discussionNote") if isinstance(discussion, dict) else None
    if not isinstance(note_obj, dict):
        note_obj = {}
    last_author = (
        note_obj.get("createdByName")
        or note_obj.get("createdBy")
        or (discussion.get("lastModifiedByName") if isinstance(discussion, dict) else None)
        or ""
    )
    if "robie" in str(last_author).lower():
        return True
    if note_text is None:
        note_text = extract_discussion_note_text(discussion)
    return _note_body_has_robie_self_marker(note_text)


def normalize_phone_e164(raw_phone: Optional[str]) -> Optional[str]:
    """Normalizes any phone string to E.164 format (+1XXXXXXXXXX)."""
    if not raw_phone:
        return None
    digits = re.sub(r"\D", "", str(raw_phone))
    if len(digits) == 10:
        return f"+1{digits}"
    elif len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    elif digits:
        return f"+{digits}"
    return None


def parse_call_note_instructions(note_text: str) -> Dict[str, Any]:
    """
    Parses a note written by a CSR in EZLynx containing call instructions.
    
    Expected flexible formats:
    - Who to call: Hartford (800-555-1234)
    - What to say: Inquire about renewal quote for policy 12345
    - Phone: 800-555-1234
    - Contact: Jane Doe
    """
    clean_text = note_text.strip()
    result = {
        "is_robie_call": False,
        "phone_number": None,
        "target_name": None,
        "policy_number": None,
        "instructions": "",
        "call_type": None,
    }

    # Check for trigger tag/keyword in the note/title text (either dispatch label)
    if _text_matches_any_robie_dispatch_trigger(clean_text):
        result["is_robie_call"] = True

    # 1. Extract Phone Number
    # Match standard US phone formats: (xxx) xxx-xxxx, xxx-xxx-xxxx, 1xxxxxxxxxx
    phone_match = re.search(r"(?:who to call|phone|call|dial|tel|number)?[:\s]*(\+?1?[-.\s]?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})\b", clean_text, re.IGNORECASE)
    if phone_match:
        raw_phone = phone_match.group(1).strip()
        digits = re.sub(r"\D", "", raw_phone)
        if len(digits) == 10:
            result["phone_number"] = f"+1{digits}"
        elif len(digits) == 11 and digits.startswith("1"):
            result["phone_number"] = f"+{digits}"

    # 2. Extract Who To Call (Entity / Carrier / Contact)
    who_match = re.search(r"(?:who to call|contact|carrier|target)[:\s]+([^;\n\r\.]+?)(?=\s+(?:policy|phone|what|say|instructions|tell)|[;\n\r\.]|$)", clean_text, re.IGNORECASE)
    if who_match:
        candidate_who = who_match.group(1).strip()
        # Clean out any trailing phone number if captured in same line
        candidate_who = re.sub(r"\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", "", candidate_who).strip()
        result["target_name"] = candidate_who.strip(" -:,.")

    # 3. Extract Policy Number
    pol_match = re.search(r"(?:policy|pol|policy number|pol#|policy#)[:\s#]+([A-Z0-9\-]{5,})", clean_text, re.IGNORECASE)
    if pol_match:
        result["policy_number"] = pol_match.group(1).strip()

    # 4. Explicit call-type cue (do not guess beyond this + who-to-call).
    # Label phrases win over ``Call type:`` when both appear:
    # client outreach > lead follow-up > explicit cue.
    type_match = re.search(
        r"call\s*type\s*:\s*(client[\s_-]*outreach|outreach|cancellation|"
        r"client(?:[\s_-]*follow[\s_-]*up)?|carrier|existing)\b",
        clean_text,
        re.IGNORECASE,
    )
    if type_match:
        result["call_type"] = normalize_call_type(type_match.group(1))
    if _text_matches_lead_followup_trigger(clean_text):
        result["call_type"] = CALL_TYPE_CLIENT_FOLLOWUP
    if _text_matches_client_outreach_trigger(clean_text):
        result["call_type"] = CALL_TYPE_CLIENT_OUTREACH

    # 5. Extract What To Say / Instructions
    say_match = re.search(r"(?:what to say|instructions|say|message|notes|details|tell)[:\s]+([\s\S]+)", clean_text, re.IGNORECASE)
    if say_match:
        instructions = say_match.group(1).strip()
        # Remove signature lines if present
        instructions = re.sub(r"(?:Robie was here|Thanks|Sincerely).*$", "", instructions, flags=re.IGNORECASE | re.DOTALL).strip()
        result["instructions"] = instructions
    else:
        # If no explicit "what to say:" label, use the remaining text
        lines = [line.strip() for line in clean_text.splitlines() if line.strip()]
        filtered_lines = []
        for line in lines:
            line_lower = line.lower()
            if any(t in line_lower for t in ROBIE_LABEL_TRIGGERS):
                continue
            if _text_matches_lead_followup_trigger(line):
                continue
            if _text_matches_client_outreach_trigger(line):
                continue
            if line_lower.startswith(("who to call:", "phone:", "contact:", "policy:", "call type:")):
                continue
            filtered_lines.append(line)
        result["instructions"] = " ".join(filtered_lines).strip()

    return result


# Hardcoded fallback only. Dispatcher lookup prefers the server store, then seed.
KNOWN_CARRIER_PHONES = HARDCODED_CARRIER_PHONES


def lookup_known_carrier_phone(carrier_name: Optional[str]) -> Optional[str]:
    """Resolve phone from runtime store → seed JSON → hardcoded map."""
    return lookup_carrier_phone(carrier_name)


def infer_call_type(
    parsed: Dict[str, Any],
    insured_name: Optional[str] = None,
    lead_followup: bool = False,
    client_outreach: bool = False,
) -> str:
    """Resolve carrier vs client_followup vs client_outreach.

    Any client-outreach trigger (``Robie client outreach`` or a pathway WF
    label such as ``Robie audit`` / ``robie cancellation``) always forces
    ``client_outreach``. ``Robie lead follow-up`` always forces
    ``client_followup``. Robie Call alone stays carrier unless the note has
    ``Call type: client`` or who-to-call insured.

    Winner when multiple dispatch labels appear on the same note (do not
    change): client_outreach beats lead_followup beats Robie Call/carrier.
    ``Robie lead follow-up`` is otherwise unchanged (client_followup +
    requestor transfer).
    """
    if client_outreach:
        return CALL_TYPE_CLIENT_OUTREACH
    if lead_followup:
        return CALL_TYPE_CLIENT_FOLLOWUP
    explicit = parsed.get("call_type")
    if explicit in (CALL_TYPE_CLIENT_OUTREACH, CALL_TYPE_CLIENT_FOLLOWUP, CALL_TYPE_CARRIER):
        return explicit
    who = (parsed.get("target_name") or "").strip().lower()
    if who in CLIENT_WHO_TOKENS:
        return CALL_TYPE_CLIENT_FOLLOWUP
    if insured_name and who and who in insured_name.lower():
        return CALL_TYPE_CLIENT_FOLLOWUP
    return CALL_TYPE_CARRIER


class EZLynxLabelCallDispatcher:
    """Dispatches calls based on EZLynx notes/discussions tagged 'robie call'."""

    def __init__(
        self,
        ezlynx_client: Optional[EZLynxApiClient] = None,
        voice_client: Optional[CarrierVoiceClient] = None,
        hydrator: Optional[ContextHydrator] = None,
        processed_store: Optional[ProcessedRobieCallStore] = None,
    ):
        self.ezlynx = ezlynx_client or EZLynxApiClient()
        self.voice = voice_client or CarrierVoiceClient()
        self.hydrator = hydrator or ContextHydrator()
        self.processed_store = processed_store or ProcessedRobieCallStore()

    def process_applicant_notes_for_calls(
        self,
        applicant_id: str,
        dry_run: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Fetches discussions/notes for an applicant, scans for 'robie call' instructions,
        resolves missing carrier phone numbers from policy or carrier directory,
        initiates the calls, and posts confirmation or clarification notes back.
        """
        results = []
        discussions = self.ezlynx.get_applicant_discussions(
            applicant_id, page_size=PORTAL_DISCUSSIONS_PAGE_SIZE
        )
        if not discussions:
            logger.info(f"No discussions found for applicant {applicant_id}")
            return results

        # Fetch applicant profile for context
        app_res = self.ezlynx.get_applicant(applicant_id)
        app_data = app_res.get("applicant", {}) if app_res.get("status") == "success" else {}
        insured_name = (
            app_data.get("BusinessName")
            or f"{app_data.get('FirstName', '')} {app_data.get('LastName', '')}".strip()
            or f"Applicant #{applicant_id}"
        )

        # Lazy cache applicant policies to avoid duplicate API calls
        applicant_policies = None

        # Sales Center producerName is the client_followup greeting source.
        # Commission policy Producer and Classic AssignedTo username are not.
        sales_opportunities = None
        sidebar = None
        try:
            sales_opportunities = self.ezlynx.get_sales_center_opportunities(applicant_id)
        except Exception as exc:
            logger.debug("Sales Center opportunities lookup skipped: %s", exc)
        # Sidebar AssignedTo is the producer fallback; CommercialDetail / contacts
        # supply the client first name when Classic only has BusinessName / LLC.
        if not extract_sales_center_producer_name(
            sales_opportunities
        ) or not extract_client_first_name(app_data, insured_name):
            try:
                sidebar = self.ezlynx.get_applicant_sidebar(applicant_id)
            except Exception as exc:
                logger.debug("Portal sidebar identity lookup skipped: %s", exc)

        for disc in discussions:
            title = disc.get("title", "")
            note_text = extract_discussion_note_text(disc)
            combined_text = f"{title}\n{note_text}"

            parsed = parse_call_note_instructions(combined_text)
            if not (discussion_is_robie_call(disc, note_text=note_text) or parsed["is_robie_call"]):
                continue

            # Guard: never re-trigger on Robie's own latest activity
            if latest_activity_is_from_robie(disc, note_text=note_text):
                logger.info(
                    f"Skipping discussion '{title}': latest activity is already from Robie."
                )
                continue

            identity = discussion_note_identity(disc)
            note_id = extract_discussion_note_id(disc)
            discussion_id = disc.get("discussionId")
            if identity and self.processed_store.has(identity):
                logger.info(
                    f"Skipping discussion '{title}' noteId={note_id}: already processed ({identity})."
                )
                continue

            # 1. Resolve Policy Number if missing in note body
            policy_num = parsed["policy_number"]
            if not policy_num:
                # Attempt to extract from discussion title (e.g. "... | PWC1239278 Associated Specialty")
                pol_in_title = re.search(r"\|\s*([A-Z0-9\-]{5,})\b", title, re.IGNORECASE)
                if pol_in_title:
                    policy_num = pol_in_title.group(1).strip()
                else:
                    # Look up active policies on applicant
                    if applicant_policies is None:
                        applicant_policies = unwrap_policy_list(
                            self.ezlynx.get_applicant_policies(applicant_id)
                        )
                    if applicant_policies:
                        policy_num = applicant_policies[0].get("policyNumber") or applicant_policies[0].get("PolicyNumber")

            # 2. Resolve Carrier Name if missing
            target_carrier = parsed["target_name"]
            if not target_carrier or target_carrier == "Carrier Representative":
                # Check policy directory or applicant policies
                if applicant_policies is None:
                    applicant_policies = unwrap_policy_list(
                        self.ezlynx.get_applicant_policies(applicant_id)
                    )
                if applicant_policies:
                    for pol in applicant_policies:
                        p_num = pol.get("policyNumber") or pol.get("PolicyNumber")
                        if policy_num and p_num == policy_num:
                            target_carrier = pol.get("carrierName") or pol.get("CarrierName") or pol.get("companyName")
                            break
                    if not target_carrier and applicant_policies:
                        target_carrier = applicant_policies[0].get("carrierName") or applicant_policies[0].get("CarrierName")

            target_carrier = target_carrier or "Carrier Representative"

            if applicant_policies is None:
                applicant_policies = unwrap_policy_list(
                    self.ezlynx.get_applicant_policies(applicant_id)
                )
            matched_policy = match_policy_record(applicant_policies or [], policy_num)
            lead_followup = discussion_is_lead_followup(disc, note_text=note_text)
            client_outreach = discussion_is_client_outreach(disc, note_text=note_text)
            call_type = infer_call_type(
                parsed,
                insured_name=insured_name,
                lead_followup=lead_followup,
                client_outreach=client_outreach,
            )

            instructions = parsed["instructions"] or "Inquire regarding policy status and quote release."
            safe_pol_num = policy_num or "N/A"
            requestor = extract_discussion_requestor(disc)

            if call_type == CALL_TYPE_CLIENT_OUTREACH:
                if sidebar is None:
                    try:
                        sidebar = self.ezlynx.get_applicant_sidebar(applicant_id)
                    except Exception as exc:
                        logger.debug("Portal sidebar ContactInfo lookup skipped: %s", exc)
                outreach_result = self._dispatch_client_outreach(
                    applicant_id=applicant_id,
                    applicant=app_data,
                    sidebar=sidebar,
                    matched_policy=matched_policy,
                    sales_opportunities=sales_opportunities,
                    insured_name=insured_name,
                    title=title,
                    policy_num=policy_num,
                    safe_pol_num=safe_pol_num,
                    target_carrier=target_carrier,
                    instructions=instructions,
                    requestor=requestor,
                    identity=identity,
                    note_id=note_id,
                    discussion_id=discussion_id,
                    dry_run=dry_run,
                    labels=extract_discussion_note_labels(disc),
                    combined_text=combined_text,
                )
                results.append(outreach_result)
                continue

            # 3. Resolve Phone Number from carrier knowledge base / directory if missing
            phone = parsed["phone_number"]
            if not phone and call_type == CALL_TYPE_CLIENT_FOLLOWUP:
                phone = normalize_phone_e164(
                    app_data.get("CellPhone")
                    or app_data.get("HomePhone")
                    or app_data.get("BusinessPhone")
                    or app_data.get("Phone")
                )
                if phone:
                    logger.info(f"Resolved client phone {phone} from applicant profile.")
            if not phone and call_type != CALL_TYPE_CLIENT_FOLLOWUP:
                phone = lookup_known_carrier_phone(target_carrier)
                if phone:
                    logger.info(f"Resolved phone {phone} for carrier '{target_carrier}' from directory.")

            # 4. If phone is STILL missing, post a polite clarification note directly via API (avoiding Playwright)
            if not phone:
                logger.warning(f"Applicant {applicant_id}: 'robie call' requested for '{target_carrier}', but no phone number found.")
                clarification_note = (
                    f"Policy: #{safe_pol_num} ({target_carrier})\n\n"
                    f"⚠️ [ROBIE CALL - PHONE NUMBER NEEDED]\n"
                    f"Robie received your request to call regarding this account, but could not determine "
                    f"the {'client' if is_client_call_type(call_type) else 'carrier'} phone number"
                    f" for '{target_carrier if not is_client_call_type(call_type) else insured_name}'.\n\n"
                    f"To trigger this call, please reply to this card with the phone number:\n"
                    f"• Example: \"Phone: 800-555-1234\"\n"
                    f"• Or provide the underwriter / client direct contact info.\n\n"
                    f"Robie will automatically place the call once the number is provided."
                )
                self._post_dispatcher_note(
                    applicant_id=applicant_id,
                    discussion_title=title,
                    note_text=clarification_note,
                    policy_number=policy_num,
                    carrier_name=target_carrier,
                )
                if identity:
                    self.processed_store.mark(
                        identity,
                        applicant_id=applicant_id,
                        discussion_id=discussion_id,
                        note_id=note_id,
                        status="CLARIFICATION_NEEDED",
                        dry_run=dry_run,
                    )
                results.append({
                    "applicant_id": applicant_id,
                    "discussion_title": title,
                    "target": target_carrier,
                    "status": "CLARIFICATION_NEEDED",
                    "reason": "MISSING_PHONE_NUMBER",
                    "call_type": call_type,
                    "note_id": note_id,
                    "note_identity": identity,
                })
                continue

            logger.info(
                f"[ROBIE CALL DETECTED] Applicant: {applicant_id} ({insured_name}) | "
                f"Target: {target_carrier} ({phone}) | Policy: {safe_pol_num}"
            )

            dossier = CallingDossier(
                policy_number=safe_pol_num,
                insured_name=insured_name,
                carrier_name=target_carrier if call_type != CALL_TYPE_CLIENT_FOLLOWUP else insured_name,
                carrier_phone=phone,
                line_of_business="Commercial Lines",
                applicant_id=applicant_id,
                custom_instructions=instructions,
                client_first_name=extract_client_first_name(
                    app_data, insured_name, sidebar=sidebar
                ),
                producer_name=extract_producer_name(
                    matched_policy,
                    app_data,
                    sales_opportunities=sales_opportunities,
                    sidebar=sidebar,
                ),
                requestor_name=requestor.get("name"),
                requestor_email=requestor.get("email"),
                call_type=call_type,
                assigned_csr_email="carlo@streetsmart.insurance",
            )
            self.hydrator.enrich_identity(
                dossier,
                applicant=app_data,
                policy=matched_policy,
                sales_opportunities=sales_opportunities,
                sidebar=sidebar,
                call_type=call_type,
                requestor_name=requestor.get("name"),
                requestor_email=requestor.get("email"),
            )

            # Build call prompt
            call_prompt = self.voice.build_call_prompt(
                dossier=dossier,
                custom_instructions=f"SPECIFIC CSR INSTRUCTIONS: {instructions}",
            )

            # Dispatch call
            call_result = self.voice.dispatch_call(
                dossier=dossier,
                dry_run=dry_run,
            )

            call_id = call_result.get("call_id", "sim_call_001")
            status = call_result.get("status", "DISPATCHED")

            transfer_line = (
                f"Warm transfer: enabled to requestor {dossier.requestor_name} at {dossier.requestor_phone}."
                if dossier.requestor_phone
                else (
                    "Warm transfer: not available (requestor / label-invoker phone must be an "
                    "E.164 DID in data/voice_call_directory.json; will not fall back to the "
                    "EZLynx Producer)."
                )
            )
            dest_label = insured_name if is_client_call_type(call_type) else target_carrier
            # Post immediate acknowledgement note back to EZLynx discussion card
            ack_note = (
                f"Policy: #{safe_pol_num} ({target_carrier})\n\n"
                f"🤖 [ROBIE AUTONOMOUS CALL DISPATCHED]\n"
                f"Robie has placed an outbound call to {dest_label} at {phone}.\n"
                f"Call type: {call_type}\n"
                f"{transfer_line}\n"
                f"Caller ID: {self.voice.from_phone or '+1 (732) 298-6745'}\n"
                f"Call ID: {call_id}\n"
                f"Instructions: \"{instructions}\"\n\n"
                f"When the call concludes, full audio recording and transcript will be posted here."
            )

            self._post_dispatcher_note(
                applicant_id=applicant_id,
                discussion_title=title,
                note_text=ack_note,
                policy_number=policy_num,
                carrier_name=target_carrier,
            )
            if identity:
                self.processed_store.mark(
                    identity,
                    applicant_id=applicant_id,
                    discussion_id=discussion_id,
                    note_id=note_id,
                    status=status,
                    dry_run=dry_run,
                )

            results.append({
                "applicant_id": applicant_id,
                "discussion_title": title,
                "phone": phone,
                "target": target_carrier,
                "call_id": call_id,
                "status": status,
                "call_type": call_type,
                "producer_name": dossier.producer_name,
                "requestor_name": dossier.requestor_name,
                "requestor_phone": dossier.requestor_phone,
                "note_id": note_id,
                "note_identity": identity,
            })

        return results

    def _dispatch_client_outreach(
        self,
        *,
        applicant_id: str,
        applicant: Dict[str, Any],
        sidebar: Optional[Dict[str, Any]],
        matched_policy: Optional[Dict[str, Any]],
        sales_opportunities: Any,
        insured_name: str,
        title: str,
        policy_num: Optional[str],
        safe_pol_num: str,
        target_carrier: str,
        instructions: str,
        requestor: Dict[str, Optional[str]],
        identity: Optional[str],
        note_id: Optional[str],
        discussion_id: Any,
        dry_run: bool,
        labels: Optional[List[str]] = None,
        combined_text: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Place primary then secondary Bland calls; mark the note processed once.

        Doom-loop guard: both dials run in this single pass. The note identity is
        marked processed only after every attempted dial (or after clarification
        when no E.164 exists). The watcher cannot double-fire the same noteId.

        Warm transfer is the account Assigned Producer DID, not the label
        invoker and not Sales Center producerName.

        Pathway copy comes only from Carlo's Manual WF Google Doc (or the
        Cancellation Notice PDF). ``renewal_reachout`` requires an explicit
        CSR phrase — never inferred from the daily renewal pipeline.
        """
        pathway = infer_outreach_pathway(
            combined_text or instructions,
            labels=labels,
        )
        targets = resolve_client_outreach_targets(
            applicant, sidebar, insured_name=insured_name
        )
        if not targets:
            logger.warning(
                "Applicant %s: Robie client outreach requested but no E.164 phone "
                "on primary or secondary.",
                applicant_id,
            )
            clarification_note = (
                f"Policy: #{safe_pol_num} ({target_carrier})\n\n"
                f"⚠️ [ROBIE CALL - PHONE NUMBER NEEDED]\n"
                f"Robie received your Robie client outreach request, but could not "
                f"determine an E.164 phone for the primary applicant or co-applicant "
                f"on '{insured_name}'. Numbers are never invented.\n\n"
                f"Add a Cell / Home / Work phone on the applicant (or co-applicant) "
                f"in EZLynx, then apply Robie client outreach again."
            )
            self._post_dispatcher_note(
                applicant_id=applicant_id,
                discussion_title=title,
                note_text=clarification_note,
                policy_number=policy_num,
                carrier_name=target_carrier,
            )
            if identity:
                self.processed_store.mark(
                    identity,
                    applicant_id=applicant_id,
                    discussion_id=discussion_id,
                    note_id=note_id,
                    status="CLARIFICATION_NEEDED",
                    dry_run=dry_run,
                )
            return {
                "applicant_id": applicant_id,
                "discussion_title": title,
                "target": insured_name,
                "status": "CLARIFICATION_NEEDED",
                "reason": "MISSING_PHONE_NUMBER",
                "call_type": CALL_TYPE_CLIENT_OUTREACH,
                "phones": [],
                "call_ids": [],
                "note_id": note_id,
                "note_identity": identity,
            }

        dials: List[Dict[str, Any]] = []
        last_dossier: Optional[CallingDossier] = None
        for target in targets:
            role = target["role"]
            phone = target["phone"]
            first_name = target.get("first_name")
            logger.info(
                "[ROBIE CLIENT OUTREACH] Applicant: %s (%s) | %s %s at %s | Policy: %s",
                applicant_id,
                insured_name,
                role,
                first_name or "unknown",
                phone,
                safe_pol_num,
            )
            dossier = CallingDossier(
                policy_number=safe_pol_num,
                insured_name=insured_name,
                carrier_name=target_carrier,
                carrier_phone=phone,
                line_of_business=(
                    (matched_policy or {}).get("lobName")
                    or (matched_policy or {}).get("LOB")
                    or (matched_policy or {}).get("lineOfBusiness")
                    or "Commercial Lines"
                ),
                applicant_id=applicant_id,
                custom_instructions=instructions,
                client_first_name=first_name,
                producer_name=None,
                requestor_name=requestor.get("name"),
                requestor_email=requestor.get("email"),
                call_type=CALL_TYPE_CLIENT_OUTREACH,
                outreach_pathway=pathway,
                assigned_csr_email="carlo@streetsmart.insurance",
            )
            self.hydrator.enrich_identity(
                dossier,
                applicant=applicant if role == "primary" else None,
                policy=matched_policy,
                sales_opportunities=None,
                sidebar=sidebar,
                call_type=CALL_TYPE_CLIENT_OUTREACH,
                requestor_name=requestor.get("name"),
                requestor_email=requestor.get("email"),
            )
            if role == "secondary":
                dossier.client_first_name = first_name
            dossier.producer_name = None
            dossier.carrier_phone = phone
            dossier.outreach_pathway = pathway
            last_dossier = dossier

            self.voice.build_call_prompt(
                dossier=dossier,
                custom_instructions=f"SPECIFIC CSR INSTRUCTIONS: {instructions}",
            )
            call_result = self.voice.dispatch_call(dossier=dossier, dry_run=dry_run)
            dials.append(
                {
                    "role": role,
                    "phone": phone,
                    "first_name": first_name,
                    "call_id": call_result.get("call_id", "sim_call_001"),
                    "status": call_result.get("status", "DISPATCHED"),
                }
            )

        transfer_line = (
            f"Warm transfer: enabled to Assigned Producer {last_dossier.assigned_producer_name} "
            f"at {last_dossier.assigned_producer_phone}."
            if last_dossier and last_dossier.assigned_producer_phone
            else (
                "Warm transfer: not available (account Assigned Producer must have an "
                "E.164 DID in data/voice_call_directory.json; will not fall back to the "
                "label invoker or Sales Center producer)."
            )
        )
        attempt_lines = "\n".join(
            f"• {dial['role'].title()} ({dial['first_name'] or 'unknown'}) at {dial['phone']} "
            f"— Call ID: {dial['call_id']}"
            for dial in dials
        )
        ack_note = (
            f"Policy: #{safe_pol_num} ({target_carrier})\n\n"
            f"🤖 [ROBIE AUTONOMOUS CALL DISPATCHED]\n"
            f"Robie has placed outbound client outreach call(s) in primary→secondary order:\n"
            f"{attempt_lines}\n"
            f"Call type: {CALL_TYPE_CLIENT_OUTREACH}\n"
            f"Outreach pathway: {pathway}\n"
            f"{transfer_line}\n"
            f"Caller ID: {self.voice.from_phone or '+1 (732) 298-6745'}\n"
            f"Instructions: \"{instructions}\"\n\n"
            f"When the call concludes, full audio recording and transcript will be posted here."
        )
        self._post_dispatcher_note(
            applicant_id=applicant_id,
            discussion_title=title,
            note_text=ack_note,
            policy_number=policy_num,
            carrier_name=target_carrier,
        )
        status = dials[-1]["status"] if dials else "DISPATCHED"
        if identity:
            self.processed_store.mark(
                identity,
                applicant_id=applicant_id,
                discussion_id=discussion_id,
                note_id=note_id,
                status=status,
                dry_run=dry_run,
            )
        return {
            "applicant_id": applicant_id,
            "discussion_title": title,
            "phone": dials[0]["phone"] if dials else None,
            "phones": [dial["phone"] for dial in dials],
            "target": insured_name,
            "call_id": dials[0]["call_id"] if dials else None,
            "call_ids": [dial["call_id"] for dial in dials],
            "dials": dials,
            "status": status,
            "call_type": CALL_TYPE_CLIENT_OUTREACH,
            "outreach_pathway": pathway,
            "producer_name": None,
            "assigned_producer_name": last_dossier.assigned_producer_name if last_dossier else None,
            "assigned_producer_phone": last_dossier.assigned_producer_phone if last_dossier else None,
            "requestor_name": last_dossier.requestor_name if last_dossier else requestor.get("name"),
            "requestor_phone": last_dossier.requestor_phone if last_dossier else None,
            "note_id": note_id,
            "note_identity": identity,
        }

    def _post_dispatcher_note(
        self,
        *,
        applicant_id: str,
        discussion_title: str,
        note_text: str,
        policy_number: Optional[str],
        carrier_name: Optional[str],
    ) -> None:
        """Post an ack/clarification note. Never apply a Robie Call trigger label."""
        self.ezlynx.add_note_to_discussion(
            applicant_id=applicant_id,
            discussion_title=discussion_title,
            note_text=note_text,
            policy_number=policy_number,
            carrier_name=carrier_name,
        )

    def process_watch_queue(
        self,
        dry_run: bool = False,
        watch_file: Optional[str] = None,
        extra_ids: Optional[Iterable[str]] = None,
    ) -> Dict[str, Any]:
        """Scan known applicants for fresh Robie Call labels (server cron entry)."""
        applicant_ids = collect_watch_applicant_ids(
            watch_file=watch_file,
            extra_ids=extra_ids,
        )
        results: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []
        for applicant_id in applicant_ids:
            try:
                results.extend(
                    self.process_applicant_notes_for_calls(applicant_id, dry_run=dry_run)
                )
            except Exception as exc:
                logger.error("Robie Call scan failed for applicant %s: %s", applicant_id, exc)
                errors.append({"applicant_id": str(applicant_id), "error": str(exc)})
        return {
            "applicants_scanned": len(applicant_ids),
            "applicant_ids": applicant_ids,
            "dispatched": results,
            "errors": errors,
            "dry_run": dry_run,
        }


def collect_watch_applicant_ids(
    watch_file: Optional[str] = None,
    extra_ids: Optional[Iterable[str]] = None,
) -> List[str]:
    """Applicant IDs from renewals.db + ROBIE_CALL_WATCH_APPLICANTS + optional file."""
    seen = set()
    ordered: List[str] = []

    def _add(raw: Optional[str]) -> None:
        value = str(raw or "").strip()
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)

    env_raw = os.getenv("ROBIE_CALL_WATCH_APPLICANTS") or ""
    for part in re.split(r"[,\s]+", env_raw):
        _add(part)

    path = watch_file or os.getenv("ROBIE_CALL_WATCH_FILE")
    if path:
        try:
            from pathlib import Path
            import json

            text = Path(path).read_text(encoding="utf-8")
            payload = json.loads(text) if text.strip().startswith(("{", "[")) else None
            if isinstance(payload, list):
                for item in payload:
                    if isinstance(item, dict):
                        _add(item.get("applicant_id") or item.get("applicantId"))
                    else:
                        _add(item)
            elif isinstance(payload, dict):
                for item in payload.get("applicant_ids") or payload.get("applicants") or []:
                    _add(item)
            else:
                for line in text.splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        _add(line)
        except Exception as exc:
            logger.warning("Could not read Robie Call watch file %s: %s", path, exc)

    try:
        from src.database.models import PolicyRenewal, RenewalStatus
        from src.database.session import SessionLocal

        excluded = {
            RenewalStatus.EXCLUDED_INACTIVE_ACCOUNT,
            RenewalStatus.EXCLUDED_TEST_ACCOUNT,
        }
        db = SessionLocal()
        try:
            rows = (
                db.query(PolicyRenewal.applicant_id)
                .filter(PolicyRenewal.applicant_id.isnot(None))
                .filter(~PolicyRenewal.status.in_(excluded))
                .distinct()
                .all()
            )
            for (applicant_id,) in rows:
                _add(applicant_id)
        finally:
            db.close()
    except Exception as exc:
        logger.warning("Could not load applicant IDs from renewals.db: %s", exc)

    if extra_ids:
        for item in extra_ids:
            _add(item)

    return ordered


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Scan EZLynx for 'Robie Call' labeled notes and dispatch calls.")
    parser.add_argument("--applicant-id", type=str, help="Specific applicant ID to scan in EZLynx")
    parser.add_argument("--scan-queue", action="store_true", help="Scan renewals.db + watch-list applicants (server cron)")
    parser.add_argument("--watch-file", type=str, default=None, help="Optional extra applicant ID JSON/list file")
    parser.add_argument("--test-note", type=str, help="Test raw note text directly from terminal")
    parser.add_argument("--phone", type=str, help="Override phone number for test calls (e.g., cell number)")
    parser.add_argument("--dry-run", action="store_true", help="Simulate call dispatch without hitting Bland AI ($0 cost)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    dispatcher = EZLynxLabelCallDispatcher()

    if args.test_note:
        print("\n=== TESTING 'Robie Call' NOTE PARSING & DISPATCH ===")
        parsed = parse_call_note_instructions(args.test_note)
        print("1. Parsed Input:")
        for k, v in parsed.items():
            print(f"   {k}: {v}")

        if not parsed["is_robie_call"]:
            print("\n❌ Note did not trigger 'Robie Call'. Ensure note includes 'Robie Call'.")
            return

        carrier = parsed["target_name"] or "The Hartford"
        phone = args.phone or parsed["phone_number"] or lookup_known_carrier_phone(carrier)
        pol = parsed["policy_number"] or "PWC1239278"
        instructions = parsed["instructions"] or "Inquire regarding renewal quote status."

        # Auto-enrich from EZLynx API if applicant_id is provided
        insured_name = "Test Insured LLC"
        applicant_id = args.applicant_id or "26356199"
        if args.applicant_id:
            app_res = dispatcher.ezlynx.get_applicant(args.applicant_id)
            if app_res.get("status") == "success":
                app_data = app_res.get("applicant", {})
                insured_name = (
                    app_data.get("BusinessName")
                    or f"{app_data.get('FirstName', '')} {app_data.get('LastName', '')}".strip()
                    or f"Applicant #{args.applicant_id}"
                )
                print(f"   [API Enriched Insured Name]: {insured_name}")
                if not args.phone and not parsed["phone_number"]:
                    if any(k in carrier.lower() for k in ["insured", "client", "carlo", "buster", "hartford"]):
                        app_phone = app_data.get("CellPhone") or app_data.get("BusinessPhone")
                        if app_phone and any(k in carrier.lower() for k in ["insured", "client", "carlo", "buster"]):
                            phone = normalize_phone_e164(app_phone)
                            print(f"   [API Enriched Phone from Applicant Profile]: {phone}")

            # If policy was not explicitly in note, resolve from applicant policies
            if not parsed["policy_number"]:
                pols = dispatcher.ezlynx.get_applicant_policies(args.applicant_id)
                if pols and isinstance(pols, list):
                    pol = pols[0].get("policyNumber") or pols[0].get("PolicyNumber") or pol
                    carrier = pols[0].get("carrierName") or pols[0].get("CarrierName") or carrier
                    print(f"   [API Enriched Policy]: {pol} ({carrier})")
                    if not phone:
                        phone = lookup_known_carrier_phone(carrier)

        print(f"\n2. Resolved Carrier: {carrier}")
        print(f"3. Resolved Phone: {phone} (Source: {'--phone override' if args.phone else 'Directory/Profile/Note'})")
        print(f"4. Associated Policy: {pol}")

        if not phone:
            print("\n⚠️ No phone number resolved. In production, Robie posts a clarification note to EZLynx.")
            return

        dossier = CallingDossier(
            policy_number=pol,
            insured_name=insured_name,
            carrier_name=carrier,
            carrier_phone=phone,
            line_of_business="Commercial Lines",
            applicant_id=applicant_id,
            custom_instructions=instructions,
        )

        prompt = dispatcher.voice.build_call_prompt(
            dossier=dossier,
            custom_instructions=f"SPECIFIC CSR INSTRUCTIONS: {instructions}",
        )
        print("\n5. Generated Voice Prompt:")
        print(f"   First Sentence: {dossier.carrier_name} Commercial Underwriting, this is Robie from StreetSmart Insurance calling regarding policy #{pol}.")
        print(f"   Objective: {instructions}")

        print(f"\n6. Dispatching Call ({'DRY RUN - SIMULATED' if args.dry_run else 'LIVE CALL'})...")
        res = dispatcher.voice.dispatch_call(dossier=dossier, dry_run=args.dry_run)
        print("   Result:", res)

        if not args.dry_run and res.get("status") in ("DISPATCHED", "SUCCESS"):
            call_id = res.get("call_id")
            dispatcher.ezlynx.add_note_to_discussion(
                applicant_id=applicant_id,
                discussion_title=f"Robie Voice AI Outbound | {pol}",
                note_text=(
                    f"Policy: #{pol} ({carrier})\n\n"
                    f"📞 [ROBIE CALL INITIATED]\n"
                    f"Robie placed an outbound call to {carrier} ({phone}) via Bland AI.\n"
                    f"• Call ID: {call_id}\n"
                    f"• Goal / Instructions: {instructions}\n"
                    f"• Status: Call in progress. Transcript and recording will post upon completion.\n"
                ),
                policy_number=pol,
                carrier_name=carrier,
            )
            print(f"   Audit note posted to EZLynx under applicant {applicant_id}.")

        print("\n✅ Test completed successfully.")

    elif args.applicant_id:
        res = dispatcher.process_applicant_notes_for_calls(args.applicant_id, dry_run=args.dry_run)
        print(f"Processed {len(res)} calls for applicant {args.applicant_id}: {res}")
    elif args.scan_queue:
        import json
        res = dispatcher.process_watch_queue(dry_run=args.dry_run, watch_file=args.watch_file)
        print(json.dumps(res, indent=2, default=str))
    else:
        print("Please provide --applicant-id, --scan-queue, or --test-note to run.")


if __name__ == "__main__":
    main()
