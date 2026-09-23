"""Chat-native Approve/Reject for HITL plan confirmations.

The Confirmations sheet is an unauthenticated surface: anyone with edit
access can type APPROVE and any name. Decisions are authorized by an
HMAC-signed decision token (``confirmations.mint_decision_token`` /
``verify_decision_token``) minted for one confirmation, one decision, and
one principal.

This module puts that same token inside Google Chat card buttons, so Carlo
can approve or reject with one tap and no sheet paste:

- ``approval_card_v2`` -- the cardsV2 approval card. The Approve/Reject
  buttons carry the signed decision token in their action parameters. A
  bare click with no valid token fails closed (``resolve_confirmation_click``
  refuses it); the button is NOT a typed "APPROVE".
- ``resolve_confirmation_click`` -- verify the token (signature, expiry,
  confirmation id, decision), require the clicking Chat user to BE the
  token's principal, then apply ``confirmations.approve`` / ``reject``.
  Already-decided confirmations resolve to ALREADY_DECIDED -- a double
  click is safe and never re-decides.
- ``confirmation_click_response`` -- the UPDATE_MESSAGE payload that
  replaces the card in Chat so it cannot be clicked again.

The designed click path is: button → Cloud Run ``robie-chat-http-bridge``
→ Pub/Sub topic ``hermes-chat-topic`` → the Hermes gateway that is
actually subscribed. ``hermes-test-01`` does not see the click while its
gateway has no messaging platform enabled. A shared subscription with
Production lets Production answer instead. Enabling Test Chat, on a
Test-only ``GOOGLE_CHAT_SUBSCRIPTION_NAME``, is configuration for Dusty
and Ralph — not something this module turns on.

``action.function`` on new cards is the full bridge action URL
(``{bridge base}/actions/robie_confirmation_decision``). A bare function
name is NOT valid here: Google treats it as an add-on deployment function
and the click never reaches the bridge (this broke every real
Approve/Reject click on 2026-09-23). ``canonical_card_action`` also
accepts the bare name for envelopes that already carry it. The adapter
folds that URL to the bare name before the "That action is not
supported." branch.

Fail-closed everywhere: no signing key, malformed/forged/expired token,
clicking user != token principal, or unknown confirmation all refuse and
leave the confirmation PENDING (or as already decided -- never flipped by
an invalid click).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

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
    """
    record = dict(record or {})
    confirmation_id = str(record.get("id") or "").strip()
    if not confirmation_id:
        raise ValueError("record needs an id to build an approval card")
    summary = confirmations.confirmation_summary(record)
    widgets: list[dict[str, Any]] = [
        {"textParagraph": {"text": "ROBIE needs your approval."}},
        {"textParagraph": {"text": summary}},
    ]
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
                                    "parameters": [
                                        {
                                            "key": "decision_token",
                                            "value": str(tokens["APPROVE"]),
                                        }
                                    ],
                                }
                            },
                        },
                        {
                            "text": "Reject",
                            "onClick": {
                                "action": {
                                    "function": function,
                                    "parameters": [
                                        {
                                            "key": "decision_token",
                                            "value": str(tokens["REJECT"]),
                                        }
                                    ],
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
    job_type = str(record.get("job_type") or "").strip()
    return {
        "cardId": f"robie-confirmation-{confirmation_id}",
        "card": {
            "header": {
                "title": "ROBIE needs your approval",
                "subtitle": job_type or "plan confirmation",
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
    try:
        token, actor = parse_confirmation_click(payload)
    except ValueError as exc:
        return ConfirmationClickResult(
            status="INVALID",
            decided=False,
            confirmation_id="",
            message=f"That approval button could not be read ({exc}). Nothing was decided.",
        )
    try:
        verified = confirmations.verify_decision_token(token, key=decision_key)
    except RuntimeError:
        return ConfirmationClickResult(
            status="NO_KEY",
            decided=False,
            confirmation_id="",
            message=(
                "Approvals are not configured right now (no decision signing "
                "key). Nothing was decided."
            ),
        )
    except ValueError as exc:
        text = str(exc).lower()
        if "expired" in text:
            return ConfirmationClickResult(
                status="EXPIRED_TOKEN",
                decided=False,
                confirmation_id="",
                message="That approval button has expired. Nothing was decided.",
            )
        return ConfirmationClickResult(
            status="INVALID_TOKEN",
            decided=False,
            confirmation_id="",
            message="That approval button is not valid. Nothing was decided.",
        )
    principal = str(verified["principal"] or "").strip().lower()
    confirmation_id = str(verified["confirmation_id"] or "").strip()
    decision = str(verified["decision"] or "").strip().upper()
    if not actor or actor != principal:
        return ConfirmationClickResult(
            status="UNAUTHORIZED",
            decided=False,
            confirmation_id=confirmation_id,
            message=(
                "That approval button is bound to a different approver. "
                "Nothing was decided."
            ),
        )
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
            return ConfirmationClickResult(
                status="ALREADY_DECIDED",
                decided=False,
                confirmation_id=confirmation_id,
                message=(
                    f"This request was already {str(record['status']).lower()}; "
                    "the second click changed nothing."
                ),
            )
        return ConfirmationClickResult(
            status="INVALID",
            decided=False,
            confirmation_id=confirmation_id,
            message=f"That approval could not be applied ({exc}). Nothing was decided.",
        )
    if decision == "APPROVE":
        return ConfirmationClickResult(
            status="APPROVED",
            decided=True,
            confirmation_id=confirmation_id,
            message="Approved. ROBIE recorded your decision and the request is no longer pending.",
        )
    return ConfirmationClickResult(
        status="REJECTED",
        decided=True,
        confirmation_id=confirmation_id,
        message="Rejected. ROBIE recorded your decision and stopped that request.",
    )


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
