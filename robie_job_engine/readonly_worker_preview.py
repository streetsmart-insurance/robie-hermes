#!/usr/bin/env python3
"""Read-only verification worker preview.

Generates the "what the AI would do" per-policy preview (Excel) from the
verification report CSVs (4246 audits, 4247 manual renewals, 4744 mortgagee).
This is the grading harness Carlo reviews — READ-ONLY: no EZLynx writes,
no notes, no outreach.

Reliability gates (all regression-tested in
tests/test_readonly_worker_preview.py):

- Dead-policy gate: the emailed reports carry no status column, so a
  cancelled policy can sit in a queue looking like live work. Callers
  pass a live PolicyApi status map
  (``{policy_number: {"status": ..., "cancellation_date": ...}}``);
  non-Active policies get a DO NOT WORK / verify-first action instead
  of normal work. Without a map the gate warns and stays off.
- Strict knowledge matching: an agency contact-type knowledge entry
  applies only on an exact contact-type match (or a ``General`` entry).
  A Policy Changes fact must never appear on a Renewals/Audits/
  Mortgagee action.
- Policy-number conflict detection: the same policy number on multiple
  rows (different accounts/carriers) is flagged on every affected row,
  never silently double-worked.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime
from pathlib import Path

MGA_KEYWORDS = ["MGA", "RT Specialty", "AmWINS", "Cover Whale",
                "Johnson & Johnson", "XPT", "USLI"]

# PolicyApi policyStatus values that mean "do not work this row".
DEAD_STATUSES = {"Inactive", "Cancelled", "Canceled"}

# Knowledge contact-type categories used by the composers below.
CONTACT_RENEWALS = "Renewals"
CONTACT_AUDITS = "Audits"
CONTACT_MORTGAGEE = "Mortgagee"


def parse_date(s):
    if not s or not str(s).strip():
        return None
    s = str(s).strip().split("T")[0]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def days_between(d1, d2):
    if not d1 or not d2:
        return None
    return (d2 - d1).days


def load_knowledge_entries(path):
    """Parse the agency contact-type knowledge file.

    Entry lines look like::

        - **Carrier Name** — Contact Type — instruction text. (Source, date)

    Returns [(carrier, contact_type, instruction, source)].
    """
    entries = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return entries
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("- **"):
            continue
        try:
            carrier, rest = line[len("- **"):].split("**", 1)
        except ValueError:
            continue
        # Remainder: " — Contact Type — instruction". The instruction
        # itself may contain " — ", so the contact type is the first
        # segment only.
        parts = [p.strip() for p in rest.split("\u2014")]
        parts = [p for p in parts if p]
        if len(parts) < 2:
            continue
        carrier = carrier.strip()
        contact, instruction = parts[0], " \u2014 ".join(parts[1:])
        source = ""
        if instruction.endswith(")") and "(" in instruction:
            instruction, source = instruction.rsplit("(", 1)
            instruction, source = instruction.strip().rstrip("."), source.rstrip(")").strip()
        if carrier and instruction:
            entries.append((carrier, contact, instruction, source))
    return entries


def knowledge_for(entries, carrier, contact_type):
    """Entries for this carrier AND contact type (strict).

    An entry applies only when its contact type matches the action
    category exactly, or the entry is marked ``General``. Carrier
    matching is a case-insensitive substring either way.
    """
    carrier_l, contact_l = (carrier or "").lower(), (contact_type or "").lower()
    hits = []
    for c, ct, instr, src in entries:
        if c.lower() in carrier_l or carrier_l in c.lower():
            ct_l = ct.lower()
            if ct_l == contact_l or ct_l == "general":
                hits.append((ct, instr, src))
    return hits


def apply_knowledge(steps, entries, carrier, contact_type):
    for contact_label, instr, src in knowledge_for(entries, carrier, contact_type):
        tag = f"Agency knowledge \u2014 {contact_label}" if contact_label else "Agency knowledge"
        if src:
            tag += f" (per {src})"
        steps.append(f"{tag}: {instr}")
    return steps


def load_status_map(path):
    """Load a PolicyApi status map JSON.

    ``{policy_number: {"status": ..., "cancellation_date": ...,
    "expiration_date": ...}}``. Returns {} when the file is missing or
    unreadable (callers treat that as "statuses UNVERIFIED").
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _status_of(status_map, policy_number):
    if not status_map:
        return None
    return status_map.get((policy_number or "").strip())


def _dead_4247_action(pol, st):
    cxld = st.get("cancellation_date") or "unknown date"
    return (f"DO NOT WORK \u2014 policy {pol} shows {st['status']} in EZLynx "
            f"(cancelled {cxld}); do not chase a renewal \u2014 verify whether it was "
            f"rewritten/replaced under a new policy number, then remove it from the queue")


def _dead_4744_action(pol, st):
    cxld = st.get("cancellation_date") or "unknown date"
    return (f"DO NOT WORK \u2014 policy {pol} shows {st['status']} in EZLynx "
            f"(cancelled {cxld}); no renewal to verify \u2014 confirm and remove from the queue")


def compose_4247_action(row, today, knowledge=(), status_map=None):
    """Policy-specific action for a 4247 manual-renewal row.

    Returns (action, why). Dead policies get DO NOT WORK instead of
    renewal work — the report carries no status column.
    """
    carrier = (row.get("Master Company") or "").strip()
    lob = (row.get("Line Of Business") or "").strip()
    pol = (row.get("Policy Number") or "").strip()
    exp = parse_date(row.get("Policy Expiration Date", ""))

    days_left = days_between(today, exp) if exp else None

    # Live-status gate (must run before any renewal work is proposed).
    st = _status_of(status_map, pol)
    if st and st.get("status") != "Active":
        why = (f"{lob} policy with {carrier}, expiring {exp or 'unknown'}. "
               f"STATUS CHECK {today}: {st['status']}.")
        if st["status"] in DEAD_STATUSES:
            return _dead_4247_action(pol, st), why
        return (f"Verify policy status ('{st['status']}') in EZLynx before working; "
                f"do not chase a renewal until it shows Active", why)

    if days_left is None:
        urgency = "UNKNOWN TIMING \u2014 no expiration date in report"
    elif days_left < 0:
        urgency = f"EXPIRED {-days_left} days ago \u2014 needs immediate action"
    elif days_left <= 15:
        urgency = f"URGENT \u2014 {days_left} days to expiration"
    elif days_left <= 30:
        urgency = f"Due soon \u2014 {days_left} days to expiration"
    elif days_left <= 45:
        urgency = f"On track \u2014 {days_left} days to expiration"
    else:
        urgency = f"Early \u2014 {days_left} days to expiration"

    steps = []
    if "Progressive" in carrier:
        # BOR takeovers: StreetSmart keyed these in by hand; at renewal
        # the policy should download automatically. Check that first.
        steps.append("BOR takeover: FIRST check if renewal downloaded into EZLynx "
                     "automatically \u2014 if yes, no manual work needed; if no, chase the download")
    elif any(k in carrier for k in MGA_KEYWORDS):
        steps.append(f"Pull renewal docs from {carrier} portal")
    elif "Assigned Risk" in carrier or "NJCRIB" in carrier:
        steps.append(f"Check {carrier} portal for renewal offer/bill")
    else:
        steps.append(f"Pull renewal docs from {carrier} portal; email carrier if not posted")

    if lob == "Flood":
        steps.append("Confirm mortgagee is listed; verify premium with lender requirements")
    elif lob == "Homeowners":
        steps.append("Confirm mortgagee clause; check escrow billing")
    elif lob == "Workers comp":
        steps.append("Note: audit papers will follow after renewal \u2014 separate 4246 track")

    steps.append("If docs missing after portal check: email carrier, then call as last resort")
    steps.append("Report outcome per policy: done / waiting on carrier / waiting on client")

    why = f"{lob} policy with {carrier}, expiring {exp or 'unknown'}. {urgency}."
    steps = apply_knowledge(steps, knowledge, carrier, CONTACT_RENEWALS)
    return "; ".join(steps), why


def compose_4246_action(row, today, knowledge=(), status_map=None):
    """Policy-specific action for a 4246 audit-verification row.

    Returns (action, why). A non-Active policy may still owe a FINAL
    audit, so it gets a verification flag rather than DO NOT WORK.
    """
    carrier = (row.get("Master Company") or "").strip()
    eff = parse_date(row.get("Effective Date", ""))
    status = (row.get("Current Policy Status") or "").strip()
    pol = (row.get("Policy Number") or "").strip()

    days_since_renewal = days_between(eff, today) if eff else None

    if days_since_renewal is None:
        window = "UNKNOWN TIMING \u2014 no effective date in report"
    elif days_since_renewal < 30:
        window = (f"Too early \u2014 only {days_since_renewal} days since renewal "
                  f"(audit window is 30-45 days after)")
    elif days_since_renewal <= 45:
        window = f"In audit window \u2014 {days_since_renewal} days since renewal"
    else:
        window = (f"OVERDUE \u2014 {days_since_renewal} days since renewal, "
                  f"past the 45-day audit window")

    steps = []
    if not carrier:
        steps.append("WARNING: no carrier listed in report \u2014 cannot chase audit papers "
                     "until carrier is identified")
    elif "AmTrust" in carrier:
        steps.append(f"Pull audit papers from AmTrust portal for {pol}")
    elif "Hartford" in carrier or "Travelers" in carrier or "Pie" in carrier:
        steps.append(f"Request audit papers from {carrier} (portal or email)")
    elif "Assigned Risk" in carrier or "NJCRIB" in carrier:
        steps.append(f"Request audit papers from {carrier}")
    else:
        steps.append(f"Chase audit papers from {carrier}")

    steps.append("Send audit papers to client for completion")
    steps.append("Follow up if client does not return within 7 days")

    st = _status_of(status_map, pol)
    if st and st.get("status") != "Active":
        steps.insert(0, f"Policy shows '{st['status']}' in EZLynx \u2014 confirm whether "
                        f"a final audit is still due before chasing papers")

    why = f"WC policy renewed {eff or 'unknown'}, status {status or 'unknown'}. {window}."
    if st and st.get("status") != "Active":
        why += f" STATUS CHECK {today}: {st['status']}."
    steps = apply_knowledge(steps, knowledge, carrier, CONTACT_AUDITS)
    return "; ".join(steps), why


def compose_4744_action(row, today, knowledge=(), status_map=None):
    """Policy-specific action for a 4744 mortgagee-expiration row.

    Returns (action, why). A cancelled policy has no renewal to verify,
    so it gets DO NOT WORK.
    """
    carrier = (row.get("Master Company") or "").strip()
    lob = (row.get("Line Of Business") or "").strip()
    pol = (row.get("Policy Number") or "").strip()
    exp = parse_date(row.get("Policy Expiration Date", ""))
    days_left = days_between(today, exp) if exp else None

    st = _status_of(status_map, pol)
    if st and st.get("status") in DEAD_STATUSES:
        why = (f"{lob} policy with {carrier}, expiring {exp or 'unknown'}. "
               f"STATUS CHECK {today}: {st['status']}.")
        return _dead_4744_action(pol, st), why

    if days_left is None:
        urgency = "UNKNOWN TIMING \u2014 no expiration date in report"
    elif days_left < 0:
        urgency = f"EXPIRED {-days_left} days ago \u2014 verify mortgagee/payment immediately"
    elif days_left < 30:
        urgency = f"URGENT \u2014 {days_left} days to expiration, inside final approach"
    elif days_left <= 45:
        urgency = f"In worker window \u2014 {days_left} days to expiration"
    else:
        urgency = f"Early \u2014 {days_left} days to expiration (outside 30-45d window)"

    steps = [
        "Resolve mortgagee of record (lender name + loan number) from the policy's Additional Interests",
        "Verify lender is still the lender of record \u2014 mismatch = flag, do not pay the wrong lender",
        "Confirm payment: escrow-billed or client-paid; client-paid items go to CSR handoff",
        "Lender portals usually need no login \u2014 use the portal's agent section with loan number + identifiers",
    ]

    if st and st.get("status") != "Active":
        steps.insert(0, f"Verify policy status ('{st['status']}') in EZLynx before working")

    steps = apply_knowledge(steps, knowledge, carrier, CONTACT_MORTGAGEE)
    action = "; ".join(steps)
    why = (f"{lob} policy with {carrier}, expiring {exp or 'unknown'}. {urgency}. "
           f"Lender identity is HOLD until the live Additional Interests read is wired "
           f"\u2014 never a guessed lender.")
    if st and st.get("status") != "Active":
        why += f" STATUS CHECK {today}: {st['status']}."
    return action, why


def detect_policy_conflicts(rows):
    """Find policy numbers appearing on multiple rows.

    Returns {policy_number: sorted [account names]} for numbers with
    more than one row. A shared policy number across accounts is a
    source-data conflict — every affected row must be flagged, never
    silently double-worked.
    """
    accounts = {}
    for row in rows:
        pol = (row.get("Policy Number") or "").strip()
        if not pol:
            continue
        accounts.setdefault(pol, set()).add((row.get("Account Name") or "").strip())
    counts = Counter((row.get("Policy Number") or "").strip()
                     for row in rows if (row.get("Policy Number") or "").strip())
    return {pol: sorted(accts) for pol, accts in accounts.items() if counts[pol] > 1}


def build_preview_workbook(report_specs, today, knowledge=(), status_map=None):
    """Build the preview Excel workbook.

    ``report_specs``: list of dicts with keys:
      - ``sheet``: worksheet title
      - ``columns``: ordered report column names to emit
      - ``rows``: list of row dicts (blank policy numbers skipped)
      - ``composer``: fn(row, today, knowledge, status_map) -> (action, why)

    Conflict-flagged and dead-policy rows are annotated in the action
    column. Returns an openpyxl Workbook (caller saves it).
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    title_font = Font(bold=True, size=14)
    header_fill = PatternFill("solid", fgColor="1F4E78")
    header_font = Font(bold=True, color="FFFFFF")
    warn_fill = PatternFill("solid", fgColor="FCE4EC")

    wb = Workbook()
    first = True
    for spec in report_specs:
        ws = wb.active if first else wb.create_sheet(spec["sheet"])
        if first:
            ws.title = spec["sheet"]
            first = False

        columns = spec["columns"]
        ws["A1"] = f"What the AI Would Do \u2014 {spec['sheet']}"
        ws["A1"].font = title_font
        ws["A2"] = (f"Generated {today.strftime('%Y-%m-%d')} | READ-ONLY | "
                     f"No EZLynx writes, no outreach")

        rows = [r for r in spec["rows"] if (r.get("Policy Number") or "").strip()]
        conflicts = detect_policy_conflicts(rows)
        conflict_counts = Counter((r.get("Policy Number") or "").strip() for r in rows)

        headers = columns + ["What AI Would Do", "Why"]
        for c, h in enumerate(headers, start=1):
            cell = ws.cell(row=4, column=c, value=h)
            cell.fill = header_fill
            cell.font = header_font

        row_num = 6  # blank separator row at 5
        for row in rows:
            action, why = spec["composer"](row, today, knowledge, status_map)
            pol = (row.get("Policy Number") or "").strip()
            flagged = False
            if pol in conflicts:
                others = ", ".join(conflicts[pol])
                action += (f"; DATA CONFLICT \u2014 policy {pol} appears "
                           f"{conflict_counts[pol]}x in this report under accounts "
                           f"({others}); confirm the correct account/carrier before acting")
                flagged = True
            for c, col in enumerate(columns, start=1):
                cell = ws.cell(row=row_num, column=c, value=row.get(col))
                if flagged:
                    cell.fill = warn_fill
            action_cell = ws.cell(row=row_num, column=len(columns) + 1, value=action)
            ws.cell(row=row_num, column=len(columns) + 2, value=why)
            if flagged:
                action_cell.fill = warn_fill
            row_num += 1

        for c in range(1, len(headers) + 1):
            ws.column_dimensions[ws.cell(row=4, column=c).column_letter].width = 28
        ws.column_dimensions[ws.cell(row=4, column=len(columns) + 1).column_letter].width = 90
        ws.column_dimensions[ws.cell(row=4, column=len(columns) + 2).column_letter].width = 70
        ws.freeze_panes = "A6"
    return wb
