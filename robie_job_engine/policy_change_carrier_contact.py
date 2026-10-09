"""Phase 2 of the 4359 program: contact the CARRIER directly.

For open policy changes the CSR hasn't progressed, this worker:

  1. Checks EZLynx documents via the read-only DocumentApi FIRST — if the
     endorsement already landed, it hands the change to phase 3's
     confirmation instead of bothering the carrier.
  2. Resolves the carrier from the 4359 "Master Company" against the
     curated routing table (carrier_policy_change_routes.py). No confident
     match, or no route in the directory -> the change goes to the
     awaiting-directory queue: reported, never emailed, never guessed.
  3. Emails the carrier's policy-change address (email-first). Portal and
     phone-only routes cannot be emailed -> manual-action queue for an
     agent. Portal routes are captured as PortalAction records (carrier,
     portal URL, action needed) — the defined contract for the future
     portal worker that will work carrier websites directly.

Eligibility (documented thresholds):
  GRACE_DAYS = 5: the CSR must have been nagged at least 5 days ago with
      no progress before the carrier is contacted. Carlo approved 5 days
      (was 7) on 2026-09-27: phase-3 reply tracking suppresses engaged
      CSRs, so the shorter window only ever bites on fully-silent CSRs.
  RECONTACT_DAYS = 14: the same carrier is never emailed twice about the
      same change inside 14 days.
  A change whose CSR is actively engaged (phase-3 status in_progress) gets
      the endorsement-landed check only — no carrier email while the CSR
      is working it. docs_claimed / confirmed / discrepancy / needs_human
      are phase 3's territory: skipped entirely.
  If the DocumentApi check is unavailable, the run FAILS CLOSED: no
      carrier is emailed without first verifying the endorsement didn't
      already download.

The worker never writes to EZLynx and never marks anything confirmed —
confirmation is phase 3's job. New statuses are recorded as history events
on the shared follow-up store (when present); phase-3's terminal
"confirmed" state is never touched.

Voice step (designed, not built): for carriers that don't answer email
after two email attempts, a Bland AI outbound call (carrier only, never
clients) reads the same policy/change facts and asks for status + ETA.
Not implemented: no tested call script, no call-outcome ingestion, no
deduplication against the email path. Build it only with a tested script
and Carlo's explicit approval.

Portal step (designed, not built — Carlo 2026-09-27: "we would be going
on carrier websites as well"): a future portal worker consumes the
PortalAction records this worker queues (see build_portal_action):
per-carrier browser flows on the ROBIE browser runtime (hermes-poc-01)
that sign in, navigate to the policy-change / endorsement inquiry, and
either submit the chase or read back the change status. Design notes:
  - One tested flow per carrier; start with the carriers that hold the
    most open changes. Reuse the phase-1/2 reliability battery pattern:
    wrong-policy submits, duplicate submits, and session-expiry mid-flow
    must all fail closed.
  - Credentials live in Secret Manager and are read at runtime by the
    box's service identity; never in code, notes, or the routing table.
  - Known constraint: some carrier portals bot-block the box's egress
    (documented exceptions live in the runbook). A blocked portal stays
    in the human manual-action queue with the URL attached — never
    retried blindly.
  - Portal outcomes feed back into the shared follow-up store as history
    events only; confirmation stays phase 3's job.

Dry-run default: nothing sent, nothing persisted.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
from collections.abc import Callable, Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .carrier_policy_change_routes import (
    emailable_routes,
    load_routing_table,
    manual_routes,
    resolve_carrier,
)
from .policy_change_mailbox_search import (
    SENDER as MAILBOX_SENDER,
    PolicyChangeMailboxSearcher,
    build_default_mailbox_searcher,
    resolve_change_mailboxes,
)
from .overdue_policy_change_reports import (
    JOB_TYPE as PHASE1_JOB_TYPE,
    REPORT_KEY,
    REPORT_MAILBOX,
    PolicyChangeReportContractError,
    default_queue_reader,
    notification_key,
    parse_4359_rows,
    row_created_date,
)

logger = logging.getLogger(__name__)

JOB_TYPE = "ezlynx.policy_change_carrier_contact"
SENDER = "robie@streetsmart.insurance"

# -- documented thresholds -----------------------------------------------------
#: Days after the CSR nag with no progress before the carrier is contacted.
#: Carlo approved 5 (was 7) on 2026-09-27: reply tracking suppresses
#: engaged CSRs, so the shorter window only affects fully-silent CSRs.
GRACE_DAYS = 5
#: Minimum days between two carrier emails about the same change.
RECONTACT_DAYS = 14

CARRIER_STORE_FILENAME = "carrier_contact.json"
DEFAULT_CARRIER_STORE = "~/.robie/overdue_policy_change_reports/carrier_contact.json"

#: Phase-3 statuses that take a change out of this worker's hands entirely.
PHASE3_OWNED_STATUSES = frozenset(
    {"docs_claimed", "confirmed", "discrepancy", "needs_human"}
)
#: Phase-3 status meaning the CSR is engaged: endorsement check only.
CSR_ENGAGED_STATUS = "in_progress"


# -- portal-action stub (future: Robie works carrier websites directly) ----------
# Carlo 2026-09-27: "we would be going on carrier websites as well." Portal
# automation is DESIGNED, not built. This record type is the contract the
# future portal worker will consume: everything it needs to open the
# carrier's site and chase the change is captured here at queue time.

def build_portal_action(item: Mapping[str, Any], carrier: Mapping[str, Any],
                        route: Mapping[str, Any]) -> dict[str, Any]:
    """One portal action a human (later: the portal worker) must take.

    `route` is a portal-type route from the routing table; the portal URL
    comes straight from the directory data (may be None when the directory
    names a portal without a URL — the human fills it in).
    """
    return {
        "type": "portal_action",
        "carrier": carrier.get("name"),
        "carrier_record_id": carrier.get("record_id"),
        "portal_url": route.get("url"),
        "route_label": route.get("label"),
        "route_notes": route.get("notes"),
        "policy_number": str(item.get("Policy Number") or ""),
        "account_name": str(item.get("Account Name") or ""),
        "action_needed": (
            "Sign in to the carrier portal, open the policy-change / "
            "endorsement inquiry for this policy, and chase the outstanding "
            "change (or confirm its status). Credentials live in "
            "Secret Manager; never in code or notes."
        ),
        "status": "queued_for_agent",
    }


def portal_actions_for(item: Mapping[str, Any],
                       carrier: Mapping[str, Any]) -> list[dict[str, Any]]:
    """All portal-type routes for a carrier as PortalAction records."""
    return [build_portal_action(item, carrier, r)
            for r in manual_routes(carrier)
            if r.get("type") == "portal"]

# -- endorsement-name hints (mirrors phase 3 PR #646; consolidate on merge) ---
ENDORSEMENT_NAME_HINTS = (
    "endorsement", "revised dec", "dec page", "declarations",
    "policy change", "amendment",
)


def default_carrier_store_path() -> str:
    override = os.environ.get("ROBIE_4359_CARRIER_STORE", "").strip()
    if override:
        return override
    return str(Path(DEFAULT_CARRIER_STORE).expanduser())


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# -- carrier-contact store -----------------------------------------------------

def _default_carrier_record() -> dict[str, Any]:
    return {
        "last_contact_date": None,
        "contact_count": 0,
        "endorsement_found_date": None,
        "history": [],
    }


class CarrierContactStore:
    """Per-change carrier-outreach state, keyed by phase 1's notification key.

    Atomic JSON save (tmp + replace). Only this worker writes it.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        except (ValueError, OSError):
            data = {}
        self._records: dict[str, dict[str, Any]] = {}
        for key, raw in (data or {}).items():
            record = _default_carrier_record()
            if isinstance(raw, dict):
                record.update({k: v for k, v in raw.items() if k in record})
                if not isinstance(record["history"], list):
                    record["history"] = []
            self._records[str(key)] = record

    def get(self, key: str) -> dict[str, Any]:
        return self._records.setdefault(str(key), _default_carrier_record())

    def record_event(self, key: str, event: str, detail: str, today: date) -> None:
        record = self.get(key)
        record["history"].append({
            "date": today.isoformat(),
            "event": str(event),
            "detail": str(detail or "")[:500],
        })

    def record_contact(self, key: str, route_email: str, today: date) -> None:
        record = self.get(key)
        record["last_contact_date"] = today.isoformat()
        record["contact_count"] = int(record.get("contact_count") or 0) + 1
        self.record_event(key, "carrier_email_sent",
                          f"to {route_email}", today)

    def record_endorsement_found(self, key: str, doc_name: str, today: date) -> None:
        record = self.get(key)
        record["endorsement_found_date"] = today.isoformat()
        self.record_event(key, "endorsement_found", doc_name, today)

    def days_since_contact(self, key: str, today: date) -> int | None:
        last = self.get(key).get("last_contact_date")
        if not last:
            return None
        try:
            return (today - datetime.strptime(str(last)[:10], "%Y-%m-%d").date()).days
        except ValueError:
            return None

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._records, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


class DryRunCarrierContactStore(CarrierContactStore):
    """A CarrierContactStore that never persists (dry-run: state untouched)."""

    def save(self) -> None:  # noqa: D102
        logger.info(
            "dry-run: carrier-contact store NOT saved (would persist %d records)",
            len(self._records),
        )


# -- endorsement-landed check (mirrors phase 3 PR #646; consolidate on merge)

def policy_digits(policy_number: Any) -> str:
    return re.sub(r"\D", "", str(policy_number or ""))


def _name_mentions_policy_digits(name: str, digits: str) -> bool:
    if not digits:
        return False
    return any(tok == digits for tok in re.findall(r"\d+", str(name or "")))


def find_endorsement_documents(
    documents: list[Mapping[str, Any]], policy_number: Any
) -> list[dict[str, Any]]:
    """Applicant documents that look like this change's endorsement.

    A candidate must carry an endorsement-type hint AND name the change's
    policy (digit run). Documents naming a DIFFERENT policy are excluded —
    they must never suppress a carrier email for this change.
    """
    digits = policy_digits(policy_number)
    candidates: list[dict[str, Any]] = []
    for doc in documents:
        if not isinstance(doc, dict):
            continue
        name = str(doc.get("name") or "")
        if not any(h in name.casefold() for h in ENDORSEMENT_NAME_HINTS):
            continue
        if _name_mentions_policy_digits(name, digits):
            candidates.append({"id": str(doc.get("id") or ""), "name": name})
    return candidates


def default_document_search(applicant_id: str) -> list[dict[str, Any]]:
    """DocumentApi search for one applicant (read-only). Returns [{id, name}]."""
    from .ezlynx_api import EzlynxApiClient, extract_document_api_results, load_ezlynx_api_config

    applicant = str(applicant_id or "").strip()
    if not applicant:
        raise PolicyChangeReportContractError("applicant id is required for document search")
    try:
        client = EzlynxApiClient(load_ezlynx_api_config())
        payload = client.search_applicant_documents(applicant)
    except Exception as exc:  # noqa: BLE001
        raise PolicyChangeReportContractError(
            f"DocumentApi search failed for applicant {applicant}: {type(exc).__name__}: {exc}"
        ) from exc
    return [{"id": row["id"], "name": row["name"]}
            for row in extract_document_api_results(payload)]


# -- nag dates (phase 1's sent store, read directly) ----------------------------

def load_nag_dates(sent_store_path: str | Path) -> dict[str, str]:
    """Map notification key -> first-nag date (ISO) from phase 1's sent store.

    Values may be a plain date string (legacy seed) or a dict with "date".
    """
    path = Path(str(sent_store_path)).expanduser()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (ValueError, OSError):
        data = {}
    dates: dict[str, str] = {}
    for key, value in (data or {}).items():
        if isinstance(value, dict):
            raw = str(value.get("date") or "")
        else:
            raw = str(value or "")
        if re.match(r"\d{4}-\d{2}-\d{2}", raw):
            dates[str(key)] = raw[:10]
    return dates


def _days_since(iso_date: str, today: date) -> int | None:
    try:
        return (today - datetime.strptime(iso_date[:10], "%Y-%m-%d").date()).days
    except ValueError:
        return None


# -- eligibility ----------------------------------------------------------------

def change_key(item: Mapping[str, Any]) -> str:
    return notification_key(
        str(item.get("CSR") or ""),
        str(item.get("Policy Number") or ""),
        str(item.get("created_date") or ""),
    )


def followup_status(followup_store: Any, key: str) -> str | None:
    """Phase-3 status for a change, or None when the store is absent."""
    if followup_store is None:
        return None
    try:
        record = followup_store.get(key)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(record, dict):
        return str(record.get("status") or "") or None
    return None


def eligibility(item: Mapping[str, Any], *, key: str, today: date,
                nag_dates: Mapping[str, str],
                carrier_store: CarrierContactStore,
                followup_store: Any = None) -> dict[str, Any]:
    """Decide what this worker may do for one open change.

    Returns {"action": "email" | "check_only" | "skip", "reason": str}.
    """
    age = int(item.get("age_days") or 0)
    status = followup_status(followup_store, key)
    if status in PHASE3_OWNED_STATUSES:
        return {"action": "skip", "reason": f"phase-3 owns (status={status})"}
    nag_date = nag_dates.get(key)
    if not nag_date:
        return {"action": "skip", "reason": "CSR not yet nagged; phase 1 owns first contact"}
    days_nagged = _days_since(nag_date, today)
    if days_nagged is None or days_nagged < GRACE_DAYS:
        return {"action": "skip",
                "reason": f"nagged {days_nagged}d ago; grace is {GRACE_DAYS}d"}
    if status == CSR_ENGAGED_STATUS:
        return {"action": "check_only",
                "reason": "CSR engaged; endorsement check only, no carrier email"}
    since_contact = carrier_store.days_since_contact(key, today)
    if since_contact is not None and since_contact < RECONTACT_DAYS:
        return {"action": "skip",
                "reason": f"carrier contacted {since_contact}d ago; re-contact window is {RECONTACT_DAYS}d"}
    return {"action": "email", "reason": f"nagged {days_nagged}d ago, no CSR progress"}


# -- carrier email ----------------------------------------------------------------

def build_carrier_email(item: Mapping[str, Any], route: Mapping[str, Any],
                        today: date) -> tuple[str, str, str]:
    """Subject, text body, HTML body for the carrier chase email."""
    account = str(item.get("Account Name") or "our client").strip()
    policy = str(item.get("Policy Number") or "").strip()
    lob = str(item.get("Line Of Business") or "").strip()
    effective = str(item.get("Effective Date") or "").strip()
    created = str(item.get("created_date") or "").strip()
    age = int(item.get("age_days") or 0)
    change_desc = str(item.get("change_description") or "").strip()

    subject = f"Follow-up: policy change for policy {policy} ({account})"
    lob_bit = f", {lob}" if lob else ""
    lines = [
        "Hello,",
        "",
        f"I'm Robie with StreetSmart Insurance, the agent of record for {account}"
        f" (policy {policy}{lob_bit}, effective {effective}).",
        "",
        f"We requested a policy change on {created} ({age} days ago).",
    ]
    if change_desc:
        lines += ["", f"Change requested: {change_desc}"]
    lines += [
        "",
        "Could you confirm you've received it and share the current status and",
        "expected completion date? If the endorsement has already been issued,",
        "please send it our way.",
        "",
        "Thank you,",
        "-Robie",
        "StreetSmart Insurance",
    ]
    text = "\n".join(lines)
    html_body = "<div>" + "".join(
        f"<p>{html.escape(p)}</p>" for p in text.split("\n\n")
    ) + "</div>"
    return subject, text, html_body


def default_carrier_mailer(*, to: list[str], cc: list[str], subject: str,
                           text_body: str, html_body: str) -> dict[str, Any]:
    """Send one carrier email from robie@ via keyless-delegated Gmail."""
    from .overdue_policy_change_reports import default_mailer

    return default_mailer(
        to=to, cc=cc, subject=subject, text_body=text_body, html_body=html_body,
    )


# -- worker -----------------------------------------------------------------------

class PolicyChangeCarrierContactWorker:
    """Phase-2 worker: endorsement-landed check, then carrier email."""

    def __init__(
        self,
        *,
        queue_reader: Callable[[Mapping[str, Any]], list[dict[str, str]]] | None = None,
        document_search: Callable[[str], list[dict[str, Any]]] | None = None,
        mailer: Callable[..., dict[str, Any]] | None = None,
        routing_table: dict[str, Any] | None = None,
        routing_table_path: str | None = None,
        cc_resolver: Callable[[Mapping[str, Any]], list[str]] | None = None,
        discussion_lookup: Callable[[str], dict[str, Any] | None] | None = None,
        mailbox_searcher: PolicyChangeMailboxSearcher | None = None,
        roster_maps: Mapping[str, Any] | None = None,
        producer_fallbacks: Mapping[str, str] | None = None,
    ) -> None:
        self.queue_reader = queue_reader or default_queue_reader
        self.document_search = document_search or default_document_search
        self.mailer = mailer or default_carrier_mailer
        self.routing_table = routing_table or load_routing_table(routing_table_path)
        self.cc_resolver = cc_resolver or (lambda item: [])
        self.discussion_lookup = discussion_lookup
        # Cross-mailbox Gmail search (Carlo-authorized domain-wide
        # delegation, read-only). None here means "resolve from the
        # environment at perform() time"; an explicit searcher (tests)
        # always wins.
        self.mailbox_searcher = mailbox_searcher
        self.roster_maps = roster_maps
        self.producer_fallbacks = producer_fallbacks or {}

    def _cross_post(self, followup_store: Any, key: str,
                    event: str, detail: str, today: date) -> None:
        """History-only event on the shared follow-up store. Never changes
        phase-3 statuses (set_status is never called from here)."""
        if followup_store is None:
            return
        try:
            followup_store.record_event(key, event, detail, today)
        except Exception as exc:  # noqa: BLE001
            logger.warning("follow-up store cross-post failed for %s: %s", key, exc)

    def perform(self, job: dict[str, Any], *, dry_run: bool = True,
                today: date | None = None) -> dict[str, Any]:
        today = today or date.today()
        payload: Mapping[str, Any] = job.get("payload") or {}
        evidence: dict[str, Any] = {
            "job_type": JOB_TYPE,
            "mode": "dry-run" if dry_run else "live",
            "ran_at": _utc_now(),
            "succeeded": False,
            "error": None,
        }
        try:
            store_path = str(
                payload.get("carrier_store_path") or default_carrier_store_path()
            )
            store: CarrierContactStore = (
                DryRunCarrierContactStore(store_path) if dry_run
                else CarrierContactStore(store_path)
            )
            nag_dates = load_nag_dates(
                str(payload.get("sent_store_path") or "~/.robie/overdue_policy_change_reports/sent.json")
            )
            followup_store = payload.get("followup_store")  # injected or None
            # `live` gates every mutation: dry-run touches nothing, not even
            # the injected follow-up store's in-memory state.
            live = not dry_run

            # Cross-mailbox Gmail search (read-only, domain-wide delegation).
            # Explicit searcher wins; otherwise resolve from the environment.
            # None = unconfigured -> the run continues without it, logged.
            searcher = self.mailbox_searcher
            if searcher is None:
                try:
                    searcher = build_default_mailbox_searcher()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("mailbox search unavailable: %s", exc)
                    searcher = None
            roster_maps = self.roster_maps
            if roster_maps is None:
                raw_roster = payload.get("roster_maps")
                roster_maps = raw_roster if isinstance(raw_roster, Mapping) else None
            producer_fallbacks = self.producer_fallbacks
            if not producer_fallbacks:
                raw_fb = payload.get("producer_fallbacks")
                if isinstance(raw_fb, Mapping):
                    producer_fallbacks = {
                        str(k): str(v) for k, v in raw_fb.items()
                    }
            mailbox_evidence: dict[str, Any] = {
                "enabled": bool(searcher and searcher.configured),
                "reason": (
                    None if (searcher and searcher.configured)
                    else "unconfigured: delegation env var or roster unavailable"
                ),
                "searches": [],
            }

            rows = self.queue_reader(payload)
            open_rows = [
                r for r in rows
                if str(r.get("Request Status") or "").strip().casefold() == "open"
            ]
            # Attach age like phase 1's qualify step (fail closed on bad dates).
            items: list[dict[str, Any]] = []
            for row in open_rows:
                created = row_created_date(row)
                age = (today - created).days
                if age < 0:
                    raise PolicyChangeReportContractError(
                        "4359 row created date is in the future"
                    )
                items.append({**row, "created_date": created.isoformat(), "age_days": age})

            # Optional change-description enrichment (discussion title only;
            # note bodies are not API-readable). The email works without it.
            if self.discussion_lookup is not None:
                for item in items:
                    try:
                        discussion = self.discussion_lookup(str(item.get("Applicant ID") or ""))
                    except Exception:  # noqa: BLE001
                        discussion = None
                    if isinstance(discussion, dict):
                        title = str(discussion.get("title") or "").strip()
                        if title:
                            item["change_description"] = title

            summary = {
                "open_queue_rows": len(items),
                "endorsement_found": 0,
                "endorsement_found_via_mailbox": 0,
                "emailed": 0,
                "send_errors": 0,
                "check_only": 0,
                "skipped": 0,
                "carrier_reply_recent": 0,
                "manual_queue_cleared": 0,
                "awaiting_directory": [],
                "manual_action": [],
                "held": [],
            }
            receipts: list[dict[str, Any]] = []

            for item in sorted(items, key=lambda r: int(r.get("age_days") or 0), reverse=True):
                key = change_key(item)
                elig = eligibility(
                    item, key=key, today=today, nag_dates=nag_dates,
                    carrier_store=store, followup_store=followup_store,
                )
                if elig["action"] == "skip":
                    summary["skipped"] += 1
                    continue

                # 1) Endorsement-landed check FIRST (fail closed on outage).
                try:
                    docs = self.document_search(str(item.get("Applicant ID") or ""))
                except PolicyChangeReportContractError as exc:
                    summary["held"].append({
                        "key": key,
                        "policy": str(item.get("Policy Number") or ""),
                        "reason": f"endorsement check unavailable: {exc}",
                    })
                    if live:
                        store.record_event(key, "endorsement_check_held", str(exc), today)
                    continue
                found = find_endorsement_documents(docs, item.get("Policy Number"))
                if found:
                    summary["endorsement_found"] += 1
                    doc_name = found[0]["name"]
                    if live:
                        store.record_endorsement_found(key, doc_name, today)
                        self._cross_post(followup_store, key, "endorsement_found",
                                         f"carrier worker found: {doc_name}", today)
                    continue

                # 1b) Cross-mailbox Gmail search (read-only delegation):
                # the CSR's or producer's inbox may already hold the
                # endorsement or a carrier reply. An endorsement found
                # here counts exactly like a DocumentApi hit; a carrier
                # reply is logged and clears the manual-action queue.
                # Every mailbox searched is logged in the evidence.
                if searcher is not None and searcher.configured:
                    mailboxes = resolve_change_mailboxes(
                        item, roster_maps, producer_fallbacks)
                    mb = searcher.search_change(item, mailboxes, today)
                    mb["change_key"] = key
                    mailbox_evidence["searches"].append(mb)
                    endorsement_hit = mb.get("endorsement")
                    if endorsement_hit:
                        summary["endorsement_found"] += 1
                        summary["endorsement_found_via_mailbox"] += 1
                        doc_name = (
                            f"gmail:{endorsement_hit.get('mailbox')}:"
                            f"{endorsement_hit.get('filename')}"
                        )
                        if live:
                            store.record_endorsement_found(key, doc_name, today)
                            self._cross_post(
                                followup_store, key, "endorsement_found",
                                f"mailbox search found: {doc_name}", today)
                        continue
                    reply_hit = mb.get("carrier_reply")
                    if reply_hit:
                        reply_detail = (
                            f"from {reply_hit.get('from')} "
                            f"in {reply_hit.get('mailbox')}: "
                            f"{reply_hit.get('subject')}"
                        )
                        if live:
                            store.record_event(key, "carrier_reply_found",
                                               reply_detail, today)
                            self._cross_post(followup_store, key,
                                             "carrier_reply_found",
                                             reply_detail, today)
                        reply_date = reply_hit.get("date")
                        recent = (
                            isinstance(reply_date, date)
                            and (today - reply_date).days < RECONTACT_DAYS
                        )
                        if elig["action"] == "email":
                            # Resolve the route now: a manual route with a
                            # live carrier reply is cleared from the queue —
                            # an agent doesn't need to chase a carrier that
                            # already answered.
                            carrier = resolve_carrier(
                                item.get("Master Company"), self.routing_table)
                            route_status = (carrier or {}).get("route_status") or ""
                            emails = emailable_routes(carrier) if carrier else []
                            is_manual = carrier is not None and (
                                route_status == "manual" or not emails)
                            if is_manual:
                                summary["manual_queue_cleared"] += 1
                                if live:
                                    store.record_event(
                                        key, "manual_queue_cleared",
                                        f"carrier replied; {reply_detail}", today)
                                continue
                            if recent:
                                # The carrier already replied recently: no
                                # duplicate outreach.
                                summary["carrier_reply_recent"] += 1
                                if live:
                                    store.record_event(
                                        key, "carrier_reply_recent",
                                        "carrier replied recently; email skipped",
                                        today)
                                continue
                            # Older reply on an emailable route: logged
                            # above; the chase email still goes out below.

                if elig["action"] == "check_only":
                    summary["check_only"] += 1
                    if live:
                        store.record_event(key, "endorsement_check_only",
                                           "no endorsement in EZLynx; CSR engaged", today)
                    continue

                # 2) Resolve the carrier -> route.
                carrier = resolve_carrier(item.get("Master Company"), self.routing_table)
                if carrier is None:
                    summary["awaiting_directory"].append({
                        "key": key,
                        "policy": str(item.get("Policy Number") or ""),
                        "master_company": str(item.get("Master Company") or ""),
                        "reason": "carrier not confidently matched to directory",
                    })
                    if live:
                        store.record_event(key, "awaiting_directory",
                                           f"unresolved carrier: {item.get('Master Company')}", today)
                    continue
                status = carrier.get("route_status") or ""
                if status == "missing":
                    summary["awaiting_directory"].append({
                        "key": key,
                        "policy": str(item.get("Policy Number") or ""),
                        "master_company": str(item.get("Master Company") or ""),
                        "reason": f"no policy-change route for {carrier.get('name')} "
                                  f"(record #{carrier.get('record_id')})",
                    })
                    if live:
                        store.record_event(key, "awaiting_directory",
                                           f"no route for {carrier.get('name')}", today)
                    continue
                emails = emailable_routes(carrier)
                if status == "manual" or not emails:
                    summary["manual_action"].append({
                        "key": key,
                        "policy": str(item.get("Policy Number") or ""),
                        "carrier": carrier.get("name"),
                        "routes": manual_routes(carrier),
                        # Portal URLs surfaced explicitly: the human agent —
                        # and the future portal worker — starts from these.
                        "portal_actions": portal_actions_for(item, carrier),
                        "reason": ("route needs an agent (portal/phone/fax-only "
                                   "or ambiguous); never emailed"),
                    })
                    if live:
                        store.record_event(key, "manual_action_queued",
                                           f"{carrier.get('name')}: manual route", today)
                    continue

                # 3) Email the carrier. Contact is recorded only AFTER a
                #    successful send, and the store is saved per send, so a
                #    mid-run crash cannot turn into a silent duplicate.
                route = emails[0]
                subject, text_body, html_body = build_carrier_email(item, route, today)
                to = [str(route["email"])]
                cc = [c for c in self.cc_resolver(item) if c and c not in to]
                if dry_run:
                    receipts.append({
                        "to": to, "cc": cc, "subject": subject,
                        "text_body": text_body,
                        "note": "dry-run: not sent",
                    })
                else:
                    try:
                        result = self.mailer(
                            to=to, cc=cc, subject=subject,
                            text_body=text_body, html_body=html_body,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.error("carrier email send failed for %s: %s",
                                     item.get("Policy Number"), exc)
                        receipts.append({"email": to[0],
                                         "error": f"{type(exc).__name__}: {exc}"})
                        summary["send_errors"] += 1
                        store.record_event(key, "send_failed",
                                           f"{type(exc).__name__}: {exc}", today)
                        continue
                    store.record_contact(key, to[0], today)
                    store.save()
                    self._cross_post(followup_store, key, "carrier_contacted",
                                     f"emailed {to[0]}", today)
                    receipts.append({"message_id": result.get("message_id"),
                                     "to": to, "cc": cc, "subject": subject,
                                     "note": "sent"})
                summary["emailed"] += 1

            evidence["summary"] = summary
            evidence["receipts"] = receipts
            evidence["mailbox_search"] = mailbox_evidence
            if not dry_run:
                store.save()
            evidence["succeeded"] = True
        except PolicyChangeReportContractError as exc:
            evidence["error"] = str(exc)
        except Exception as exc:  # noqa: BLE001
            evidence["error"] = f"carrier-contact run failed: {type(exc).__name__}: {exc}"
        return evidence
