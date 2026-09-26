"""Applied Pay payout matcher - phase 1, READ-ONLY.

Inputs are plain JSON snapshots (pulled elsewhere); this module never talks to
EZLynx, QBO or the bank and never posts anything. It proposes Bank Deposits and
reports every line into one of six buckets:
  ready            - cleared bank record and all lines matched; unposted proposal built
  already_posted   - existing QBO deposit groups precisely the matched JEs
  already_posted_needs_review - exact QBO group mapped by amount/customer,
                       but PSP-bound EZLynx notes are missing
  scheduled_unlanded - matched settlement email, no verified cleared bank record yet
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
        self.bank = snap["bank_deposits"]        # existing QBO 3021 deposits (NOT bank-clearing proof)
        self.cleared_bank = snap.get("cleared_bank_deposits", [])  # independently verified bank-cleared records
        self.aliases = {k.lower(): v for k, v in snap.get("aliases", {}).items()}  # approved payer->customer
        self.fee_rules = snap.get("fee_rules", {})  # explicit review clues, never posting rules
        self.used_je: set[str] = set()
        self.used_bank: set[str] = set()

    # ---- bank ---------------------------------------------------------------
    def _bank_candidates(self, records, amount: Decimal, pdate: date):
        return [b for b in records if b["id"] not in self.used_bank and D(b["amount"]) == amount
                and pdate <= d(b["date"]) <= pdate + timedelta(days=1)]

    def _cleared_candidates(self, amount: Decimal, pdate: date, payout_ref: str):
        # Amount/date alone cannot bind a bank item to this transfer. A verifier
        # must provide an independent bank source and the exact payout reference.
        return [b for b in self._bank_candidates(self.cleared_bank, amount, pdate)
                if b.get("status") == "cleared" and b.get("account") == TRUST_3021
                and b.get("verified_payout_ref") == payout_ref
                and b.get("verification_source") == "bank_record"
                and b.get("bank_transaction_id")]

    def _scheduled_candidates(self, amount: Decimal, pdate: date):
        return [b for b in self._bank_candidates(self.cleared_bank, amount, pdate)
                if b.get("status") == "scheduled" and b.get("account") == TRUST_3021]


    @staticmethod
    def _group_ids(bank):
        return {str(g) for g in bank.get("groups", [])}


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

    def _fee_review(self, line):
        """Use source-bound EZLynx notes only as review clues, not posting rules."""
        amount = D(line["amount"])
        evidence = line.get("ezlynx_note_check") or {}
        checked = (evidence.get("status") == "checked" and
                   evidence.get("source") == "ezlynx" and
                   evidence.get("payment_ref") and
                   evidence.get("payment_ref") == line.get("psp_ref") and
                   evidence.get("note_ids") and
                   isinstance(evidence.get("notes"), list))
        if not checked:
            return "needs_approval", "EZLynx notes not checked and bound to this payment; fee/payable type unknown", None
        context = " ".join(str(n.get("text") or "") for n in evidence["notes"] if isinstance(n, dict))
        fee_terms = self.fee_rules.get("note_terms", ["MBR", "agency fee"])
        payable_terms = self.fee_rules.get("payable_terms", ["payable"])
        has_fee = any(re.search(r"\b" + re.escape(str(t)) + r"\b", context, re.I) for t in fee_terms if t)
        has_payable = any(re.search(r"\b" + re.escape(str(t)) + r"\b", context, re.I) for t in payable_terms if t)
        if has_fee and has_payable:
            return "stop", "EZLynx notes mention fee and payable; conflicting classification", None
        if has_payable:
            return "needs_approval", "EZLynx notes suggest payable, not a fee; review allocation", None
        components = line.get("fee_components") or []
        if components:
            if not has_fee:
                return "stop", "fee components supplied without an EZLynx fee note tied to this payment", None
            fees = sum(D(x["amount"]) for x in components)
            premium = D(line.get("premium_amount", amount - fees))
            if fees <= 0 or premium < 0 or premium + fees != amount:
                return "stop", "fee components do not tie to settled payment; check source", None
            labels = ", ".join(str(x.get("type", "unclassified")) for x in components)
            return "needs_approval", f"proposed premium {premium} + fee {fees} ({labels}); verify fee account and notes", premium
        if has_fee:
            return "needs_approval", "EZLynx note mentions fee; inspect premium/fee split and account", None
        if amount < SMALL_LINE:
            return "needs_approval", "small payment with no fee/payable note; review before classification", None
        return None

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
        # A structured fee split can identify a premium receipt but the fee's QBO
        # account is not inferred. A note term alone cannot set the split.
        fee = self._fee_review(line)
        if fee:
            status, note, premium = fee
            out.update(status=status, notes=[note])
            if status == "stop" or premium is None or premium == 0:
                return out
            premium_line = dict(line, amount=str(premium))
            cands = [(j, self._name_ok(premium_line, j)) for j in self._receipt_candidates(premium_line, pdate)]
            named = [c for c in cands if c[1][0]]
            if len(named) != 1:
                out.update(status="stop", notes=[note, "premium receipt missing or ambiguous"])
                return out
            j = named[0][0]
            self.used_je.add(j["je_id"])
            out["je"] = j
            out["notes"].append(f"premium receipt {j['receipt_no']} found; fee still requires accounting review")
            if j.get("bank_account") != UNDEPOSITED or j.get("deposited_in"):
                out["status"] = "stop"
                out["notes"].append("premium receipt unavailable in Undeposited Funds")
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
        """A JE in another QBO deposit is never available for a new proposal."""
        for l in f.lines:
            je = l.get("je")
            if not je or not je.get("deposited_in"):
                continue
            deposited = str(je["deposited_in"])
            linked = deposited if deposited.startswith("QBO-DEP-") else "QBO-DEP-" + deposited
            if not f.bank or linked != str(f.bank["id"]) or f.bank not in self.bank:
                f.reasons.append(f"{je['receipt_no']} already grouped in QBO deposit {deposited} - would double count")
                l["status"] = "stop"

    def _posted_without_note_check(self, posted, counted, pdate):
        """Find an existing QBO deposit's exact JE group, never approve a new posting.

        No fee/payable claim can be made without PSP-bound EZLynx notes. This
        path is only a provisional *already posted* classification, and refuses
        any ambiguous mapping, fee split, reduction, or previously used JE.
        """
        if len(posted) != 1 or not counted or any(
            l["line"]["type"] != "sale" or l["status"] != "needs_approval" or
            l.get("je") or l["line"].get("fee_components") or
            not any("EZLynx notes not checked and bound" in n for n in l["notes"])
            for l in counted
        ):
            return None
        bank = posted[0]
        groups = [str(g) for g in bank.get("groups", [])]
        if len(groups) != len(counted) or len(set(groups)) != len(groups):
            return None
        by_id = {str(j["je_id"]): j for j in self.ledger if str(j["je_id"]) in groups}
        if len(by_id) != len(groups):
            return None
        matches = []
        for l in counted:
            candidates = [j for j in by_id.values()
                if str(j["je_id"]) not in self.used_je
                and j["kind"] == "receipt"
                and j.get("bank_account") == UNDEPOSITED
                and str(j.get("deposited_in")) in (str(bank["id"]), str(bank["id"]).removeprefix("QBO-DEP-"))
                and D(j["amount"]) == D(l["line"]["amount"])
                and pdate - timedelta(days=RECEIPT_LOOKBACK_DAYS) <= d(j["date"]) <= pdate
                and self._name_ok(l["line"], j)[0]]
            if not candidates:
                return None
            matches.append(candidates)
        assignments = []
        for choice in itertools.product(*matches):
            if len({str(j["je_id"]) for j in choice}) == len(groups):
                assignments.append(choice)
                if len(assignments) > 1:
                    return None  # same-amount duplicates must never be guessed
        return (bank, assignments[0]) if len(assignments) == 1 else None

    # ---- payouts ------------------------------------------------------------
    def run(self):
        findings = []
        refs = [p["ref"] for p in self.payouts]
        if len(refs) != len(set(refs)):
            return [Finding(payout_ref=p["ref"], bucket="unmatched",
                            reasons=["duplicate payout transfer ID in input; stop all matching"])
                    for p in self.payouts]
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
            # QBO deposit is not evidence of bank clearing. An existing deposit must
            # contain precisely these JE IDs, never just the same date and dollars.
            posted = self._bank_candidates(self.bank, net, pdate)
            selected_ids = {str(l["je"]["je_id"]) for l in counted if l.get("je")}
            existing_exact = len(posted) == 1 and len(selected_ids) == len(counted) and self._group_ids(posted[0]) == selected_ids
            provisional_posted = None
            if not existing_exact and line_sum == net:
                provisional_posted = self._posted_without_note_check(posted, counted, pdate)
            if provisional_posted:
                bank, assigned = provisional_posted
                for line, je in zip(counted, assigned):
                    line["je"] = je
                    line["notes"].append(f"existing deposit groups receipt {je['receipt_no']} by amount and customer; PSP/EZLynx note binding unverified")
                    self.used_je.add(str(je["je_id"]))
                f.bank = bank
                self.used_bank.add(bank["id"])
                f.reasons.append("existing QBO deposit groups exactly the amount/customer-matched JEs; needs source-bound PSP and EZLynx note review, not bank-clearing proof")
            if existing_exact:
                f.bank = posted[0]
                self.used_bank.add(f.bank["id"])
                f.reasons.append("existing QBO deposit groups exactly these receipt/reversal JEs; already posted, not a new proposal")
            elif posted and not provisional_posted:
                f.reasons.append("existing QBO 3021 deposit has same date/amount but grouped JE IDs do not match exactly (or ambiguous deposit); stop")
            cleared = self._cleared_candidates(net, pdate, p["ref"])
            if not existing_exact and not posted and len(cleared) == 1:
                f.bank = cleared[0]
                self.used_bank.add(f.bank["id"])
            elif len(cleared) > 1:
                f.reasons.append(f"ambiguous: {len(cleared)} independently cleared 3021 bank deposits of {net}")
            if f.bank is None and not posted and not cleared:
                f.reasons.append("no independently verified cleared 3021 bank record tied to this payout reference; never infer landing from email")
                if self._scheduled_candidates(net, pdate):
                    f.reasons.append("matching bank item is only scheduled, not cleared")
            self._already_grouped(f)
            stops = [l for l in counted if l["status"] == "stop"]
            approvals = [l for l in counted if l["status"] == "needs_approval"]
            if stops or line_sum != net or (posted and not existing_exact and not provisional_posted) or len(cleared) > 1:
                f.bucket = "unmatched"
                f.reasons += [f"{l['line'].get('business') or l['line'].get('payer')} {l['line']['amount']}: {'; '.join(l['notes'])}" for l in stops]
            elif provisional_posted:
                f.bucket = "already_posted_needs_review"
                f.reasons += [f"{l['line'].get('business') or l['line'].get('payer')} {l['line']['amount']}: {'; '.join(l['notes'])}" for l in approvals]
            elif existing_exact:
                f.bucket = "already_posted" if not approvals and not any(r.startswith("wash") for r in f.reasons) else "waiting_approval"
                f.reasons += [f"{l['line'].get('business') or l['line'].get('payer')} {l['line']['amount']}: {'; '.join(l['notes'])}" for l in approvals]
            elif f.bank is None:
                f.bucket = "scheduled_unlanded" if not approvals and not any(r.startswith("wash") for r in f.reasons) and (p.get("status") == "scheduled" or self._scheduled_candidates(net, pdate)) else "unmatched"
                f.reasons += [f"{l['line'].get('business') or l['line'].get('payer')} {l['line']['amount']}: {'; '.join(l['notes'])}" for l in approvals]
            elif approvals or any(r.startswith("wash") for r in f.reasons):
                f.bucket = "waiting_approval"
                f.reasons += [f"{l['line'].get('business') or l['line'].get('payer')} {l['line']['amount']}: {'; '.join(l['notes'])}" for l in approvals]
            else:
                f.bucket = "ready"
            if f.bucket == "ready" and not existing_exact and f.bank is not None:
                sel = [{"je_id": l["je"]["je_id"], "receipt_no": l["je"]["receipt_no"], "amount": str(D(l["line"]["amount"]))} for l in counted]
                tot = sum(D(s["amount"]) for s in sel)
                f.proposed_deposit = {"account": TRUST_3021, "date": f.bank["date"], "amount": str(net),
                                      "from": UNDEPOSITED, "selected": sel, "ties": tot == net,
                                      "memo": f"Applied Pay payout {p['ref']} ({p['payout_date']})", "action": "PROPOSE ONLY - not posted"}
            findings.append(f)
        return findings

def report(findings) -> str:
    order = [("ready", "Matched + ready (proposed deposits, not posted)"), ("waiting_approval", "Matched - waiting approval"),
             ("already_posted", "Already posted in QBO (no new proposal)"),
             ("already_posted_needs_review", "Existing QBO deposit - needs source-bound review (no new proposal)"),
             ("scheduled_unlanded", "Scheduled / bank not verified cleared"),
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
