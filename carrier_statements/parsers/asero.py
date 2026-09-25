"""Asero Insurance Services (formerly TIP National) STATEMENT PDF (pdftotext -layout)."""
import re
from model import money, mdy, line, statement
NUM = r"-?[\d,]+\.\d\d"
ROW = re.compile(r"^\s*(PA\d+)\s+(?:(\S+)\s+)?(\d\d/\d\d/\d\d)\s+(\d\d/\d\d/\d\d)\s+(\d\d/\d\d/\d\d)\s+(.+?)\s+(-?[\d.]+)%\s+((?:" + NUM + r"\s*)+)$")
CONT = re.compile(r"^\s{50,}(\S.*?)\s*$")

def parse(text, source=None):
    acct = (re.search(r"Customer ID\s+(\S+)", text) or [None, None])[1]
    sdate = re.search(r"Date\s+(\d{1,2}/\d{1,2}/\d{4})", text)
    total = re.search(r"Total Due:\s+(" + NUM + ")", text)
    lines, last = [], None
    for raw in text.splitlines():
        m = ROW.match(raw)
        if m:
            inv, pol, eff, invd, due, desc, pct, nums = m.groups()
            n = [money(x) for x in nums.split()]
            # columns right-to-left: Balance Due, Net Amount, [Financed], Taxes, Fees, Com $
            bal, net_amt = n[-1], n[-2]
            comm = n[0] if len(n) >= 5 else None
            fin = desc.startswith("PRM FINA")
            last = line(insured=desc[len("PRM FINA "):].strip() if fin else desc.strip(), policy=pol, invoice=inv,
                        txn_type="PRM", description=desc.strip(), eff_date=mdy(eff), due_date=mdy(due),
                        gross=net_amt, comm_rate=money(pct), comm_amt=comm, net=bal,
                        kind="credit" if bal < 0 else "charge", group=inv,
                        flags=["finance-company credit - quarantine, may be owed to the PFC"] if fin and bal < 0 else [])
            lines.append(last); continue
        m = CONT.match(raw)
        if m and last is not None and not re.search(r"Total|Formerly", raw):
            last["insured"] += " " + m.group(1); last["description"] += " " + m.group(1)
        if "Total Due for Insured" in raw: last = None
    return statement("Asero (formerly TIP National)", acct, mdy(sdate.group(1)) if sdate else None,
                     money(total.group(1)) if total else None, lines, {}, source)
