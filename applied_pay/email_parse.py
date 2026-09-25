"""Parse Applied Pay 'Batch Settlement Details' emails (body + xlsx) into the same
payout structure portal_live_parse produces. Read-only; no network."""
import re, json, datetime, email.utils, openpyxl
from decimal import Decimal
from zoneinfo import ZoneInfo
ET = ZoneInfo("America/New_York")

def _money(v):
    if v in (None, ""): return Decimal("0")
    s = str(v).replace("$", "").replace(",", "").strip()
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    d = Decimal(s or "0")
    return -d if neg else d

def _date(v):
    if isinstance(v, (datetime.date, datetime.datetime)): return v.strftime("%Y-%m-%d")
    m, d, y = str(v).split("/"); return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"

def _rows(ws):
    it = ws.iter_rows(values_only=True)
    hdr = [str(h).strip() if h else "" for h in next(it)]
    for r in it:
        if not r or all(c in (None, "") for c in r): continue
        yield dict(zip(hdr, r))

def parse(body_text, email_date, xlsx_path):
    tid = re.search(r"Transfer ID:\s*([A-Z0-9]+)", body_text).group(1)
    net = _money(re.search(r"Total Deposit:\s*(\(?-?\$?[\d,]+\.\d\d\)?)", body_text).group(1))
    dt = email.utils.parsedate_to_datetime(email_date).astimezone(ET)
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    lines, conv = [], Decimal("0")
    for name in wb.sheetnames:
        ws = wb[name]; low = name.lower()
        if low.startswith("pending"): continue
        if low.startswith("transactions"):
            for r in _rows(ws):
                fee = _money(r.get("Convenience Fee")); conv += fee
                lines.append(dict(txn_date=_date(r["Authorized Date"]), type="sale",
                    amount=str(_money(r.get("Settlement Amount") or r.get("Premium"))),
                    purchase_amount=str(_money(r.get("Premium"))), conv_fee=str(fee),
                    method=(r.get("Type") or "").strip(), psp_ref=(r.get("PSP Reference ID") or "").strip(),
                    payer=(r.get("Customer Name") or "").strip(), business=(r.get("Business Name") or "").strip(),
                    invoice=r.get("Invoice") or None, description=r.get("Description") or None, policy=r.get("Policy #") or None))
        elif low.startswith("returns"):
            for r in _rows(ws):
                reason = (r.get("Reason") or "").strip().lower(); method = (r.get("Type") or "").strip()
                typ = "refund" if reason == "refund" else f"{method}_chargeback"
                lines.append(dict(txn_date=_date(r["Authorized Date"]), type=typ,
                    amount=str(-_money(r.get("Returned Amount"))), purchase_amount=str(_money(r.get("Premium"))),
                    conv_fee=str(_money(r.get("Fee"))), method=method, reason=reason,
                    psp_ref=(r.get("PSP Reference ID") or "").strip(), payer=(r.get("Customer Name") or "").strip(),
                    business=(r.get("Business Name") or "").strip(), invoice=r.get("Invoice") or None,
                    description=r.get("Description") or None, policy=r.get("Policy #") or None))
    total = sum(Decimal(l["amount"]) for l in lines)
    return dict(ref=tid, payout_date=dt.strftime("%Y-%m-%d"), net=str(net), status="scheduled", portal_conv_total=str(conv),
                lines=lines, source="email", lines_sum=str(total), lines_tie=(total == net))
