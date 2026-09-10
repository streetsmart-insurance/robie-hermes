"""EZLynx Directory action resolver — source of truth for how Robie reaches carriers.

Aligns with the Directory scraper (`ezlynx_full_extracted_directory.json` /
`crawl_ezlynx_directory.py`), NOT the Carrier Login Google Sheet.

Canonical actions (Manual Renewals, policy change, audits, doc retrieval):
  - document_download
  - policy_change
  - loss_runs
  - claims
  - renewals
  - endorsements
  - portal_docs

If a required contact/channel is missing → return pending + email_rep (and
optionally call_rep) so the job can pend for a response instead of guessing.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("ezlynx_directory_resolver")

BASE_DIR = Path(__file__).resolve().parents[2]
EXTRACTED_PATH = BASE_DIR / "data" / "ezlynx_full_extracted_directory.json"
ROUTING_PATH = BASE_DIR / "data" / "carrier_directory.json"

# title/name needles → canonical action
ACTION_NEEDLES: Dict[str, Tuple[str, ...]] = {
    "document_download": (
        "document download",
        "documents",
        "download department",
        "agency download",
        "dec page",
        "declarations",
    ),
    "policy_change": (
        "policy change",
        "policy changes",
        "endorsement",
        "endorsements",
        "service center",
        "client service",
        "customer service",
    ),
    "loss_runs": ("loss run", "loss runs", "lossrun"),
    "claims": ("claim", "claims"),
    "renewals": ("renewal", "renewals", "underwriter", "underwriting"),
    "endorsements": ("endorsement", "endorsements"),
    "portal_docs": ("portal", "website how to", "agent portal", "eselect", "foragentsonly"),
}

REQUIRED_ACTIONS_BY_WORKFLOW = {
    "manual_renewal": ("document_download", "renewals", "loss_runs"),
    "policy_change": ("policy_change", "document_download"),
    "audit": ("renewals", "document_download"),
    "doc_retrieval": ("document_download",),
}


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Failed to load %s: %s", path, e)
        return {}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def match_carrier_entry(carrier_name: str, extracted: Optional[Dict[str, Any]] = None) -> Tuple[str, Dict[str, Any]]:
    extracted = extracted if extracted is not None else _load_json(EXTRACTED_PATH)
    if not carrier_name:
        return "", {}
    if carrier_name in extracted:
        return carrier_name, extracted[carrier_name]
    needle = _norm(carrier_name)
    for key, payload in extracted.items():
        k = _norm(key)
        if needle in k or k in needle:
            return key, payload
        dname = _norm((payload.get("directory") or {}).get("name", ""))
        if needle in dname or dname in needle:
            return key, payload
    return "", {}


def _contact_blob(c: Dict[str, Any]) -> str:
    return _norm(" ".join(str(c.get(k) or "") for k in ("title", "name", "email", "phone")))


def resolve_action_contacts(carrier_name: str, action: str) -> Dict[str, Any]:
    """Return best Directory contacts for a canonical action."""
    matched_name, payload = match_carrier_entry(carrier_name)
    directory = (payload or {}).get("directory") or {}
    contacts = (payload or {}).get("contacts") or []
    needles = ACTION_NEEDLES.get(action) or (action.replace("_", " "),)

    hits: List[Dict[str, Any]] = []
    for c in contacts:
        blob = _contact_blob(c)
        if any(n in blob for n in needles):
            hits.append(
                {
                    "name": c.get("name") or "",
                    "title": c.get("title") or "",
                    "email": c.get("email") or "",
                    "phone": c.get("phone") or "",
                    "action": action,
                }
            )

    routing = _load_json(ROUTING_PATH)
    route = {}
    _, route_payload = match_carrier_entry(carrier_name, routing) if False else ("", {})
    # routing file is keyed by carrier display names directly
    for key, conf in routing.items():
        if _norm(key) in _norm(carrier_name) or _norm(carrier_name) in _norm(key):
            route = conf
            break

    primary = next((h for h in hits if h.get("email")), hits[0] if hits else None)
    missing = primary is None or not (primary.get("email") or primary.get("phone"))

    result = {
        "carrier_input": carrier_name,
        "matched_directory_name": matched_name or None,
        "directory_id": directory.get("id"),
        "action": action,
        "channel": (route or {}).get("channel") or ("PORTAL" if directory.get("url") else "EMAIL"),
        "portal_url": (route or {}).get("portal_url") or directory.get("url") or None,
        "underwriter_email": (route or {}).get("underwriter_email") or directory.get("email") or None,
        "contacts": hits,
        "primary": primary,
        "missing": missing,
        "pending": missing,
        "next_step": None,
    }
    if missing:
        result["next_step"] = {
            "type": "email_rep_and_pend",
            "pend_business_days": 3,  # Carlo: pend 2-3 days
            "pend_min_days": 2,
            "pend_max_days": 3,
            "also_call": True,  # call if no reply after pend window
            "call_after_pend": True,
            "ask": [
                f"Who handles {action.replace('_', ' ')} for StreetSmart?",
                "Name, email, phone",
                "Do you have an agent portal for document retrieval / policy service? URL + add-user path for robie@streetsmart.insurance",
            ],
            "to_guess": result.get("underwriter_email"),
            "reason": f"Directory missing usable contact for action={action}",
            "playbook": (
                "1) Email Directory/UW rep with ask list. "
                "2) Pend job 2-3 business days for reply. "
                "3) If still missing, call (+ also_call) and note outcome. "
                "4) Write contacts into EZLynx Directory titles matching action map; re-enrich carrier_directory.json."
            ),
        }
    return result


def resolve_workflow(carrier_name: str, workflow: str) -> Dict[str, Any]:
    """Resolve all required actions for a workflow; pend if any required missing."""
    required = REQUIRED_ACTIONS_BY_WORKFLOW.get(workflow) or ()
    actions = {a: resolve_action_contacts(carrier_name, a) for a in required}
    missing = [a for a, r in actions.items() if r.get("missing")]
    return {
        "carrier_input": carrier_name,
        "workflow": workflow,
        "actions": actions,
        "missing_actions": missing,
        "ready": not missing,
        "pend": bool(missing),
        "next_step": actions[missing[0]]["next_step"] if missing else None,
    }


def enrich_routing_from_directory(carrier_name: str) -> Dict[str, Any]:
    """Upsert hermes carrier_directory.json from extracted Directory (no passwords)."""
    from src.portals.carrier_routing import CarrierRoutingMatrix  # type: ignore

    matched, payload = match_carrier_entry(carrier_name)
    directory = (payload or {}).get("directory") or {}
    renew = resolve_action_contacts(carrier_name, "renewals")
    docs = resolve_action_contacts(carrier_name, "document_download")
    email = (
        (renew.get("primary") or {}).get("email")
        or (docs.get("primary") or {}).get("email")
        or directory.get("email")
    )
    portal = directory.get("url") or None
    channel = "PORTAL" if portal and not docs.get("missing") and "download" in _norm(
        ((docs.get("primary") or {}).get("title") or "") + ((docs.get("primary") or {}).get("name") or "")
    ) else ("EMAIL_ASK_PORTAL" if docs.get("missing") or renew.get("missing") else "EMAIL")
    notes = (
        f"Directory scraper-backed. matched={matched or carrier_name}. "
        f"docs_missing={docs.get('missing')} renewals_missing={renew.get('missing')}. "
        "If missing: email rep + pend (+ call). Robie was here."
    )
    return CarrierRoutingMatrix.update_carrier(
        matched or carrier_name,
        channel=channel,
        portal_url=portal,
        underwriter_email=email,
        notes=notes,
    )


if __name__ == "__main__":
    import sys

    carrier = sys.argv[1] if len(sys.argv) > 1 else "Selective Insurance"
    wf = sys.argv[2] if len(sys.argv) > 2 else "policy_change"
    print(json.dumps(resolve_workflow(carrier, wf), indent=2))
