"""One80 / Diesel (IEX - Intl Excess Program Managers) 'Agency Bill Statement' xlsx."""
import re, openpyxl
from model import money, mdy, line, statement

def parse(xlsx_path, statement_date=None, source=None):
    ws = openpyxl.load_workbook(xlsx_path, data_only=True).worksheets[0]
    rows = ws.iter_rows(values_only=True)
    hdr = [str(h).strip() if h else "" for h in next(rows)]
    ix = {h: i for i, h in enumerate(hdr)}
    lines, groups, total = [], {}, None
    for r in rows:
        if not r or all(c in (None, "") for c in r): continue
        ins, pol = (r[ix["InsuredPolicyName"]] or ""), (r[ix["PolicyNumber"]] or "")
        if str(ins).strip() == "Grand Total": total = money(r[ix["NetDue"]]); continue
        if not pol:   # insured subtotal row
            groups[str(ins).strip()] = money(r[ix["NetDue"]]); continue
        charge = r[ix["ChargeName"]] or ""
        kind = "tax_fee" if re.search(r"tax|fee", charge, re.I) and not charge.lower().startswith("premium") else "charge"
        if money(r[ix["NetDue"]]) < 0: kind = "credit" if kind == "charge" else kind
        lines.append(line(insured=str(ins).strip(), policy=str(pol).strip(), invoice=str(r[ix["OfficeInvoiceNum"]]),
            txn_type=r[ix["LOB"]], description=charge.strip(), eff_date=mdy(r[ix["EffectiveDate"]]),
            due_date=None, gross=money(r[ix["AmtBilled"]]), comm_rate=money(r[ix["Comm %"]]),
            comm_amt=money(r[ix["Comm Amt"]]), net=money(r[ix["NetDue"]]), kind=kind, group=str(ins).strip()))
    if statement_date is None:
        m = re.search(r"(\d{4}-\d\d-\d\d)", str(xlsx_path)); statement_date = m and m.group(1)
    return statement("One80 / Diesel (IEX)", None, statement_date, total, lines, groups, source or str(xlsx_path))
