"""Parse Applied Pay payout line detail (the portal text dump format) into snapshot payouts."""
import re, sys, json
from datetime import datetime
HEAD = re.compile(r"^== (\d\d/\d\d/\d{4}) (\S+) payout \$([\d,.-]+) USD conv \$([\d,.]+) USD \| lines (\d+) Items? net-of-lines ([\d.-]+)")
LINE = re.compile(r"^\s+(\d\d/\d\d/\d{4}) (\w+) ([\d.]+) conv ([\d.]+) (\w+) (\S+) \| (.*?) \| (.*?) \| inv (.*?) \| desc (.*?) \| pol (.*)$")
TYPES = {"sale": "sale", "return": "refund", "refund": "refund", "chargeback": "ach_chargeback", "reversal": "reversal"}
def iso(s): return datetime.strptime(s, "%m/%d/%Y").date().isoformat()
def parse(text):
    payouts, cur = [], None
    for raw in text.splitlines():
        m = HEAD.match(raw)
        if m:
            cur = {"payout_date": iso(m[1]), "ref": m[2], "net": m[3].replace(",", ""), "portal_conv_total": m[4],
                   "portal_line_count": int(m[5]), "portal_net_of_lines": m[6], "lines": []}
            payouts.append(cur); continue
        m = LINE.match(raw)
        if m and cur is not None:
            typ = TYPES.get(m[2].lower(), "unknown:" + m[2])
            amt = m[3] if typ == "sale" else "-" + m[3]
            n = lambda v: None if v.strip() in ("-", "") else v.strip()
            cur["lines"].append({"txn_date": iso(m[1]), "type": typ, "amount": amt, "conv_fee": m[4], "method": m[5],
                                 "psp_ref": m[6], "payer": n(m[7]), "business": n(m[8]), "invoice": n(m[9]),
                                 "description": n(m[10]), "policy": n(m[11])})
        elif raw.strip() and cur is not None and not raw.startswith("=="):
            raise ValueError(f"unparsed line: {raw!r}")
    for p in payouts:
        if len(p["lines"]) != p["portal_line_count"]:
            raise ValueError(f"{p['ref']}: parsed {len(p['lines'])} lines, portal says {p['portal_line_count']}")
    return payouts
if __name__ == "__main__":
    print(json.dumps(parse(open(sys.argv[1]).read()), indent=1))
