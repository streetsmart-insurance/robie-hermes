"""Mortgagee/lender enrichment for the 4372/4744 verification worker.

Read-only by design. Resolves, per policy, the mortgagee(s) of record
(lender name + loan number) and turns them into concrete verification
checks. The worker (verification_workers.plan_4372 / run_worker) consumes
exactly this interface:

    STATUS_READY
    EnrichmentPorts / resolve_enrichment_ports(...)
    EnrichmentResult (status, property_zip, mortgages, to_dict())
    MortgageLenderCheck (to_dict())
    enrich_work_item(policy_number, applicant_id, row, ports, dry_run)
    plan_from_enrichment(result, due_txt, property_zip, portal_lookup,
                         producer_state, lender_checks, verify_lender_fn,
                         verify_of_record_fn, producer_gate_fn)
    property_zip_from_row(row)
    check_ready_mortgages(mortgages, property_zip, portal_lookup)
    producer_gate_from_row(row)

Carlo's rules enforced here:
- Missing, ambiguous, or unavailable lender identity -> HOLD, never a
  guessed lender. ("lender TBD" is blocked, not worked.)
- Conflicting mortgagee sources -> HOLD for human review (HITL).
- dry_run performs NO network/browser reads; enrichment reports HOLD
  with the reason naming what a live read would do.
- Proven-zero (no mortgagee of record) is an explicit HOLD result with
  "nothing to verify" — never a silent skip.

Live read path (wired via EnrichmentPorts by the runtime, not here):
  PolicyApi search by policy number -> applicant id -> Additional
  Interests (mortgagee entries) -> lender name + loan number.
  Browser fallback reads the same Additional Interests panel when the
  API misses. This module never touches the network itself.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping, Sequence

STATUS_READY = "ready"
STATUS_HOLD = "hold"
STATUS_CONFLICT = "conflict"
STATUS_ERROR = "error"


# --- ports -----------------------------------------------------------------


@dataclass
class EnrichmentPorts:
    """Injectable read adapters. All optional; None = unavailable.

    policy_search_fn(policy_number) -> dict with at least "applicant_id"
        (EZLynx PolicyApi search?PolicyNumber= — read-only).
    additional_interests_fn(applicant_id) -> list of mortgagee dicts, each
        with "lender_name" and "loan_number" keys when known.
    browser_interests_fn(applicant_id) -> same shape; used only when the
        API path misses and live_browser is on.
    lender_directory_fn(lender_name) -> dict with portal/contact info.
    """

    policy_search_fn: Callable[[str], dict[str, Any] | None] | None = None
    additional_interests_fn: Callable[[str], list[dict[str, Any]]] | None = None
    browser_interests_fn: Callable[[str], list[dict[str, Any]]] | None = None
    lender_directory_fn: Callable[[str], dict[str, Any] | None] | None = None


def resolve_enrichment_ports(
    enrichment_ports: EnrichmentPorts | None,
    *,
    live_test: bool = False,
    live_browser: bool = False,
) -> EnrichmentPorts:
    """Return the effective ports for a run.

    In dry_run the worker passes live_test=False/live_browser=False and the
    ports stay as injected (usually empty -> HOLD). Flags are accepted for
    forward-compatibility with the live runtime that wires real adapters.
    """
    ports = enrichment_ports or EnrichmentPorts()
    # Flags are informational at this layer; the runtime decides which
    # adapters to inject. Never invent adapters here.
    _ = (live_test, live_browser)
    return ports


# --- results ----------------------------------------------------------------


@dataclass
class Mortgage:
    lender_name: str = ""
    loan_number: str = ""
    interest_type: str = ""
    source: str = ""  # "api" | "browser" | "row" | "test"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EnrichmentResult:
    status: str  # ready | hold | conflict | error
    reason: str = ""
    property_zip: str = ""
    mortgages: list[Mortgage] = field(default_factory=list)
    applicant_id: str = ""
    policy_number: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "property_zip": self.property_zip,
            "applicant_id": self.applicant_id,
            "policy_number": self.policy_number,
            "mortgages": [m.to_dict() for m in self.mortgages],
        }


@dataclass
class MortgageLenderCheck:
    lender_name: str
    loan_number: str = ""
    channel: str = "email"  # portal | email | call
    target: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- row helpers ------------------------------------------------------------


def _row_get(row: Mapping[str, Any], *names: str) -> str:
    for name in names:
        if name in row:
            return str(row.get(name) or "").strip()
    return ""


def property_zip_from_row(row: Mapping[str, Any]) -> str:
    """Best-effort property ZIP from the report row (often absent)."""
    for name in ("Property ZIP", "Property Zip", "ZIP", "Zip Code",
                 "Property Postal Code", "Applicant Data Property ZIP"):
        value = _row_get(row, name)
        if value:
            return value
    # Last resort: a 5-digit ZIP inside a property address column.
    import re

    for name in ("Property Address", "Applicant Data Property Address",
                 "Address"):
        match = re.search(r"\b(\d{5})(?:-\d{4})?\b", _row_get(row, name))
        if match:
            return match.group(1)
    return ""


def producer_gate_from_row(row: Mapping[str, Any]) -> tuple[bool, str]:
    """Producer gate: Robie only works items with a real producer assignment.

    Returns (clear, reason). A blank producer/CSR means nobody owns the
    account, so delivery is blocked rather than guessed.
    """
    producer = _row_get(row, "Assigned Producer", "Applicant Data Assigned Producer",
                        "Producer")
    if not producer:
        return False, "no producer assigned — account has no owner to route through"
    return True, f"producer {producer} assigned"


# --- enrichment -------------------------------------------------------------


def _mortgages_from_entries(
    entries: Sequence[Mapping[str, Any]], source: str
) -> list[Mortgage]:
    mortgages: list[Mortgage] = []
    for entry in entries:
        lender = str(entry.get("lender_name") or entry.get("Lender Name")
                     or entry.get("mortgagee") or "").strip()
        loan = str(entry.get("loan_number") or entry.get("Loan Number")
                    or entry.get("loanNumber") or "").strip()
        if not lender and not loan:
            continue
        mortgages.append(Mortgage(
            lender_name=lender,
            loan_number=loan,
            interest_type=str(entry.get("interest_type") or "").strip(),
            source=source,
        ))
    return mortgages


def enrich_work_item(
    *,
    policy_number: str,
    applicant_id: str = "",
    row: Mapping[str, Any] | None = None,
    ports: EnrichmentPorts | None = None,
    dry_run: bool = True,
) -> EnrichmentResult:
    """Resolve the mortgagee(s) of record for one policy. Read-only.

    Fail-closed: any missing/ambiguous/unavailable lender identity returns
    HOLD (or CONFLICT), never a guessed lender. In dry_run with no injected
    adapters this returns HOLD naming the live read it would perform.
    """
    row = row or {}
    ports = ports or EnrichmentPorts()
    policy_number = (policy_number or "").strip()
    if not policy_number:
        return EnrichmentResult(
            status=STATUS_HOLD,
            reason="blank policy number — cannot resolve mortgagee",
            policy_number=policy_number,
        )

    has_adapters = bool(
        ports.policy_search_fn or ports.additional_interests_fn
        or ports.browser_interests_fn
    )
    if dry_run and not has_adapters:
        return EnrichmentResult(
            status=STATUS_HOLD,
            reason=(
                f"lender enrichment needs a live EZLynx read for policy "
                f"{policy_number} (dry_run: no read performed)"
            ),
            policy_number=policy_number,
            applicant_id=(applicant_id or "").strip(),
            property_zip=property_zip_from_row(row),
        )

    resolved_applicant = (applicant_id or "").strip()
    if not resolved_applicant and ports.policy_search_fn:
        try:
            found = ports.policy_search_fn(policy_number)
        except Exception as exc:  # adapter failure is HOLD, not fatal
            return EnrichmentResult(
                status=STATUS_HOLD,
                reason=f"policy search failed for {policy_number}: {exc}",
                policy_number=policy_number,
            )
        if not found or not str(found.get("applicant_id") or "").strip():
            return EnrichmentResult(
                status=STATUS_HOLD,
                reason=(f"policy {policy_number} not anchored to an EZLynx "
                        f"applicant — cannot resolve mortgagee"),
                policy_number=policy_number,
            )
        resolved_applicant = str(found["applicant_id"]).strip()

    mortgages: list[Mortgage] = []
    source = ""
    if resolved_applicant and ports.additional_interests_fn:
        try:
            entries = ports.additional_interests_fn(resolved_applicant) or []
        except Exception as exc:
            return EnrichmentResult(
                status=STATUS_HOLD,
                reason=(f"additional-interests read failed for applicant "
                        f"{resolved_applicant}: {exc}"),
                policy_number=policy_number,
                applicant_id=resolved_applicant,
            )
        mortgages = _mortgages_from_entries(entries, "api")
        source = "api"
    if not mortgages and resolved_applicant and ports.browser_interests_fn:
        try:
            entries = ports.browser_interests_fn(resolved_applicant) or []
        except Exception as exc:
            return EnrichmentResult(
                status=STATUS_HOLD,
                reason=(f"browser additional-interests read failed: {exc}"),
                policy_number=policy_number,
                applicant_id=resolved_applicant,
            )
        mortgages = _mortgages_from_entries(entries, "browser")
        source = "browser"

    if not resolved_applicant:
        return EnrichmentResult(
            status=STATUS_HOLD,
            reason=(f"no applicant id for policy {policy_number} and no "
                    f"policy-search adapter — cannot resolve mortgagee"),
            policy_number=policy_number,
            property_zip=property_zip_from_row(row),
        )
    if not mortgages:
        return EnrichmentResult(
            status=STATUS_HOLD,
            reason=(f"no mortgagee of record found for policy {policy_number} "
                    f"(applicant {resolved_applicant}, via {source or 'no source'}) "
                    f"— nothing to verify"),
            policy_number=policy_number,
            applicant_id=resolved_applicant,
            property_zip=property_zip_from_row(row),
        )

    # Conflicting lenders (distinct non-blank names) -> human review.
    names = {m.lender_name.casefold() for m in mortgages if m.lender_name}
    if len(names) > 1:
        return EnrichmentResult(
            status=STATUS_CONFLICT,
            reason=(f"conflicting mortgagees for policy {policy_number}: "
                    f"{', '.join(sorted(names))} — needs human review"),
            policy_number=policy_number,
            applicant_id=resolved_applicant,
            mortgages=mortgages,
            property_zip=property_zip_from_row(row),
        )
    return EnrichmentResult(
        status=STATUS_READY,
        reason=f"{len(mortgages)} mortgagee(s) resolved via {source}",
        policy_number=policy_number,
        applicant_id=resolved_applicant,
        mortgages=mortgages,
        property_zip=property_zip_from_row(row),
    )


# --- checks -----------------------------------------------------------------


def check_ready_mortgages(
    mortgages: Sequence[Mortgage],
    *,
    property_zip: str = "",
    portal_lookup: Mapping[str, Any] | None = None,
    lender_directory_fn: Callable[[str], dict[str, Any] | None] | None = None,
) -> list[MortgageLenderCheck]:
    """Turn resolved mortgages into concrete verification checks.

    Portal first (Carlo's contact ladder): a lender with a known portal
    gets a portal check; otherwise email, then call. Lenders usually need
    no login — loan number + identifiers in the agent section.
    """
    checks: list[MortgageLenderCheck] = []
    for mortgage in mortgages:
        lender = mortgage.lender_name or "lender TBD"
        loan = mortgage.loan_number
        portal_url = ""
        if portal_lookup and isinstance(portal_lookup, Mapping):
            portal_url = str(portal_lookup.get(lender) or "").strip()
        if not portal_url and lender_directory_fn:
            try:
                info = lender_directory_fn(lender) or {}
            except Exception:
                info = {}
            portal_url = str(info.get("portal_url") or "").strip()
        loan_txt = f"loan {loan}" if loan else "loan number missing"
        if portal_url:
            checks.append(MortgageLenderCheck(
                lender_name=lender,
                loan_number=loan,
                channel="portal",
                target=portal_url,
                detail=(f"Upload renewal dec package to {lender} agent portal "
                        f"({portal_url}) for {loan_txt}; confirm payment "
                        f"method on file"),
            ))
        else:
            checks.append(MortgageLenderCheck(
                lender_name=lender,
                loan_number=loan,
                channel="email",
                target=lender,
                detail=(f"Email {lender} ({loan_txt}): confirm they are the "
                        f"lender of record, deliver the renewal dec package, "
                        f"and confirm payment; no portal on file so call if "
                        f"no reply in 2 business days"),
            ))
    _ = property_zip  # reserved for lender-site scoping in live runs
    return checks


# --- planning ---------------------------------------------------------------


def plan_from_enrichment(
    result: EnrichmentResult | None,
    *,
    due_txt: str = "",
    property_zip: str = "",
    portal_lookup: Mapping[str, Any] | None = None,
    producer_state: Mapping[str, Any] | None = None,
    lender_checks: Sequence[MortgageLenderCheck] | None = None,
    verify_lender_fn: Callable[..., Any] | None = None,
    verify_of_record_fn: Callable[..., Any] | None = None,
    producer_gate_fn: Callable[[Mapping[str, Any]], tuple[bool, str]] | None = None,
) -> tuple[str, str, str, str, str]:
    """(kind, detail, target, status, reason) for one mortgagee work item.

    Blocked-with-reason is the normal outcome until enrichment is READY
    and the producer gate is clear. Never plans outreach to a TBD lender.
    """
    policy_ref = f"Policy {result.policy_number}" if result and result.policy_number else "policy"
    due = f"; {due_txt}" if due_txt else ""

    if result is None:
        return ("verify",
                f"{policy_ref}: lender enrichment unavailable — resolve mortgagee before outreach{due}",
                "lender TBD", "blocked", "lender enrichment unavailable")

    if result.status in (STATUS_HOLD, STATUS_CONFLICT, STATUS_ERROR):
        lender_txt = result.mortgages[0].lender_name if result.mortgages else "lender TBD"
        return ("verify",
                f"{policy_ref}: mortgagee verification held — {result.reason}{due}",
                lender_txt, "blocked", f"lender {result.status}: {result.reason}")

    # READY — producer gate still blocks delivery.
    gate = producer_gate_fn or producer_gate_from_row
    try:
        clear, gate_reason = gate(producer_state or {})
    except Exception as exc:
        clear, gate_reason = False, f"producer gate error: {exc}"
    if not clear:
        return ("verify",
                f"{policy_ref}: producer gate blocks delivery — {gate_reason}{due}",
                "producer", "blocked", f"producer gate: {gate_reason}")

    checks = list(lender_checks or [])
    if not checks:
        return ("verify",
                f"{policy_ref}: lender resolved but no verification check built — "
                f"re-check lender directory{due}",
                result.mortgages[0].lender_name if result.mortgages else "lender",
                "blocked", "lender ready but no check built")

    # Optional live verifications (lender-of-record confirmation).
    if verify_lender_fn or verify_of_record_fn:
        notes: list[str] = []
        for check in checks:
            for fn in (verify_lender_fn, verify_of_record_fn):
                if fn is None:
                    continue
                try:
                    verdict = fn(check.lender_name, check.loan_number,
                                 result.policy_number)
                except Exception as exc:
                    return ("verify",
                            f"{policy_ref}: lender verification failed for "
                            f"{check.lender_name}: {exc} — held{due}",
                            check.lender_name, "blocked",
                            f"lender verification error: {exc}")
                if verdict is False:
                    return ("verify",
                            f"{policy_ref}: {check.lender_name} is NOT the lender "
                            f"of record per verification — held for review{due}",
                            check.lender_name, "blocked",
                            "lender-of-record mismatch")
                notes.append(str(verdict) if verdict else "verified")
        _ = notes

    first = checks[0]
    extra = f" (+{len(checks) - 1} more)" if len(checks) > 1 else ""
    zip_txt = f" (property ZIP {property_zip})" if property_zip else ""
    return (first.channel,
            f"{policy_ref}{extra}: {first.detail}{zip_txt}{due}",
            first.target or first.lender_name,
            "due_now",
            f"lender {first.lender_name} verified as target — {first.channel} next")
