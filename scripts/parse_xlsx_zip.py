import zipfile
import xml.etree.ElementTree as ET
import json
import re

xlsx_path = "/Users/carloferrara/Documents/antigravity/happy-fermi/data/APE_policies.xlsx"

with zipfile.ZipFile(xlsx_path, 'r') as z:
    # Read workbook.xml to get sheet names and ids
    wb_xml = z.read("xl/workbook.xml")
    root = ET.fromstring(wb_xml)
    ns = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
          "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    
    sheets = []
    for s in root.findall(".//main:sheet", ns):
        name = s.attrib["name"]
        sheet_id = s.attrib["sheetId"]
        r_id = s.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
        sheets.append({"name": name, "sheetId": sheet_id, "rId": r_id})
        
    print("Sheets in workbook:", sheets)
    
    # Read shared strings if any
    shared_strings = []
    if "xl/sharedStrings.xml" in z.namelist():
        ss_xml = z.read("xl/sharedStrings.xml")
        ss_root = ET.fromstring(ss_xml)
        for si in ss_root.findall(".//main:si", ns):
            t_el = si.find(".//main:t", ns)
            if t_el is not None and t_el.text:
                shared_strings.append(t_el.text)
            else:
                # might be multiple r/t
                text_parts = [t.text for t in si.findall(".//main:t", ns) if t.text]
                shared_strings.append("".join(text_parts))
    print(f"Loaded {len(shared_strings)} shared strings.")
    
    # Read workbook rels
    wb_rels_xml = z.read("xl/_rels/workbook.xml.rels")
    wb_rels_root = ET.fromstring(wb_rels_xml)
    rel_map = {}
    for r in wb_rels_root:
        rel_map[r.attrib["Id"]] = r.attrib["Target"]
        
    all_data = {}
    
    for s in sheets:
        sheet_target = "xl/" + rel_map[s["rId"]]
        print(f"\n=================== SHEET: {s['name']} ({sheet_target}) ===================")
        sheet_xml = z.read(sheet_target)
        s_root = ET.fromstring(sheet_xml)
        
        # Check hyperlinks rels for this sheet
        sheet_rels_target = f"xl/worksheets/_rels/{sheet_target.split('/')[-1]}.rels"
        hyperlinks_map = {}
        if sheet_rels_target in z.namelist():
            s_rels_root = ET.fromstring(z.read(sheet_rels_target))
            for r in s_rels_root:
                hyperlinks_map[r.attrib["Id"]] = r.attrib.get("Target", "")
        
        # Cell hyperlinks in sheet.xml
        cell_links = {}
        for hl in s_root.findall(".//main:hyperlink", ns):
            ref = hl.attrib.get("ref", "")
            r_id = hl.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id", "")
            if r_id and r_id in hyperlinks_map:
                cell_links[ref] = hyperlinks_map[r_id]
                
        # Parse rows and cells
        rows_data = []
        for row in s_root.findall(".//main:row", ns):
            r_idx = row.attrib.get("r", "")
            row_cells = []
            for c in row.findall(".//main:c", ns):
                cell_ref = c.attrib.get("r", "")
                cell_type = c.attrib.get("t", "")
                val_el = c.find(".//main:v", ns)
                val = ""
                if val_el is not None and val_el.text:
                    if cell_type == "s":
                        idx = int(val_el.text)
                        val = shared_strings[idx] if idx < len(shared_strings) else ""
                    else:
                        val = val_el.text
                elif cell_type == "inlineStr":
                    t_el = c.find(".//main:t", ns)
                    if t_el is not None and t_el.text:
                        val = t_el.text
                        
                link = cell_links.get(cell_ref, "")
                if link:
                    row_cells.append(f"{val} [URL: {link}]")
                elif val:
                    row_cells.append(val)
                else:
                    row_cells.append("")
                    
            while row_cells and not row_cells[-1]:
                row_cells.pop()
            if any(row_cells):
                print(f"  Row {r_idx}: {' | '.join(row_cells)}")
                rows_data.append({"row": r_idx, "cells": row_cells})
                
        all_data[s["name"]] = rows_data

with open("/Users/carloferrara/Documents/antigravity/happy-fermi/data/ape_sheets_extracted.json", "w") as f:
    json.dump(all_data, f, indent=2)

print("\nSuccessfully extracted all sheets and saved to data/ape_sheets_extracted.json!")
