"""Build the QBO side of a matcher snapshot, read-only: EZLynx receipt / -R reversal JEs with
their bank-side account, and deposits into Trust Checking WF (3021) (proxy for Wells deposits;
sound only for reconciled periods). Also records which JEs each existing deposit already groups."""
import json, re, sys
from qbo_client import QBO
RX = re.compile(r"EZLynx Receipt(?: reversal)? (\d{6}(?:-R)?)", re.I)
TRUST_3021_ID = "27"
CASH_SIDE = {"Undeposited Funds", "Trust", "Trust Checking WF (3021)", "Manasqan - Trust"}
def build(start, end, dep_start, dep_end):
    q = QBO()
    jes = q.query(f"SELECT * FROM JournalEntry WHERE TxnDate >= '{start}' AND TxnDate <= '{end}'")
    ledger, skipped = [], []
    for j in jes:
        lines = [l for l in j["Line"] if "JournalEntryLineDetail" in l]
        m = next((RX.search(l.get("Description") or "") for l in lines if RX.search(l.get("Description") or "")), None)
        if not m: continue
        no = m.group(1)
        un = [l for l in lines if l["JournalEntryLineDetail"]["AccountRef"].get("name") == "Unapplied Cash"]
        other = [l for l in lines if l not in un]
        if len(un) != 1 or len(other) != 1:
            skipped.append({"je_id": j["Id"], "receipt_no": no, "why": "not a 2-line receipt/reversal JE (e.g. fee application)"}); continue
        u, b = un[0], other[0]
        bank_name = b["JournalEntryLineDetail"]["AccountRef"].get("name")
        if bank_name not in CASH_SIDE:
            skipped.append({"je_id": j["Id"], "receipt_no": no, "why": f"application to {bank_name}, not a cash receipt/reversal"}); continue
        is_rev = "reversal" in (m.string or "").lower() or no.endswith("-R")
        debit_uc = u["JournalEntryLineDetail"]["PostingType"] == "Debit"
        if is_rev != debit_uc:
            skipped.append({"je_id": j["Id"], "receipt_no": no, "why": "direction does not fit receipt/reversal label"}); continue
        kind = "reversal" if is_rev else "receipt"
        ledger.append({"receipt_no": no if kind == "receipt" or no.endswith("-R") else no + "-R", "je_id": j["Id"],
                       "customer": ((u["JournalEntryLineDetail"].get("Entity") or {}).get("EntityRef") or {}).get("name", ""),
                       "amount": f"{u['Amount']:.2f}", "date": j["TxnDate"], "bank_account": b["JournalEntryLineDetail"]["AccountRef"].get("name"),
                       "kind": kind})
    deps = q.query(f"SELECT * FROM Deposit WHERE TxnDate >= '{dep_start}' AND TxnDate <= '{dep_end}'")
    bank, grouped = [], {}
    for d in deps:
        if d["DepositToAccountRef"]["value"] != TRUST_3021_ID: continue
        links = []
        for l in d["Line"]:
            lt = [t for t in l.get("LinkedTxn", []) if t.get("TxnType") == "JournalEntry"]
            if lt: links.append(lt[0]["TxnId"])
            else: links.append("direct:" + ((l.get("DepositLineDetail") or {}).get("AccountRef") or {}).get("name", "?") + f":{l.get('Amount')}")
        bank.append({"id": "QBO-DEP-" + d["Id"], "date": d["TxnDate"], "amount": f"{d['TotalAmt']:.2f}",
                     "memo": (d.get("PrivateNote") or "")[:120], "groups": links})
        for x in links: grouped[x] = d["Id"]
    for r in ledger: r["deposited_in"] = grouped.get(r["je_id"])
    return {"ledger": ledger, "bank_deposits": bank, "skipped_jes": skipped}
if __name__ == "__main__":
    start, end, ds, de, out = sys.argv[1:6]
    json.dump(build(start, end, ds, de), open(out, "w"), indent=1)
