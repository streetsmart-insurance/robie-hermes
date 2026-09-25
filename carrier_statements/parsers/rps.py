"""Risk Placement Services 'Broker Statement' PDF (text from pdftotext -layout)."""
import re
from model import money, mdy, line, statement
MONEY = r"\(?\$[\d,]+\.\d\d\)?"
CHARGE = re.compile(r"^\s*(\d{1,2}/\d{2})\s+(\d{6,})\s+([A-Z]+)\s+(.+?)\s+(\d\d/\d\d/\d\d)\s+(\d\d/\d\d/\d\d)\s+(.*)$")
ACTIVITY = re.compile(r"^\s{25,}(\d\d/\d\d/\d\d)\s+(\S.*?)\s{2,}(" + MONEY + r")\s*$")
CONT = re.compile(r"^\s{40,}([A-Za-z*#].*?)\s*$")
BAL = re.compile(r"Policy Balance Due\s+(" + MONEY + ")")
TOTAL = re.compile(r"Balance due this statement\s+(" + MONEY + ")")

def parse(text, source=None):
    acct = (re.search(r"Broker No\.\s+(\S+)", text) or [None, None])[1]
    sdate = re.search(r"Broker Statement\s*\n\s*(\d{1,2}/\d{1,2}/\d{4})", text)
    lines, groups, g, last, total = [], {}, 0, None, None
    cur_policy = cur_insured = None
    for raw in text.splitlines():
        if "Policy Balance Due" in raw:
            groups[g] = money(BAL.search(raw).group(1)); g += 1; last = None; continue
        m = TOTAL.search(raw)
        if m: total = money(m.group(1)); continue
        m = CHARGE.match(raw)
        if m:
            month, inv, typ, pol, eff, due, rest = m.groups()
            mm = re.match(r"(.+?)\s{2,}(.*)$", rest.strip())
            insured, tail = (mm.group(1), mm.group(2)) if mm else (rest.strip(), "")
            amts = re.findall(r"\(?\$[\d,]+\.\d\d\)?|\b\d+\.\d\d\b", tail)
            rate = gross = comm = None
            if len(amts) == 4: rate, gross, comm, net = amts
            elif len(amts) == 1: net = amts[0]
            else: raise ValueError(f"RPS: unexpected amount columns: {raw!r}")
            cur_policy, cur_insured = re.sub(r"\s+", " ", pol.strip()), insured
            last = line(insured=insured, policy=cur_policy, invoice=inv, txn_type=typ, eff_date=mdy(eff),
                        due_date=mdy(due), gross=money(gross), comm_rate=money(rate), comm_amt=money(comm),
                        net=money(net), kind="charge" if typ not in ("SLT", "NJPLIG") else "tax_fee",
                        group=g, description=f"acct month {month}")
            lines.append(last); continue
        m = ACTIVITY.match(raw)
        if m:
            dt, desc, amt = m.groups()
            kind = "payment" if desc.lower().startswith(("payment", "* void")) else "adjustment"
            last = line(insured=cur_insured, policy=cur_policy, txn_type="ACTIVITY", description=desc.strip(),
                        eff_date=mdy(dt), net=money(amt), kind=kind, group=g)
            if "commission retained" in desc.lower(): last["flags"].append("commission retained by carrier")
            lines.append(last); continue
        m = CONT.match(raw)
        if m and last is not None and last["txn_type"] == "ACTIVITY" and "$" not in raw:
            last["description"] += " " + m.group(1)
            if "aging balance" in last["description"].lower() and "commission retained by carrier" not in last["flags"]:
                last["flags"].append("commission retained by carrier")
    return statement("RPS", acct, mdy(sdate.group(1)) if sdate else None, total, lines, groups, source)
