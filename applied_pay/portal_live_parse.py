"""Parse the Applied Pay Payouts page (all batches expanded, read as text) into payouts JSON.
Each batch: TransferID, date, merchant, total, conv, proc, recaptured, then a 15-col header and
15-field rows, ending with 'N Item(s)'. Verifies row count per batch."""
import re, sys, json
from datetime import datetime
COLS = ["Date","Record Type","Invoice Number","Purchase Amount","Convenience Fee Amount","Authorized Amount","Payout Amount",
        "PSP Reference ID","Account Number","Payment Method","Customer Name","Business Name","Description","Policy Number","Workflow Detail"]
TYPES = {"sale": "sale", "return": "refund", "refund": "refund", "chargeback": "ach_chargeback", "reversal": "reversal"}
money = lambda s: s.replace("$", "").replace(" USD", "").replace(",", "").strip()
nz = lambda v: None if v.strip() in ("-", "") else v.strip()
def parse(text):
    L = [l.strip() for l in text.splitlines()]
    out, i = [], 0
    while i < len(L):
        if re.fullmatch(r"38[A-Z0-9]{12,16}", L[i]) and i + 1 < len(L) and re.match(r"\d\d/\d\d/\d{4}", L[i+1]):
            p = {"ref": L[i], "payout_date": datetime.strptime(L[i+1][:10], "%m/%d/%Y").date().isoformat(),
                 "net": money(L[i+3]), "portal_conv_total": money(L[i+4]), "lines": []}
            i += 7
            if L[i:i+15] != COLS: raise ValueError(f"{p['ref']}: batch not expanded or header changed at line {i}")
            i += 15
            while not re.fullmatch(r"\d+ Items?", L[i]):
                r = dict(zip(COLS, L[i:i+15])); i += 15
                typ = TYPES.get(r["Record Type"].lower(), "unknown:" + r["Record Type"])
                amt = money(r["Payout Amount"])
                if typ != "sale" and not amt.startswith("-"): amt = "-" + money(r["Purchase Amount"])
                p["lines"].append({"txn_date": datetime.strptime(r["Date"][:10], "%m/%d/%Y").date().isoformat(), "type": typ,
                    "amount": amt, "purchase_amount": money(r["Purchase Amount"]), "conv_fee": money(r["Convenience Fee Amount"]),
                    "method": r["Payment Method"], "psp_ref": r["PSP Reference ID"], "payer": nz(r["Customer Name"]),
                    "business": nz(r["Business Name"]), "invoice": nz(r["Invoice Number"]), "description": nz(r["Description"]),
                    "policy": nz(r["Policy Number"])})
            n = int(L[i].split()[0])
            if n != len(p["lines"]): raise ValueError(f"{p['ref']}: parsed {len(p['lines'])} rows, portal says {n} (virtual scroll?)")
            out.append(p)
        i += 1
    return out
if __name__ == "__main__": print(json.dumps(parse(open(sys.argv[1]).read()), indent=1))
