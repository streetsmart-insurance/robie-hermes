"""Role-aware accountability rows built only from supplied evidence.

The same raw metric must not be used to grade every employee.  Producers are
evaluated on sales activity and outbound follow-up; service staff on calls,
callbacks, tasks, and email; managers on queue/backlog oversight.  Missing
evidence remains UNVERIFIED rather than becoming a zero.
"""

from __future__ import annotations

from typing import Any, Mapping


ROLE_ALIASES = {
    "producer": "producer",
    "sales producer": "producer",
    "bdr": "producer",
    "account manager": "service",
    "commercial lines account manager": "service",
    "personal lines account manager": "service",
    "csr": "service",
    "customer service representative": "service",
    "technician": "technician",
    "department manager": "manager",
    "commercial lines department manager": "manager",
    "trucking department manager": "manager",
    "director of first impressions": "front_desk",
    "receptionist": "front_desk",
}


def normalize_role(value: str) -> str:
    text = " ".join(str(value or "").strip().casefold().split())
    if text in ROLE_ALIASES:
        return ROLE_ALIASES[text]
    if "producer" in text or "sales" in text:
        return "producer"
    if "manager" in text or "director" in text:
        return "manager"
    if "account manager" in text or "csr" in text or "service" in text:
        return "service"
    if "technician" in text or "processor" in text:
        return "technician"
    return "unmapped"


def _lookup(mapping: Mapping[str, Any], name: str, default: Any = None) -> Any:
    folded = name.casefold().strip()
    for key, value in mapping.items():
        if str(key).casefold().strip() == folded:
            return value
    return default


def build_role_rows(
    registry: Mapping[str, Any],
    *,
    call_data: Mapping[str, Any],
    task_data: Mapping[str, Any],
    sales_data: Mapping[str, Any],
    email_data: Mapping[str, Any],
    magellan_data: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Return per-person facts with metric applicability and evidence status."""

    call_rows = {str(row.get("employee") or "").casefold(): row for row in call_data.get("employee_rows", [])}
    overdue = task_data.get("overdue_by_rep", {}) or {}
    email_by_employee = email_data.get("by_employee", {}) or {}
    magellan_by_employee = magellan_data.get("by_employee", {}) or {}
    sales_by_producer: dict[str, int] = {}
    for item in sales_data.get("exceptions", []) or []:
        producer = str(item.get("producer") or "Unassigned").casefold()
        sales_by_producer[producer] = sales_by_producer.get(producer, 0) + 1

    rows: list[dict[str, Any]] = []
    for employee, raw in registry.items():
        config = raw if isinstance(raw, Mapping) else {"role": raw}
        role_label = str(config.get("role") or "Unmapped")
        family = normalize_role(role_label)
        call = call_rows.get(str(employee).casefold(), {})
        mailbox = str(config.get("email") or "").casefold()
        email = _lookup(email_by_employee, mailbox, {}) if mailbox else {}
        magellan = _lookup(magellan_by_employee, str(employee), {}) or {}
        sample = int(magellan.get("call_count") or magellan.get("calls") or 0)
        min_sample = int(config.get("magellan_min_sample") or 10)

        facts: list[str] = []
        if family in {"service", "front_desk", "manager"}:
            if call_data.get("source_status") == "available":
                facts.append(
                    f"phone: {call.get('answered', 0)}/{call.get('calls_presented', 0)} answered, "
                    f"{call.get('unreturned', 0)} unreturned"
                )
            else:
                facts.append("phone: UNVERIFIED")
        elif family == "producer":
            facts.append(f"outbound calls: {call.get('outbound', 'UNVERIFIED') if call else 'UNVERIFIED'}")
            sales_status = str(sales_data.get("source_status") or "").casefold()
            inactive = sales_by_producer.get(str(employee).casefold(), 0) if sales_status in {"available", "complete", "verified"} else "UNVERIFIED"
            facts.append(f"inactive sales accounts: {inactive}")
        else:
            facts.append("role metrics: UNVERIFIED — role not mapped")

        if family in {"service", "technician", "manager"}:
            value = _lookup(overdue, str(employee))
            facts.append(f"overdue EZLynx tasks: {value if value is not None else 'UNVERIFIED'}")
        if family in {"service", "producer", "manager", "front_desk"}:
            facts.append(f"email awaiting employee >24h: {email.get('stalled_threads', 'UNVERIFIED')}")
        if magellan:
            quality = magellan.get("score", magellan.get("satisfied_rate", "UNVERIFIED"))
            facts.append(
                f"Magellan quality: {quality} ({sample} calls; "
                f"{'eligible' if sample >= min_sample else 'INSUFFICIENT_SAMPLE'})"
            )

        rows.append({
            "employee": str(employee),
            "role": role_label,
            "role_family": family,
            "facts": facts,
        })
    return rows
