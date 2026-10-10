"""'Account Statement' layout shared by PPIB (Specialty Program Group) and XPT Partners (pdftotext -layout).
Policy header at column 0, indented item/payment lines, 'Inv. N Balance' and 'Policy X Balance' subtotals,
'Account Total' at the end. Some descriptions wrap onto two lines with the invoice number glued on."""
import re
from model import money, mdy, line, statement
NUMS = r"(?P<nums>[\d,().\s]+)"
ITEM = re.compile(r"^\s{8,}(?P<desc>\S.*?)\s+(?:(?P<inv>\d{5,7})\s+)?(?P<d1>\d\d/\d\d/\d\d)(?:\s+(?P<d2>\d\d/\d\d/\d\d))?\s+" + NUMS + "$")
PENDING = re.compile(r"^\s{8,}(?P<desc>\S.*?)\s+(?P<inv>\d{5,7})\s*$")
HEADER = re.compile(r"^(?P<pol>[A-Z0-9][A-Z0-9/\-]{4,})\s{2,}(?P<ins>.+?)\s{2,}(?P<lob>\S.*)$")
POLBAL = re.compile(r"Policy (\S+) Balance\s+(\(?\$[\d,.]+\)?)")
NUM = re.compile(r"\(?\$?[\d,]+\.\d\d\)?")

def parse(text, carrier, source=None):
    acct = (re.search(r"Account No\.\s+(\S+)", text) or [None, None])[1]
    sdate = re.search(r"Statement Date\s+(\d{1,2}/\d{1,2}/\d{2,4})", text)
    total, lines, groups = None, [], {}
    pol = ins = None; pending = None; inv_cur = None
    for raw in text.splitlines():
        raw = re.sub(r"^(\s{8,}.*[a-z])(\d{6})\s*$", r"\1  \2", raw)
        m = POLBAL.search(raw)
        if m: groups[m.group(1)] = money(m.group(2)); continue
        if "Account Total" in raw:
            nums = NUM.findall(raw); total = money(nums[-1]) if nums else None; continue
        if re.search(r"Inv\. \S+ Balance", raw): continue
        m = HEADER.match(raw)
        if m and not raw.startswith(("Policy No", "Remittance", "Thank", "Please")):
            pol, ins = m.group("pol"), m.group("ins").strip(); pending = None; continue
        m = ITEM.match(raw)
        if m and pol:
            desc = m.group("desc").strip(); inv = m.group("inv")
            if pending: desc = pending[0] + " " + desc; inv = inv or pending[1]; pending = None
            if inv: inv_cur = inv
            nums = [money(x) for x in NUM.findall(m.group("nums"))]
            gross = pct = comm = None; net = nums[-1]
            if len(nums) == 4: gross, pct, comm, net = nums
            elif len(nums) == 2: gross, net = nums
            elif len(nums) != 1: raise ValueError(f"{carrier}: unexpected amount columns: {raw!r}")
            is_pay = desc.lower().startswith("payment")
            kind = "payment" if is_pay else ("charge" if comm is not None else "tax_fee")
            if not is_pay and net is not None and net < 0 and kind == "charge": kind = "credit"
            if not is_pay and gross is None and comm is None and m.group("d2") is None: kind = "adjustment"
            lines.append(line(insured=ins, policy=pol, invoice=None if is_pay else inv_cur, txn_type=kind.upper(),
                description=desc, due_date=None if is_pay or not m.group("d2") else mdy(m.group("d1")),
                eff_date=mdy(m.group("d2") or m.group("d1")), gross=gross, comm_rate=pct, comm_amt=comm,
                net=net, kind=kind, group=pol))
            continue
        m = PENDING.match(raw)
        if m and pol: pending = (m.group("desc").strip(), m.group("inv"))
    return statement(carrier, acct, mdy(sdate.group(1)) if sdate else None, total, lines, groups, source)
