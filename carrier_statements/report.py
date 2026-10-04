"""Per-statement summary for reconciliation: open items, past due, credits, flags. PROPOSE ONLY."""
from decimal import Decimal
Z = Decimal("0")

def summarize(s):
    out = [f"## {s['carrier']}  acct {s['account_no'] or '-'}  statement {s['statement_date']}",
           f"- printed total: {s['total_due']}  | lines: {len(s['lines'])}  | ties: {'YES' if s['ties'] else 'NO - STOP'}"]
    out += [f"  - problem: {p}" for p in s["problems"]]
    if not s["ties"]:
        out.append("- not reconciled: fix the parse or read the statement by hand first"); return "\n".join(out)
    groups = {}
    for l in s["lines"]: groups.setdefault(l["group"], []).append(l)
    for g, ls in groups.items():
        bal = sum((l["net"] or Z) for l in ls)
        if bal == Z and not any(l["flags"] for l in ls): continue
        first = ls[0]; dues = [l["due_date"] for l in ls if l["due_date"]]
        due = min(dues) if dues else None
        tag = "credit" if bal < 0 else ("PAST DUE" if due and s["statement_date"] and due < s["statement_date"] else "open")
        flags = sorted({f for l in ls for f in l["flags"]})
        out.append(f"- {first['insured']} | {first['policy'] or g} | balance {bal} | due {due or '-'} | {tag}"
                   + (f" | flags: {'; '.join(flags)}" if flags else ""))
    return "\n".join(out)
