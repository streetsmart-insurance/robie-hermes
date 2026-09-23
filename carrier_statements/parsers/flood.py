"""Flood Risk Solutions 'Commission Statement Report' xlsx (commission paid TO the agency, not agency bill).
net = commission due to StreetSmart; tie-out = sum of line commissions vs the sheet's TOTAL row."""
import openpyxl
from model import money, mdy, line, statement

def parse(xlsx_path, statement_date=None, source=None):
    ws = openpyxl.load_workbook(xlsx_path, data_only=True)["Payments"]
    rows = ws.iter_rows(values_only=True)
    hdr = [str(h).strip() if h else "" for h in next(rows)]
    col = lambda name: next(i for i, h in enumerate(hdr) if h.startswith(name))
    cN, cP, cE, cG, cC, cR, cA = (col("Policy Name"), col("Policy Number"), col("Effective Date"),
        col("Premium Total"), col("Commissionable Premium"), col("Agency Commission Override"), col("Producer 1 Projected Commission"))
    lines, total = [], None
    for r in rows:
        if not r or all(c in (None, "") for c in r): continue
        if str(r[0]).strip().upper() == "TOTAL": total = money(r[cA]); continue
        lines.append(line(insured=str(r[cN]).replace(" - Flood", "").strip(), policy=str(r[cP]), txn_type=r[1],
            description=f"{r[0]} {r[1]} flood", eff_date=mdy(r[cE]), gross=money(r[cG]), comm_rate=money(r[cR]),
            comm_amt=money(r[cA]), net=money(r[cA]), kind="commission_due_to_agency", group=str(r[cP])))
    s = statement("Flood Risk Solutions (commission)", None, statement_date, total, lines, {}, source or str(xlsx_path))
    s["direction"] = "carrier pays agency"
    return s
