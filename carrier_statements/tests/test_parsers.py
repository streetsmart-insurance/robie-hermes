"""Wholly invented fixture amounts, identifiers, names and dates."""
import sys,pathlib
from decimal import Decimal as D
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from parsers import rps,asero,spg,xsb,one80,flood
sys.path.insert(0,str(ROOT/'fixtures'));from xlsx_from_json import build
F=ROOT/'fixtures'
def test():
 cases=[
  ('RPS',rps.parse((F/'rps_synthetic.txt').read_text()),D('60.00'),2),
  ('Asero',asero.parse((F/'asero_synthetic.txt').read_text()),D('-60.00'),1),
  ('PPIB',spg.parse((F/'ppib_synthetic.txt').read_text(),'PPIB'),D('90.00'),1),
  ('XPT',spg.parse((F/'xpt_synthetic.txt').read_text(),'XPT'),D('90.00'),1),
  ('XS',xsb.parse((F/'xsb_synthetic.txt').read_text()),D('60.00'),2),
  ('One80',one80.parse(build(F/'one80_synthetic.sheets.json'),statement_date='2025-04-12'),D('90.00'),1),
  ('Flood',flood.parse(build(F/'flood_synthetic.sheets.json'),statement_date='2025-04-12'),D('10.00'),1)]
 failures=[]
 for name,stmt,total,count in cases:
  if not stmt['arithmetic_ties'] or stmt['total_due']!=total or len(stmt['lines'])!=count:
   failures.append(f"{name}: {stmt['total_due']} {len(stmt['lines'])} {stmt['problems']}")
 if not any('quarantine' in flag for l in cases[1][1]['lines'] for flag in l['flags']):failures.append('Asero quarantine')
 if cases[4][1]['ties']:failures.append('XS derived balance falsely accepted')
 if not any(l['txn_type']=='NOT_ITEMIZED' for l in cases[4][1]['lines']):failures.append('XS hidden payment')
 broken=(F/'rps_synthetic.txt').read_text().replace('Policy Balance Due             $60.00','Policy Balance Due             $61.00')
 if rps.parse(broken)['ties']:failures.append('tampered RPS balance ties')
 print('PASS' if not failures else 'FAIL: '+repr(failures));return not failures
if __name__=='__main__':sys.exit(0 if test() else 1)
