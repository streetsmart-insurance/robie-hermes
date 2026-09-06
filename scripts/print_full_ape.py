import json

with open("/Users/carloferrara/Documents/antigravity/happy-fermi/data/ape_sheets_extracted.json") as f:
    data = json.load(f)

for sname, rows in data.items():
    print(f"\n=================== {sname} ({len(rows)} rows) ===================")
    for r in rows:
        row_num = r['row']
        content = "  |  ".join(r['cells'])
        print(f"[R{row_num}] {content}\n")
