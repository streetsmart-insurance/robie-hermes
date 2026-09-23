"""Rebuild a scrubbed xlsx fixture from its .sheets.json (keeps binary files out of git)."""
import json, datetime, tempfile, openpyxl

def build(json_path):
    wb = openpyxl.Workbook(); wb.remove(wb.active)
    for title, rows in json.load(open(json_path)).items():
        ws = wb.create_sheet(title)
        for r in rows:
            ws.append([datetime.datetime.fromisoformat(v) if isinstance(v, str) and len(v) == 19 and v[4] == "-" and v[10] == "T" else v for v in r])
    out = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False).name; wb.save(out); return out
