"""Sender domain -> parser. Unknown senders are reported, never guessed."""
import subprocess
from parsers import rps, asero, one80, spg, xsb, flood
SENDERS = {"rpsins.com": "rps", "tipnational.com": "asero", "aseroins.com": "asero", "one80.com": "one80",
           "specialtyprogramgroup.com": "ppib", "xptspecialty.com": "xpt", "xsbrokers.com": "xsb", "floodsol.com": "flood"}

def carrier_for(from_addr):
    dom = from_addr.lower().rsplit("@", 1)[-1].strip(">")
    for k, v in SENDERS.items():
        if dom == k or dom.endswith("." + k): return v
    return None

def pdf_text(path):
    return subprocess.run(["pdftotext", "-layout", path, "-"], capture_output=True, text=True, check=True).stdout

def parse_file(carrier, path):
    if carrier == "rps": return rps.parse(pdf_text(path), source=path)
    if carrier == "asero": return asero.parse(pdf_text(path), source=path)
    if carrier == "one80": return one80.parse(path, source=path)
    if carrier == "ppib": return spg.parse(pdf_text(path), "PPIB", source=path)
    if carrier == "xpt": return spg.parse(pdf_text(path), "XPT Partners", source=path)
    if carrier == "xsb": return xsb.parse(pdf_text(path), source=path)
    if carrier == "flood": return flood.parse(path, source=path)
    raise KeyError(carrier)
