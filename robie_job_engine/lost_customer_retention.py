"""Monthly lost-customer retention reporting with fail-closed evidence rules."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
UNKNOWN = "Unknown"
REQUIRED_REVIEW_HEADERS = (
    "Month", "Applicant ID", "Account Name", "Policy Count", "Lines of Business",
    "Policy Numbers", "Department", "CSR", "Assigned Agent", "Annualized Premium",
    "Account Status Classification", "Evidence-Supported Cause", "Evidence Summary",
    "Responsibility Lane", "Preventable?", "Confidence", "Magellan Match",
    "Magellan Sentiment", "Recovery Opportunity", "Recommended Account Action",
    "Systemic Prevention",
)


def normalize(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def money(value: Any) -> float:
    cleaned = re.sub(r"[^0-9.()-]", "", normalize(value)).replace("(", "-").replace(")", "")
    try:
        return float(cleaned or 0)
    except ValueError:
        return 0.0


def records(values: Sequence[Sequence[Any]]) -> list[dict[str, str]]:
    if not values:
        return []
    headers = [normalize(v) for v in values[0]]
    if tuple(headers[: len(REQUIRED_REVIEW_HEADERS)]) != REQUIRED_REVIEW_HEADERS:
        raise ValueError("3-Month Account Review headers do not match the approved contract")
    output = []
    for row in values[1:]:
        item = {header: normalize(row[i]) if i < len(row) else "" for i, header in enumerate(headers)}
        if item.get("Month") and item.get("Applicant ID"):
            if not item.get("Evidence-Supported Cause") or "not found" in item["Evidence-Supported Cause"].casefold():
                item["Evidence-Supported Cause"] = UNKNOWN
            output.append(item)
    return output


def validate_monthly_source(values: Sequence[Sequence[Any]], month: str) -> dict[str, Any]:
    rows = []
    for row in values:
        first = normalize(row[0] if row else "")
        if first.isdigit() and len(row) > 12 and normalize(row[9]):
            rows.append(row)
    keys = ["|".join((normalize(r[0]).casefold(), normalize(r[9]).casefold(), normalize(r[12]).casefold())) for r in rows]
    if len(keys) != len(set(keys)):
        raise ValueError(f"{month}: duplicate policy transaction keys remain")
    departments = Counter(normalize(r[8]) or UNKNOWN for r in rows)
    return {
        "month": month,
        "policy_rows": len(rows),
        "accounts": len({normalize(r[0]) for r in rows}),
        "departments": dict(sorted(departments.items())),
        "sha256": hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
    }


def summarize(items: Iterable[Mapping[str, str]]) -> dict[str, Any]:
    grouped: dict[str, dict[str, Any]] = {}
    for item in items:
        key = f"{item.get('Month')}|{item.get('Department') or UNKNOWN}"
        group = grouped.setdefault(key, {
            "month": item.get("Month"), "department": item.get("Department") or UNKNOWN,
            "accounts": 0, "policies": 0, "premium": 0.0, "classifications": Counter(),
            "causes": Counter(), "magellan_matches": 0,
        })
        group["accounts"] += 1
        group["policies"] += int(money(item.get("Policy Count")))
        group["premium"] += money(item.get("Annualized Premium"))
        group["classifications"][item.get("Account Status Classification") or UNKNOWN] += 1
        group["causes"][item.get("Evidence-Supported Cause") or UNKNOWN] += 1
        if normalize(item.get("Magellan Match")).casefold() not in {"", "no matched record", "not available"}:
            group["magellan_matches"] += 1
    result = []
    for group in grouped.values():
        group["classifications"] = dict(group["classifications"])
        group["causes"] = dict(group["causes"])
        group["premium"] = round(group["premium"], 2)
        result.append(group)
    return {"groups": sorted(result, key=lambda x: (x["month"], x["department"]))}


def department_email(department: str, items: Sequence[Mapping[str, str]], *, run_id: str) -> str:
    lines = [
        f"StreetSmart Lost Customer Retention Review — {department}", "",
        "June–August 2026 validated backfill", f"Run ID: {run_id}", "",
    ]
    if not items:
        lines += ["No account-level records mapped to this department for the validated period."]
        return "\n".join(lines)
    totals = summarize(items)["groups"]
    lines.append("Monthly totals:")
    for group in totals:
        lines.append(f"- {group['month']}: {group['accounts']} accounts / {group['policies']} policies / ${group['premium']:,.2f}")
    lines += ["", "Account-level findings:"]
    for item in items:
        lines += [
            "",
            f"- {item['Month']} | Applicant {item['Applicant ID']} | {item['Account Name']}",
            f"  Policies: {item['Policy Count']} | LOB: {item['Lines of Business']} | Premium: {item['Annualized Premium']}",
            f"  Status: {item['Account Status Classification']} | Cause: {item['Evidence-Supported Cause'] or UNKNOWN}",
            f"  Evidence: {item['Evidence Summary'] or 'No supporting evidence found.'}",
            f"  Magellan: {item['Magellan Match'] or 'No matched record'} / {item['Magellan Sentiment'] or 'Not available'}",
            f"  Recommended action: {item['Recommended Account Action'] or 'Review and document a supported cause.'}",
        ]
    lines += ["", "Assignment alone is not evidence of employee fault. Unsupported causes remain Unknown."]
    return "\n".join(lines)


def executive_email(items: Sequence[Mapping[str, str]], *, run_id: str) -> str:
    summary = summarize(items)["groups"]
    total_accounts = len(items)
    total_policies = sum(int(money(x.get("Policy Count"))) for x in items)
    total_premium = sum(money(x.get("Annualized Premium")) for x in items)
    statuses = Counter(x.get("Account Status Classification") or UNKNOWN for x in items)
    lines = [
        "StreetSmart Lost Customer Retention Review — Executive Trends", "",
        "June–August 2026 validated backfill", f"Run ID: {run_id}",
        f"Overall: {total_accounts} account-months / {total_policies} policies / ${total_premium:,.2f} flagged annualized premium", "",
        "Department/month totals:",
    ]
    for group in summary:
        lines.append(f"- {group['month']} — {group['department']}: {group['accounts']} accounts / {group['policies']} policies / ${group['premium']:,.2f}")
    lines += ["", "Corrected account classifications:"]
    for label, count in statuses.most_common():
        lines.append(f"- {label}: {count}")
    lines += [
        "", "Controls verified:",
        "- Raw policy rows are consolidated to account-month findings.",
        "- Rewrites, reinstatements and partial-policy losses remain separate from true customer loss.",
        "- Unsupported causes are Unknown.",
        "- Assignment alone is never treated as employee fault.",
        "- Magellan is corroborating evidence only when an account match exists.",
        "", "This is a scheduled monthly-updated Google Sheet, not a continuously updating dashboard.",
    ]
    return "\n".join(lines)


@dataclass(frozen=True)
class MessageSpec:
    key: str
    to: tuple[str, ...]
    subject: str
    body: str


def build_messages(items: Sequence[Mapping[str, str]], recipients: Mapping[str, str], run_id: str) -> list[MessageSpec]:
    by_department: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for item in items:
        by_department[item.get("Department") or UNKNOWN].append(item)
    mapping = (("Commercial", "commercial"), ("Personal", "personal"), ("Trucking", "trucking"))
    messages = []
    for department, key in mapping:
        messages.append(MessageSpec(
            key=f"department:{key}", to=(recipients[key],),
            subject=f"Lost Customer Retention Review — {department} — June–August 2026",
            body=department_email(department, by_department.get(department, []), run_id=run_id),
        ))
    messages.append(MessageSpec(
        key="executive", to=(recipients["carlo"], recipients["jake"]),
        subject="Lost Customer Retention Review — Executive Trends — June–August 2026",
        body=executive_email(items, run_id=run_id),
    ))
    return messages


def send_message(gmail: Any, sender: str, spec: MessageSpec) -> dict[str, Any]:
    message = EmailMessage()
    message["To"] = ", ".join(spec.to)
    message["From"] = sender
    message["Subject"] = spec.subject
    message["X-ROBIE-Idempotency-Key"] = spec.key
    message.set_content(spec.body)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    sent = gmail.users().messages().send(userId="me", body={"raw": raw}).execute()
    return {"key": spec.key, "to": list(spec.to), "message_id": sent.get("id"), "thread_id": sent.get("threadId")}


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"runs": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(path)

