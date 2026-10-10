"""XS Brokers 'Account Statement' PDF (pdftotext -layout). Lists charges per policy but NOT the payments
already applied; the printed 'Policy Balance' is authoritative, so the difference is added as a flagged
'not itemized on statement' line per policy (arithmetic only, with a hold; never payment or clearing proof)."""
import re
from decimal import Decimal
from model import money, mdy, line, statement
NUM = re.compile(r"\(?\$?[\d,]+\.\d\d\)?")
ITEM = re.compile(r"^\s{10,}(\d\d/\d\d/\d\d)\s+(\S.*?)\s{2,}([\d,().\s]+)$")
BAL = re.compile(r"Policy Balance\s+(\(?\$[\d,.]+\)?)\s+(\d\d/\d\d/\d\d)")
TOTAL = re.compile(r"(\(?\$[\d,.]+\)?)\s+TOTAL\s*$")
SKIP = ("Policy #", "StreetSmart", "Streetsmart", "Page", "Agt", "Fiercely", "Proudly", "Premium must", "Thank")

def parse(text, source=None):
    acct = (re.search(r"Account No\.\s+(\S+)", text) or [None, None])[1]
    sdate = re.search(r"Statement Date\s+(\d{1,2}/\d{1,2}/\d{2,4})", text)
    lines, groups, total, pol, ins = [], {}, None, None, None
    for raw in text.splitlines():
        m = TOTAL.search(raw)
        if m: total = money(m.group(1)); continue
        m = BAL.search(raw)
        if m and pol:
            bal = money(m.group(1)); groups[pol] = bal
            listed = sum((l["net"] for l in lines if l["group"] == pol), Decimal("0"))
            if listed != bal:
                lines.append(line(insured=ins, policy=pol, txn_type="NOT_ITEMIZED", kind="adjustment",
                    description="payments/credits applied but not itemized on statement", net=bal - listed,
                    due_date=mdy(m.group(2)), group=pol, flags=["derived from printed policy balance"]))
            for l in lines:
                if l["group"] == pol and l["due_date"] is None: l["due_date"] = mdy(m.group(2))
            continue
        m = ITEM.match(raw)
        if m and pol:
            nums = [money(x) for x in NUM.findall(m.group(3))]
            gross = pct = comm = None; net = nums[-1]
            if len(nums) == 4: gross, pct, comm, net = nums
            elif len(nums) == 2: gross, net = nums
            elif len(nums) != 1: raise ValueError(f"XSB: unexpected amount columns: {raw!r}")
            desc = m.group(2).strip()
            kind = "charge" if comm is not None else "tax_fee"
            if kind == "charge" and net < 0: kind = "credit"
            lines.append(line(insured=ins, policy=pol, txn_type=kind.upper(), description=desc, eff_date=mdy(m.group(1)),
                gross=gross, comm_rate=pct, comm_amt=comm, net=net, kind=kind, group=pol))
            continue
        if raw[:1].strip() and not raw.startswith(SKIP) and not NUM.search(raw):
            parts = re.split(r"\s{2,}", raw.strip(), maxsplit=1)
            if len(parts) == 1:
                g = re.match(r"^(.*?\d)([A-Z][a-z].*)$", parts[0]); parts = [g.group(1), g.group(2)] if g else parts
            if len(parts) == 2: pol, ins = parts[0].strip(), parts[1].strip()
    return statement("XS Brokers", acct, mdy(sdate.group(1)) if sdate else None, total, lines, groups, source)
