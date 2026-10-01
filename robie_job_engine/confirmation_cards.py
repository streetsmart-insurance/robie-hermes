"""Chat-native Approve/Reject for HITL plan confirmations.

The Confirmations sheet is an unauthenticated surface: anyone with edit
access can type APPROVE and any name. Decisions are authorized by an
HMAC-signed decision token (``confirmations.mint_decision_token`` /
``verify_decision_token``) minted for one confirmation, one decision, and
one principal.

This module puts that same token inside Google Chat card buttons, so Carlo
can approve or reject with one tap and no sheet paste:

- ``approval_card_v2`` -- the cardsV2 approval card. The face shows a
  policy line, then a short confirmation ref (the first 8 characters of
  the confirmation id, with an ellipsis when the id is longer), a job-type
  category, and a short description of what will run. The full
  evidence-loop job id is not placed on the card; it stays in logs. The
  Approve/Reject buttons carry the signed decision token in their action
  parameters. A bare click with no valid token fails closed
  (``resolve_confirmation_click`` refuses it); the button is NOT a typed
  "APPROVE".
- ``resolve_confirmation_click`` -- verify the token (signature, expiry,
  confirmation id, decision), require the clicking Chat user to BE the
  token's principal, then apply ``confirmations.approve`` / ``reject``.
  Already-decided confirmations resolve to ALREADY_DECIDED -- a double
  click is safe and never re-decides.
- ``confirmation_click_response`` -- the UPDATE_MESSAGE payload that
  replaces the card in Chat so it cannot be clicked again.

The designed click path is: button → Cloud Run ``robie-chat-http-bridge``
→ Pub/Sub topic ``hermes-chat-topic``. Buttons minted while ``ROBIE_ENV``
is TEST or Production also carry ``robie_env`` (``test`` or ``prod``).
The bridge copies that value onto the Pub/Sub attribute ``robie_env`` for
card clicks only, so each environment's subscription can filter to its
own clicks. Ordinary Chat messages are not tagged. A gateway that does
not have the confirmation id in its own database acks the click and does
not patch the card.

``action.function`` on new cards is the full bridge action URL
(``{bridge base}/actions/robie_confirmation_decision``). A bare function
name is NOT valid here: Google treats it as an add-on deployment function
and the click never reaches the bridge (this broke every real
Approve/Reject click on 2026-09-23). ``canonical_card_action`` also
accepts the bare name for envelopes that already carry it. The adapter
folds that URL to the bare name before it matches this action.

Fail-closed everywhere: no signing key, malformed/forged/expired token,
clicking user != token principal, or unknown confirmation all refuse and
leave the confirmation PENDING (or as already decided -- never flipped by
an invalid click).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping

logger = logging.getLogger(__name__)

from . import confirmations


#: Action name for confirmation clicks (also the bridge's
#: ``/actions/<name>`` path). The bridge echoes this as
#: ``common.invokedFunction`` on ``CARD_CLICKED``.
CARD_ACTION = "robie_confirmation_decision"


#: Base URL of the Chat HTTP bridge that receives card-button clicks.
#: Chat apps on an HTTP endpoint MUST put the full HTTPS action URL in
#: ``action.function``. A bare function name is treated by Google as an
#: add-on deployment function: the click never reaches the bridge. Every
#: real Approve/Reject click on 2026-09-23 failed exactly this way
#: (Google: "The Chat app didn't respond or its response was invalid").
#: Same env knob as ``integrations/google_chat/adapter.py``.
CARD_ACTION_BASE_URL = (
    os.environ.get(
        "GOOGLE_CHAT_CARD_ACTION_BASE_URL",
        "https://robie-chat-http-bridge-751771086524.us-east1.run.app",
    )
    .strip()
    .rstrip("/")
)


def _click_function() -> str:
    """Full action URL a Chat-app cardsV2 button must use.

    ``{bridge base}/actions/robie_confirmation_decision`` -- the URL the
    bridge receives real clicks on. A bare ``CARD_ACTION`` is wrong: Google
    treats it as an add-on deployment function and the click never
    reaches the HTTP endpoint.
    """
    return f"{CARD_ACTION_BASE_URL}/actions/{CARD_ACTION}"


def canonical_card_action(raw: str) -> str:
    """Return the bare action name for a confirmation click, or ``raw``.

    Accepts the full bridge URL (``.../actions/robie_confirmation_decision``)
    that current cards store in ``action.function``, and the bare name the
    bridge echoes as ``common.invokedFunction``. Any other string is
    returned unchanged so unrelated card actions keep their own names.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    bare = text.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if bare == CARD_ACTION or bare.endswith("/actions/" + CARD_ACTION):
        return CARD_ACTION
    return text


# ---------------------------------------------------------------------------
# Card rendering (cardsV2, posted via chat_app_post.post_card_as_chat_app)
# ---------------------------------------------------------------------------

def _decision_button_parameters(token: str) -> list[dict[str, str]]:
    """Token plus the owning environment, when this process knows it.

    ``robie_env`` is how the shared bridge routes the click to one gateway.
    Unset ``ROBIE_ENV`` omits it; those clicks stay untagged.
    """
    from robie_job_engine.runtime_env import chat_routing_env

    parameters = [{"key": "decision_token", "value": str(token)}]
    env = chat_routing_env()
    if env:
        parameters.append({"key": "robie_env", "value": env})
    return parameters


#: Plain labels for job types the shared confirmation map does not name.
#: Anything else is turned into words by ``_category_label``.
_EXTRA_CATEGORY_LABELS = {
    "ezlynx.commercial_auto": "Commercial auto",
    "ezlynx.policy_setup": "Policy setup",
    "ezlynx.document_upload": "Document upload",
    "ascend.create_program": "Ascend program",
    "manual_renewal_verification": "Manual renewal check",
}

_FIELD_PHRASES = {
    "writtenPremium": "written premium",
    "written_premium": "written premium",
    "fullTermPremium": "full-term premium",
    "full_term_premium": "full-term premium",
    "policyStatus": "policy status",
    "policy_status": "policy status",
    "expirationDate": "expiration date",
    "expiration_date": "expiration date",
    "effectiveDate": "effective date",
    "effective_date": "effective date",
    "policyNumber": "policy number",
    "policy_number": "policy number",
}

_VENDOR_PREFIXES = frozenset({"ezlynx", "hermes", "ascend"})


def _without_full_ids(text: str, *blocked: str) -> str:
    """Drop full confirmation and loop-job ids from user-visible card text."""
    cleaned = str(text or "")
    for blocked_id in blocked:
        token = str(blocked_id or "").strip()
        if len(token) < 8:
            continue
        cleaned = cleaned.replace(token, "")
    return " ".join(cleaned.split())


def _sentence(text: str) -> str:
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return ""
    if cleaned[-1] not in ".!?":
        cleaned += "."
    return cleaned[0].upper() + cleaned[1:]


def _text_widget(text: str) -> dict[str, Any] | None:
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return None
    return {"textParagraph": {"text": cleaned}}


def _confirmation_ref_display(confirmation_id: str) -> str:
    """Card face ref: first 8 characters, with an ellipsis when truncated.

    Logs keep the same 8 characters without the ellipsis
    (``_short_confirmation_ref``). The sheet stores the full confirmation id.
    """
    short = _short_confirmation_ref(confirmation_id)
    text = str(confirmation_id or "").strip()
    if not text or short == "-":
        return "-"
    if len(text) > 8:
        return short + "…"
    return short


def _category_label(job_type: str) -> str:
    key = str(job_type or "").strip()
    if not key:
        return "Plan confirmation"
    known = confirmations._JOB_TYPE_LABELS.get(key) or _EXTRA_CATEGORY_LABELS.get(key)
    if known:
        return known
    words = [
        word
        for word in key.replace(".", " ").replace("_", " ").replace("-", " ").split()
        if word
    ]
    if len(words) > 1 and words[0].casefold() in _VENDOR_PREFIXES:
        words = words[1:]
    if not words:
        return "Plan confirmation"
    head = words[0][:1].upper() + words[0][1:]
    tail = [word.lower() for word in words[1:]]
    return " ".join([head, *tail])


def _policy_number(record: Mapping[str, Any]) -> str:
    changes = confirmations._parse_changes(record)
    for key in ("policy_number", "policyNumber"):
        raw = changes.get(key)
        if isinstance(raw, (Mapping, list)):
            continue
        value = str(raw or "").strip()
        if value:
            return value
    nested = changes.get("changes")
    if isinstance(nested, Mapping):
        for key in ("policy_number", "policyNumber"):
            raw = nested.get(key)
            if isinstance(raw, (Mapping, list)):
                continue
            value = str(raw or "").strip()
            if value:
                return value
    return ""


def _policy_line(record: Mapping[str, Any], blocked: tuple[str, ...]) -> str:
    policy = _without_full_ids(_policy_number(record), *blocked)
    if not policy:
        return "Policy: not listed on this request"
    return f"Policy: {policy}"


def _field_phrase(name: str) -> str:
    key = str(name or "").strip()
    if key in _FIELD_PHRASES:
        return _FIELD_PHRASES[key]
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", key)
    spaced = spaced.replace("_", " ").replace(".", " ").replace("-", " ")
    words = [word.lower() for word in spaced.split() if word]
    return " ".join(words) or "that field"


def _plain_value(value: Any, blocked: tuple[str, ...]) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, (Mapping, list)):
        return ""
    return _without_full_ids(str(value), *blocked)


def _change_phrase(field: Any, value: Any, blocked: tuple[str, ...]) -> str:
    if str(field or "").strip() in {"policy_number", "policyNumber"}:
        return ""
    label = _field_phrase(str(field))
    if isinstance(value, Mapping):
        new = value.get("new", value.get("to", value.get("value")))
        old = value.get("old", value.get("from"))
        new_text = _plain_value(new, blocked) if new is not None else ""
        old_text = _plain_value(old, blocked) if old is not None else ""
        if old_text and new_text:
            return f"change {label} from {old_text} to {new_text}"
        if new_text:
            return f"set {label} to {new_text}"
        return ""
    if isinstance(value, str) and not str(field).strip():
        text = _without_full_ids(value, *blocked)
        if not text or len(text) > 120:
            return ""
        return text
    text = _plain_value(value, blocked)
    if not text:
        return ""
    return f"set {label} to {text}"


def _join_and(parts: list[str]) -> str:
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return ", ".join(parts[:-1]) + f", and {parts[-1]}"


def _describe_changes(record: Mapping[str, Any], blocked: tuple[str, ...]) -> str:
    changes = confirmations._parse_changes(record)
    change_map = changes.get("changes")
    phrases: list[str] = []
    if isinstance(change_map, Mapping):
        items = list(change_map.items())
    elif isinstance(change_map, list):
        items = [("", entry) for entry in change_map]
    else:
        items = []
    for field, value in items:
        phrase = _change_phrase(field, value, blocked)
        if phrase:
            phrases.append(phrase)
    if not phrases:
        return ""
    shown = phrases[:3]
    extra = len(phrases) - len(shown)
    if extra:
        shown.append(f"{extra} more change{'s' if extra != 1 else ''}")
    return _join_and(shown)


def _job_run_description(
    record: Mapping[str, Any],
    blocked: tuple[str, ...],
    category: str,
) -> str:
    """One short sentence describing the work an approval will allow."""
    draft = str(record.get("draft_summary") or "").strip()
    one_line = ""
    if draft and "\n" not in draft and not draft.startswith(("{", "[")):
        one_line = _without_full_ids(draft, *blocked)
        if len(one_line) > 220:
            one_line = ""
    if one_line:
        return _sentence(one_line)
    from_changes = _describe_changes(record, blocked)
    if from_changes:
        return _sentence(from_changes)
    label = category[:1].lower() + category[1:] if category else "request"
    return _sentence(f"run this {label} after you approve")


def approval_card_v2(
    record: Mapping[str, Any],
    tokens: Mapping[str, str] | None,
) -> dict[str, Any]:
    """Build the cardsV2 approval card for a confirmation record.

    When ``tokens`` (APPROVE/REJECT, from
    ``confirmation_notify.mint_approver_tokens``) is provided, the buttons
    carry the signed tokens. When ``tokens`` is None (no signing key), the
    card is display-only: no buttons, and the text says approvals cannot
    be taken until the key is configured.

    The card face, top to bottom: a policy line, the short confirmation
    ref directly under it, the job-type category, a short description of
    what will run, then the existing status sentence. Button parameters
    are unchanged.
    """
    record = dict(record or {})
    confirmation_id = str(record.get("id") or "").strip()
    if not confirmation_id:
        raise ValueError("record needs an id to build an approval card")
    loop_job_id = str(record.get("loop_job_id") or "").strip()
    blocked = (loop_job_id, confirmation_id)
    category = _without_full_ids(
        _category_label(str(record.get("job_type") or "")),
        *blocked,
    ) or "Plan confirmation"
    description = _job_run_description(record, blocked, category)
    summary = confirmations.confirmation_summary(record)
    raw_type = str(record.get("job_type") or "").strip()
    if raw_type and raw_type != category and len(raw_type) >= 3:
        summary = summary.replace(raw_type, category)
        description = description.replace(raw_type, category)
    summary = _without_full_ids(summary, *blocked)
    description = _without_full_ids(description, *blocked)
    logger.info(
        "confirmation_cards.render ref=%s confirmation_id=%s loop_job_id=%s category=%s",
        _short_confirmation_ref(confirmation_id),
        confirmation_id,
        loop_job_id or "-",
        category,
    )
    widgets: list[dict[str, Any]] = []
    for text in (
        "ROBIE needs your approval.",
        _policy_line(record, blocked),
        f"Confirmation ref: {_confirmation_ref_display(confirmation_id)}",
        f"Category: {category}",
        f"What this will do: {description}",
        summary,
    ):
        widget = _text_widget(text)
        if widget:
            widgets.append(widget)
    widgets.append({"divider": {}})
    if tokens and tokens.get("APPROVE") and tokens.get("REJECT"):
        function = _click_function()
        widgets.append(
            {
                "buttonList": {
                    "buttons": [
                        {
                            "text": "Approve",
                            "onClick": {
                                "action": {
                                    "function": function,
                                    "parameters": _decision_button_parameters(
                                        tokens["APPROVE"]
                                    ),
                                }
                            },
                        },
                        {
                            "text": "Reject",
                            "onClick": {
                                "action": {
                                    "function": function,
                                    "parameters": _decision_button_parameters(
                                        tokens["REJECT"]
                                    ),
                                }
                            },
                        },
                    ]
                }
            }
        )
        widgets.append(
            {
                "textParagraph": {
                    "text": (
                        "Tap Approve or Reject above. The button carries a "
                        "signed approval for this exact request -- a typed "
                        "word is not an approval."
                    )
                }
            }
        )
    else:
        widgets.append(
            {
                "textParagraph": {
                    "text": (
                        "This request is display-only right now (no decision "
                        "signing key configured), so the decision cannot be "
                        "taken from Chat until the key is set."
                    )
                }
            }
        )
    return {
        "cardId": f"robie-confirmation-{confirmation_id}",
        "card": {
            "header": {
                "title": "ROBIE needs your approval",
                "subtitle": category,
            },
            "sections": [{"widgets": widgets}],
        },
    }


# ---------------------------------------------------------------------------
# Click parsing + resolution
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConfirmationClickResult:
    """Outcome of resolving a Chat approval-button click."""

    status: str  # APPROVED | REJECTED | ALREADY_DECIDED | UNAUTHORIZED |
    # EXPIRED_TOKEN | INVALID_TOKEN | NO_KEY | INVALID
    decided: bool
    confirmation_id: str
    message: str


def _click_parameters(payload: Mapping[str, Any]) -> dict[str, str]:
    """Read action parameters from a Chat card-click envelope.

    Handles both the raw Chat API shape (``common.parameters`` as a dict,
    ``action.parameters`` as a [{key, value}] list) and the bridge-normalized
    shape (``common.invokedFunction`` set from the URL action name).
    """
    payload = dict(payload or {})
    common = payload.get("common") or {}
    if not isinstance(common, dict):
        common = {}
    direct = common.get("parameters") or payload.get("parameters")
    if isinstance(direct, dict) and direct:
        return {str(k): str(v) for k, v in direct.items()}
    # Fall through to the raw Chat API [{key, value}] list shape below.
    action = payload.get("action") or {}
    result: dict[str, str] = {}
    items = action.get("parameters") if isinstance(action, dict) else None
    for item in items or []:
        if isinstance(item, dict) and item.get("key") is not None:
            result[str(item["key"])] = str(item.get("value") or "")
    return result


def _click_action(payload: Mapping[str, Any]) -> str:
    payload = dict(payload or {})
    common = payload.get("common") or {}
    if not isinstance(common, dict):
        common = {}
    action = payload.get("action") or {}
    if not isinstance(action, dict):
        action = {}
    return canonical_card_action(
        str(
            common.get("invokedFunction")
            or action.get("actionMethodName")
            or payload.get("actionMethodName")
            or ""
        )
    )


def _click_actor(payload: Mapping[str, Any]) -> str:
    """The authenticated Chat user who clicked, lowercased email.

    Live 2026-09-23: for this Chat app, Workspace Add-ons card clicks carry
    the clicking user at ``chat.user``. ``commonEventObject`` has no ``user``
    key, and the bridge's top-level ``user`` is the message sender -- the
    bot that posted the card -- so it must not shadow the true clicker.
    Prefer the add-on clicker locations first, then fall back to the native
    Chat API top-level ``user``.
    """
    payload = dict(payload or {})
    common = payload.get("common") or {}
    if not isinstance(common, dict):
        common = {}
    chat = payload.get("chat") or {}
    if not isinstance(chat, dict):
        chat = {}
    for candidate in (
        common.get("user"),
        chat.get("user"),
        payload.get("user"),
    ):
        if isinstance(candidate, dict):
            actor = str(candidate.get("email") or candidate.get("name") or "").strip().lower()
            if actor:
                return actor
    return ""


def parse_confirmation_click(payload: Mapping[str, Any]) -> tuple[str, str]:
    """Extract (decision_token, actor) from a card-click envelope.

    Raises ValueError when the envelope is not a confirmation-decision
    click or is missing the token / actor identity. A click with no token
    (e.g. a forged button) never parses -- it fails closed downstream.
    """
    action = _click_action(payload)
    if action != CARD_ACTION:
        raise ValueError(
            f"not a confirmation decision click (action={action!r})"
        )
    token = _click_parameters(payload).get("decision_token", "").strip()
    actor = _click_actor(payload)
    if not token:
        raise ValueError("confirmation click carries no decision token")
    if not actor:
        raise ValueError("confirmation click carries no user identity")
    return token, actor


def _short_confirmation_ref(confirmation_id: str) -> str:
    text = str(confirmation_id or "").strip()
    if not text:
        return "-"
    return text[:8]


def _actor_domain(actor: str) -> str:
    """Mail domain only. The local part is not written to the log."""
    text = str(actor or "").strip().lower()
    if "@" not in text:
        return "-"
    domain = text.rsplit("@", 1)[1].strip().rstrip(".")
    if not domain or any(ch.isspace() for ch in domain):
        return "-"
    return domain


def _log_confirmation_click(
    payload: Mapping[str, Any],
    result: ConfirmationClickResult,
    *,
    decision: str,
) -> None:
    action = decision if decision in {"APPROVE", "REJECT"} else (_click_action(payload) or "-")
    logger.info(
        "confirmation_cards.click ref=%s action=%s click.status=%s actor_domain=%s",
        _short_confirmation_ref(result.confirmation_id),
        action,
        result.status,
        _actor_domain(_click_actor(payload)),
    )


INACTIVE_CARD_TEXT = "This card is no longer active."


def already_decided_text(record: Mapping[str, Any]) -> str:
    """Plain-English line for a click that did not change the decision."""
    status = str((record or {}).get("status") or "").strip().lower()
    if status == "approved":
        return (
            "This request was already approved. The extra click changed nothing."
        )
    if status == "rejected":
        return (
            "This request was already rejected. The extra click changed nothing."
        )
    if status == "expired":
        return "This request already expired. The extra click changed nothing."
    return "This request was already decided. The extra click changed nothing."


def text_for_card_update(db_path: str, payload: Mapping[str, Any], proposed: str) -> str:
    """Re-read the confirmation immediately before the Chat card update.

    A sentence that claims this click approved or rejected is kept only
    when the stored status still says that. Any other terminal status
    becomes the already-decided sentence, so the card is not left on
    Processing and does not announce a decision that lost the race.

    Read-only. A missing database, table, or row leaves ``proposed``
    unchanged and creates no schema.
    """
    token = _click_parameters(payload).get("decision_token", "")
    confirmation_id = confirmations.peek_confirmation_id(token)
    if not confirmation_id:
        return proposed
    status = confirmations.read_confirmation_status(db_path, confirmation_id)
    if not status or status == "PENDING":
        return proposed
    if status == "APPROVED" and str(proposed or "").startswith("Approved."):
        return proposed
    if status == "REJECTED" and str(proposed or "").startswith("Rejected."):
        return proposed
    return already_decided_text({"status": status})


def resolve_confirmation_click(
    store: Any,
    payload: Mapping[str, Any],
    *,
    decision_key: Any = None,
) -> ConfirmationClickResult:
    """Resolve a Chat approval-button click. Fail closed, idempotent.

    1. Parse the click (action, token, actor) -- unparseable clicks refuse.
    2. Verify the token signature + expiry. No signing key, forged, or
       expired tokens refuse; the confirmation is untouched.
    3. Require the clicking user to BE the token's principal. A token
       clicked by anyone else refuses -- seeing the button is not an
       authorization.
    4. Apply approve/reject. ``confirmations`` only decides PENDING rows,
       so a second click on an already-decided confirmation resolves to
       ALREADY_DECIDED instead of re-deciding (double-submit safe).
    """
    decision = ""

    def finish(result: ConfirmationClickResult) -> ConfirmationClickResult:
        _log_confirmation_click(payload, result, decision=decision)
        return result

    try:
        token, actor = parse_confirmation_click(payload)
    except ValueError as exc:
        return finish(ConfirmationClickResult(
            status="INVALID",
            decided=False,
            confirmation_id="",
            message=f"That approval button could not be read ({exc}). Nothing was decided.",
        ))
    try:
        verified = confirmations.verify_decision_token(token, key=decision_key)
    except RuntimeError:
        return finish(ConfirmationClickResult(
            status="NO_KEY",
            decided=False,
            confirmation_id="",
            message=(
                "Approvals are not configured right now (no decision signing "
                "key). Nothing was decided."
            ),
        ))
    except ValueError as exc:
        text = str(exc).lower()
        if "expired" in text:
            return finish(ConfirmationClickResult(
                status="EXPIRED_TOKEN",
                decided=False,
                confirmation_id="",
                message="That approval button has expired. Nothing was decided.",
            ))
        return finish(ConfirmationClickResult(
            status="INVALID_TOKEN",
            decided=False,
            confirmation_id="",
            message="That approval button is not valid. Nothing was decided.",
        ))
    principal = str(verified["principal"] or "").strip().lower()
    confirmation_id = str(verified["confirmation_id"] or "").strip()
    decision = str(verified["decision"] or "").strip().upper()
    if not actor or actor != principal:
        return finish(ConfirmationClickResult(
            status="UNAUTHORIZED",
            decided=False,
            confirmation_id=confirmation_id,
            message=(
                "That approval button is bound to a different approver. "
                "Nothing was decided."
            ),
        ))
    try:
        if decision == "APPROVE":
            confirmations.approve(confirmation_id, decided_by=principal, store=store)
        elif decision == "REJECT":
            confirmations.reject(
                confirmation_id,
                decided_by=principal,
                reason="rejected from Chat",
                store=store,
            )
        else:  # verify_decision_token only allows APPROVE/REJECT; defensive
            raise ValueError(f"token carries unexpected decision {decision!r}")
    except ValueError as exc:
        record = confirmations.get(confirmation_id, store=store)
        if record is not None and str(record.get("status")) != "PENDING":
            return finish(ConfirmationClickResult(
                status="ALREADY_DECIDED",
                decided=False,
                confirmation_id=confirmation_id,
                message=already_decided_text(record),
            ))
        return finish(ConfirmationClickResult(
            status="INVALID",
            decided=False,
            confirmation_id=confirmation_id,
            message=f"That approval could not be applied ({exc}). Nothing was decided.",
        ))
    if decision == "APPROVE":
        return finish(ConfirmationClickResult(
            status="APPROVED",
            decided=True,
            confirmation_id=confirmation_id,
            message="Approved. ROBIE recorded your decision and the request is no longer pending.",
        ))
    return finish(ConfirmationClickResult(
        status="REJECTED",
        decided=True,
        confirmation_id=confirmation_id,
        message="Rejected. ROBIE recorded your decision and stopped that request.",
    ))


def confirmation_click_response(result: ConfirmationClickResult) -> dict[str, Any]:
    """UPDATE_MESSAGE payload: replace the card with the outcome text.

    The answered card is replaced (``cardsV2`` cleared) so it cannot be
    clicked again; a repeat click that still arrives resolves idempotently
    in ``resolve_confirmation_click``.
    """
    return {
        "actionResponse": {"type": "UPDATE_MESSAGE"},
        "text": result.message,
        "cardsV2": [],
    }
