"""Build SCRUBBED test fixtures from real statements (run locally; real files are never committed).
Insured names and policy numbers are replaced with same-length fakes so column layout is unchanged;
amounts, dates and structure stay real. Bank/remittance footers are dropped."""
import sys, re, random, shutil, json, openpyxl
sys.path.insert(0, ".")
from registry import parse_file, pdf_text

def fake_like(s, rnd):
    return "".join(rnd.choice("ABCDEFGHJKLMNPRSTVWXYZ") if c.isupper() else rnd.choice("abcdefghijkmnoprstuvwyz") if c.islower()
                   else rnd.choice("0123456789") if c.isdigit() else c for c in s)

def scrub_text(carrier, path, out, extra=()):
    rnd = random.Random(carrier); s = parse_file(carrier, path); text = pdf_text(path)
    names = {l["insured"] for l in s["lines"] if l["insured"]} | {l["policy"] for l in s["lines"] if l["policy"]} | set(extra)
    for n in sorted(names, key=len, reverse=True):
        for part in [n] + n.split(" "):   # wrapped insured names appear in pieces
            if len(part) >= 2 and part.upper() not in ("LLC", "INC", "INC.", "CORP", "THE", "AND", "OF", "CO", "PRM", "FINA"):
                text = text.replace(part, fake_like(part, rnd))
    text = re.split(r"\n\s*(Remittance Address:|To remit payment via ACH)", text)[0]
    text = re.sub(r"(Routing Number|Account Number):\s*\d+", r"\1: XXXX", text)
    open(out, "w").write(text)

def scrub_xlsx(path, out, cols):
    rnd = random.Random(out); wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.worksheets[0]; seen = {}
    for row in ws.iter_rows(min_row=2):
        for c in row:
            if c.column in cols and isinstance(c.value, str) and c.value.strip() and c.value.strip() not in ("Grand Total", "TOTAL"):
                seen.setdefault(c.value, fake_like(c.value, rnd)); c.value = seen[c.value]
    wb.save(out)

# xlsx fixtures are then dumped to fixtures/*.sheets.json (see fixtures/xlsx_from_json.py) and the .xlsx deleted.
if __name__ == "__main__":
    D = "/downloads/"
    scrub_text("rps", D + "8-2026 statement for A0027426.pdf", "fixtures/rps_2026-08-31.txt")
    scrub_text("asero", D + "AG002620_2026_09.pdf", "fixtures/asero_2026-09-21.txt")
    scrub_text("ppib", D + "ppib_2026-09-01.pdf", "fixtures/ppib_2026-09-01.txt")
    scrub_text("xpt", D + "xpt_2026-09-04.pdf", "fixtures/xpt_2026-09-04.txt", extra=["3AA856953"])
    scrub_text("xsb", D + "xsb_2026-09-01.pdf", "fixtures/xsb_2026-09-01.txt")
    scrub_xlsx(D + "Statement Diesel Insurance Solutions – Street Smart Insurance - Freehold, NJ-4c5fa9 - IEX - 2026-09-22 - AB.xlsx",
               "fixtures/one80_2026-09-22.xlsx", cols={1, 2, 5, 14})
    scrub_xlsx(D + "flood_2026-08-26.xlsx", "fixtures/flood_2026-08-26.xlsx", cols={3, 4})
