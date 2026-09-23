"""Every parser must tie to the printed total on scrubbed real statements. Run: python3 tests/test_parsers.py"""
import sys, pathlib
from decimal import Decimal as D
ROOT = pathlib.Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))
from parsers import rps, asero, spg, xsb, one80, flood
sys.path.insert(0, str(ROOT / "fixtures")); from xlsx_from_json import build
F = ROOT / "fixtures"
txt = lambda n: (F / n).read_text()
CASES = [  # name, parse(), printed total, line count, per-carrier checks
 ("RPS 8/31", lambda: rps.parse(txt("rps_2026-08-31.txt")), D("18916.08"), 21),
 ("Asero 9/21", lambda: asero.parse(txt("asero_2026-09-21.txt")), D("-16028.24"), 8),
 ("PPIB 9/01", lambda: spg.parse(txt("ppib_2026-09-01.txt"), "PPIB"), D("1430.70"), 7),
 ("XPT 9/04", lambda: spg.parse(txt("xpt_2026-09-04.txt"), "XPT Partners"), D("-4159.19"), 31),
 ("XS Brokers 9/01", lambda: xsb.parse(txt("xsb_2026-09-01.txt")), D("4160.25"), 28),
 ("One80 9/22", lambda: one80.parse(build(F / "one80_2026-09-22.sheets.json"), statement_date="2026-09-22"), D("2963.87"), 16),
 ("Flood 8/26", lambda: flood.parse(build(F / "flood_2026-08-26.sheets.json")), D("668.69"), 3),
]
def test():
    bad = []
    for name, fn, total, n in CASES:
        s = fn()
        if not s["ties"] or s["total_due"] != total or len(s["lines"]) != n:
            bad.append(f"{name}: ties={s['ties']} total={s['total_due']} lines={len(s['lines'])} {s['problems']}")
    s = rps.parse(txt("rps_2026-08-31.txt"))
    if sum(1 for l in s["lines"] if "commission retained by carrier" in l["flags"]) != 2: bad.append("RPS: commission-retained flags")
    if sorted(s["groups"].values())[-1] != D("13317.20"): bad.append("RPS: largest policy balance")
    a = asero.parse(txt("asero_2026-09-21.txt"))
    if sum(1 for l in a["lines"] if any("quarantine" in f for f in l["flags"])) != 7: bad.append("Asero: 7 PFC credits should be quarantined")
    x = xsb.parse(txt("xsb_2026-09-01.txt"))
    if sum(1 for l in x["lines"] if l["txn_type"] == "NOT_ITEMIZED") != 2: bad.append("XSB: not-itemized payment lines")
    broken = txt("rps_2026-08-31.txt").replace("$718.10", "$719.10", 1)   # a printed balance that doesn't tie must stop
    if rps.parse(broken)["ties"]: bad.append("RPS: tampered balance still ties")
    print("PASS" if not bad else "FAIL\n" + "\n".join(bad)); return not bad
if __name__ == "__main__":
    sys.exit(0 if test() else 1)
