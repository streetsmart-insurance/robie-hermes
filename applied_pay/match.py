"""Applied Pay payout matcher - phase 1, READ-ONLY.

Inputs are plain JSON snapshots (pulled elsewhere); this module never talks to
EZLynx, QBO or the bank and never posts anything. It proposes Bank Deposits and
reports every line into one of four buckets:
  ready            - every line + bank deposit matched, deposit proposal built
  waiting_approval - matched, but needs a person (alias, wash, reduction w/o rule...)
  unmatched        - stop: reason given, never guessed
  past_cutoff      - payout after the period cutoff
"""
from __future__ import annotations
import itertools, json, re, sys
from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
from decimal import Decimal

UNDEPOSITED = "Undeposited Funds"
TRUST_3021 = "10002 Trust Checking WF (3021)"
REDUCTION_TYPES = {"refund", "ach_chargeback", "chargeback", "reversal"}
SMALL_LINE = Decimal("25.00")
RECEIPT_LOOKBACK_DAYS = 30
STOPWORDS = {"llc", "inc", "co", "corp", "the", "and", "of", "ltd", "llp", "services",
             "service", "transportation", "transport", "trucking", "logistics"}

def D(x) -> Decimal: return Decimal(str(x)).quantize(Decimal("0.01"))
def d(s) -> date: return date.fromisoformat(s)

def tokens(*names) -> set[str]:
    out = set()
    for n in names:
        for t in re.findall(r"[a-z0-9]+", (n or "").lower()):
            if t not in STOPWORDS and len(t) > 1: out.add(t)
    return out

@dataclass
class Finding:
    payout_ref: str
    bucket: str
    reasons: list = field(default_factory=list)
    lines: list = field(default_factory=list)
    bank: dict | None = None
    proposed_deposit: dict | None = None

class Matcher:
    def __init__(self, snap: dict):
        self.cutoff = d(snap["cutoff"]) if snap.get("cutoff") else None
        self.payouts = sorted(snap["payouts"], key=lambda p: (p["payout_date"], p["ref"]))
        self.ledger = snap["ledger"]            # receipt / reversal JEs from QBO
        self.bank = snap["bank_deposits"]        # Wells 3021 deposits
        self.aliases = {k.lower(): v for k, v in snap.get("aliases", {}).items()}  # approved payer->customer
        self.used_je: set[str] = set()
        self.used_bank: set[str] = set()

    # ---- bank ---------------------------------------------------------------
    def _bank_candidates(self, amount: Decimal, pdate: date):
        return [b for b in self.bank if b["id"] not in self.used_bank and D(b["amount"]) == amount
                and pdate <= d(b["date"]) <= pdate + timedelta(days=1)]

    def _group_ties(self, idx: int):
        """Neighbouring payouts whose nets only tie to one bank deposit as a group."""
        hits = []
        for size in (2, 3):
            for combo in itertools.combinations(range(max(0, idx - 3), min(len(self.payouts), idx + 4)), size):
                if idx not in combo: continue
                ps = [self.payouts[i] for i in combo]
                total = sum(D(p["net"]) for p in ps)
                last = max(d(p["payout_date"]) for p in ps)
                first = min(d(p["payout_date"]) for p in ps)
                for b in self.bank:
                    if D(b["amount"]) == total and first <= d(b["date"]) <= last + timedelta(days=1):
                        hits.append({"payouts": [p["ref"] for p in ps], "total": str(total), "bank": b})
        return hits

    # ---- ledger -------------------------------------------------------------
    def _name_ok(self, line, je) -> tuple[bool, str]:
        alias = self.aliases.get((line.get("payer") or "").lower()) or self.aliases.get((line.get("business") or "").lower())
        if alias and alias.lower() == je["customer"].lower(): return True, "approved alias"
        if tokens(line.get("payer"), line.get("business")) & tokens(je["customer"]): return True, "name match"
        norm = lambda v: re.sub(r"[^a-z0-9]", "", (v or "").lower())
        if norm(je["customer"]) and norm(je["customer"]) in (norm(line.get("payer")), norm(line.get("business"))): return True, "exact name"
        return False, f"payer '{line.get('business') or line.get('payer')}' vs customer '{je['customer']}'"

    def _receipt_candidates(self, line, pdate):
        amt = D(line["amount"])
        return [j for j in self.ledger if j["kind"] == "receipt" and j["je_id"] not in self.used_je
                and D(j["amount"]) == amt and pdate - timedelta(days=RECEIPT_LOOKBACK_DAYS) <= d(j["date"]) <= pdate]

    def _reversal_candidates(self, line):
        amt = abs(D(line["amount"]))
        return [j for j in self.ledger if j["kind"] == "reversal" and j["je_id"] not in self.used_je and D(j["amount"]) == amt]

    def match_line(self, line, pdate):
        amt = D(line["amount"])
        out = {"line": line, "status": None, "je": None, "notes": []}
        if line["type"] == "convenience_fee":
            out.update(status="excluded", notes=["client convenience fee - never reaches bank"]); return out
        if line["type"] in REDUCTION_TYPES:
            cands = [(j, self._name_ok(line, j)) for j in self._reversal_candidates(line)]
            named = [c for c in cands if c[1][0]]
            pick = named if named else cands
            if len(pick) == 1:
                j, (ok, why) = pick[0]
                self.used_je.add(j["je_id"])
                out.update(je=j, status="matched" if ok else "needs_approval",
                           notes=[f"reduction -> reversal {j['receipt_no']} ({why})"])
                if j.get("bank_account") != UNDEPOSITED:
                    out["status"] = "stop"; out["notes"].append(f"reversal JE bank line on '{j.get('bank_account')}', not Undeposited Funds")
            elif len(pick) > 1:
                out.update(status="stop", notes=[f"ambiguous reversal: {[c[0]['receipt_no'] for c in pick]}"])
            else:
                orig = [j for j in self.ledger if j["kind"] == "receipt" and D(j["amount"]) == abs(amt) and self._name_ok(line, j)[0]]
                out.update(status="stop", notes=["no source JE for reduction (no -R reversal)"
                           + (f"; original receipt {orig[0]['receipt_no']} still unapplied?" if orig else "")])
            return out
        # sale
        cands = [(j, self._name_ok(line, j)) for j in self._receipt_candidates(line, pdate)]
        named = [c for c in cands if c[1][0]]
        if len(named) > 1 and line.get("txn_date"):
            near = [c for c in named if abs((d(c[0]["date"]) - d(line["txn_date"])).days) <= 3]
            if len(near) == 1: named = near; out["notes"].append(f"{len(cands)} same-amount receipts; picked the one dated within 3 days of the payment")
        pick = named if len(named) == 1 else (cands if len(cands) == 1 else None)
        if pick:
            j, (ok, why) = pick[0]
            self.used_je.add(j["je_id"])
            out.update(je=j, status="matched" if ok else "needs_approval", notes=out["notes"] + [f"receipt {j['receipt_no']} ({why})"])
            if j.get("bank_account") != UNDEPOSITED:
                out["status"] = "stop"; out["notes"].append(f"receipt JE bank line on '{j.get('bank_account')}' (legacy Trust?), must be Undeposited Funds")
        elif cands:
            out.update(status="stop", notes=[f"ambiguous: {len(cands)} receipts at {amt}: {[c[0]['receipt_no'] for c in cands]}"])
        else:
            out.update(status="stop", notes=["no EZLynx receipt JE for this payer+amount"])
        if amt < SMALL_LINE and out["status"] != "matched":
            out["notes"].append("small line - confirm it is a real payment, not a fee")
        return out

    def _split_hint(self, p, q):
        """Which combination of the pair's lines adds up to this payout's net (a hint for a person, never applied)."""
        allp = [l for l in p["lines"] + q["lines"] if l["type"] != "convenience_fee"]
        refs = {}
        for l in allp: refs.setdefault(l.get("psp_ref"), []).append(l)
        zero = {r for r, ls in refs.items() if r and len(ls) > 1 and sum(D(x["amount"]) for x in ls) == 0}
        pool = [l for l in allp if l.get("psp_ref") not in zero]
        if len(pool) > 16: return ["too many lines to suggest a split"]
        net, hits = D(p["net"]), []
        for n in range(1, len(pool) + 1):
            for combo in itertools.combinations(range(len(pool)), n):
                if sum(D(pool[k]["amount"]) for k in combo) == net:
                    hits.append(combo)
                    if len(hits) > 3: break
        lab = lambda l: f"{l.get('business') or l.get('payer')} {D(l['amount'])}"
        if not hits: return ["no line split reproduces this net"]
        zn = f"; zero-sum wash pairs left out (placement doesn't change cash): {sorted(zero)}" if zero else ""
        if len(hits) > 1: return [f"{len(hits) if len(hits) <= 3 else '4+'} possible line splits reproduce net {net} - a person must pick{zn}"]
        return ["suggested split (cash actually netted here): " + ", ".join(lab(pool[k]) for k in hits[0]) + zn]

    def _cross_payout_notes(self, p, lines):
        notes = []
        for l in lines:
            ln = l["line"]
            if ln["type"] not in REDUCTION_TYPES: continue
            for q in self.payouts:
                for s in q["lines"]:
                    if s["type"] == "sale" and s.get("psp_ref") and s["psp_ref"] == ln.get("psp_ref"):
                        where = "same payout" if q is p else f"payout {q['ref']} ({q['payout_date']})"
                        if q is not p and l["status"] == "matched":
                            notes.append(f"info: {ln.get('business') or ln.get('payer')} {abs(D(ln['amount']))} reverses sale {s['psp_ref']} in {where}; reversal JE {l['je']['receipt_no']} found")
                            continue
                        notes.append(f"wash: {ln.get('business') or ln.get('payer')} {abs(D(ln['amount']))} reverses sale {s['psp_ref']} in {where} - both JEs still need handling")
        return notes

    def _already_grouped(self, f):
        """A receipt already grouped in a different deposit than the matched bank deposit = double count risk."""
        for l in f.lines:
            je = l.get("je")
            if je and je.get("deposited_in") and f.bank and ("QBO-DEP-" + str(je["deposited_in"])) != f.bank["id"]:
                f.reasons.append(f"{je['receipt_no']} already grouped in QBO deposit {je['deposited_in']} - would double count")
                l["status"] = "stop"

    # ---- payouts ------------------------------------------------------------
    def run(self):
        findings = []
        for i, p in enumerate(self.payouts):
            pdate, net = d(p["payout_date"]), D(p["net"])
            f = Finding(payout_ref=p["ref"], bucket="")
            if self.cutoff and pdate > self.cutoff:
                f.bucket = "past_cutoff"; f.reasons.append(f"payout {pdate} after cutoff {self.cutoff}")
                findings.append(f); continue
            lines = [self.match_line(l, pdate) for l in p["lines"]]
            f.lines = lines
            counted = [l for l in lines if l["status"] != "excluded"]
            line_sum = sum(D(l["line"]["amount"]) for l in counted)
            if line_sum != net:
                f.reasons.append(f"lines sum {line_sum} != payout net {net} (lines netted into another payout?)")
                for j in (i - 1, i + 1):
                    if 0 <= j < len(self.payouts):
                        q = self.payouts[j]
                        qsum = sum(D(l["amount"]) for l in q["lines"] if l["type"] != "convenience_fee")
                        if line_sum + qsum == net + D(q["net"]):
                            f.reasons.append(f"ties only as a group with {q['ref']} ({q['payout_date']}): lines {line_sum + qsum} = nets {net} + {D(q['net'])}")
                            f.reasons += self._split_hint(p, q)
            f.reasons += self._cross_payout_notes(p, counted)
            bank = self._bank_candidates(net, pdate)
            if len(bank) == 1:
                f.bank = bank[0]; self.used_bank.add(bank[0]["id"])
                if bank[0].get("feed_suggested_match"):
                    f.reasons.append(f"bank feed suggests {bank[0]['feed_suggested_match']} - never auto-accept")
            else:
                ties = self._group_ties(i)
                if ties: f.reasons.append(f"ties only as a group: {ties[0]['payouts']} = {ties[0]['total']} -> bank {ties[0]['bank']['date']}")
                elif len(bank) > 1: f.reasons.append(f"ambiguous: {len(bank)} Wells deposits of {net}")
                else: f.reasons.append(f"no Wells 3021 deposit of {net} on {pdate} or {pdate + timedelta(days=1)}")
            self._already_grouped(f)
            stops = [l for l in counted if l["status"] == "stop"]
            approvals = [l for l in counted if l["status"] == "needs_approval"]
            if stops or f.bank is None or line_sum != net:
                f.bucket = "unmatched"
                f.reasons += [f"{l['line'].get('business') or l['line'].get('payer')} {l['line']['amount']}: {'; '.join(l['notes'])}" for l in stops]
            elif approvals or any(r.startswith(("wash", "bank feed")) for r in f.reasons):
                f.bucket = "waiting_approval"
                f.reasons += [f"{l['line'].get('business') or l['line'].get('payer')} {l['line']['amount']}: {'; '.join(l['notes'])}" for l in approvals]
            else:
                f.bucket = "ready"
            if f.bucket in ("ready", "waiting_approval"):
                sel = [{"je_id": l["je"]["je_id"], "receipt_no": l["je"]["receipt_no"], "amount": str(D(l["line"]["amount"]))} for l in counted]
                tot = sum(D(s["amount"]) for s in sel)
                f.proposed_deposit = {"account": TRUST_3021, "date": f.bank["date"], "amount": str(net),
                                      "from": UNDEPOSITED, "selected": sel, "ties": tot == net,
                                      "memo": f"Applied Pay payout {p['ref']} ({p['payout_date']})", "action": "PROPOSE ONLY - not posted"}
            findings.append(f)
        return findings

def report(findings) -> str:
    order = [("ready", "Matched + ready (proposed deposits, not posted)"), ("waiting_approval", "Matched - waiting approval"),
             ("unmatched", "Unmatched (stop - reason)"), ("past_cutoff", "Past cutoff")]
    out = []
    for key, title in order:
        fs = [f for f in findings if f.bucket == key]
        out.append(f"## {title} ({len(fs)})")
        for f in fs:
            pd_ = f.proposed_deposit
            head = f"- {f.payout_ref}"
            if pd_: head += f": deposit {pd_['date']} ${pd_['amount']} = " + " + ".join(f"{s['receipt_no']} {s['amount']}" for s in pd_["selected"])
            out.append(head)
            for r in f.reasons: out.append(f"    - {r}")
    return "\n".join(out)

if __name__ == "__main__":
    snap = json.load(open(sys.argv[1]))
    fs = Matcher(snap).run()
    print(report(fs))
    if len(sys.argv) > 2:
        json.dump([asdict(f) for f in fs], open(sys.argv[2], "w"), indent=1, default=str)
