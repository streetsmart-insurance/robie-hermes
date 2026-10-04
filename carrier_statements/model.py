"""Common statement structure every carrier parser produces. Read-only; no network."""
import re
from decimal import Decimal

def money(s):
    """'$1,234.56' / '($30.19)' / '-1,550.84' / 12.5 -> Decimal (None for blank)."""
    if s is None: return None
    if isinstance(s, (int, float, Decimal)): return Decimal(str(s)).quantize(Decimal("0.01"))
    t = str(s).strip()
    if not t: return None
    neg = t.startswith("(") and t.endswith(")") or t.startswith("-")
    t = re.sub(r"[()$,\s-]", "", t)
    if not t: return None
    d = Decimal(t).quantize(Decimal("0.01"))
    return -d if neg else d

def mdy(s):
    """'07/31/26' or '7/31/2026' -> '2026-07-31'."""
    if s is None or s == "": return None
    if hasattr(s, "strftime"): return s.strftime("%Y-%m-%d")
    m, d, y = str(s).strip().split("/")
    y = int(y); y = y + 2000 if y < 100 else y
    from datetime import date
    return date(y, int(m), int(d)).isoformat()

def line(**kw):
    base = dict(insured=None, policy=None, invoice=None, txn_type=None, description=None,
                eff_date=None, due_date=None, gross=None, comm_rate=None, comm_amt=None,
                net=None, kind="charge", group=None, flags=[])
    base.update(kw); base["flags"] = list(base["flags"]); return base

def statement(carrier, account_no, statement_date, total_due, lines, groups, source):
    """groups: {group_key: printed balance or None}. Adds tie-out checks."""
    s = dict(carrier=carrier, account_no=account_no, statement_date=statement_date,
             total_due=total_due, lines=lines, groups=groups, source=source, problems=[])
    if any(l["net"] is None for l in lines):
        s["problems"].append("missing line net amount")
    tot = sum((l["net"] if l["net"] is not None else Decimal("0")) for l in lines)
    s["lines_total"] = tot
    if total_due is None: s["problems"].append("no printed total found")
    elif tot != total_due: s["problems"].append(f"lines total {tot} != printed total {total_due}")
    for g, printed in groups.items():
        if printed is None: continue
        gt = sum((l["net"] or Decimal("0")) for l in lines if l["group"] == g)
        if gt != printed: s["problems"].append(f"group {g}: lines {gt} != printed balance {printed}")
    s["arithmetic_ties"] = not s["problems"]
    if any(l.get("txn_type") == "NOT_ITEMIZED" for l in lines):
        s["problems"].append("unitemized balance difference is not verified payment evidence")
    s["ties"] = not s["problems"]
    return s

def to_json(s):
    import json
    return json.loads(json.dumps(s, default=str))
