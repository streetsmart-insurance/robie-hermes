#!/usr/bin/env python3
"""Curate the draft routing table into the shipped carrier table.

Usage: curate_carrier_routes.py <draft.json> <out.json>

Applies human review decisions (recorded 2026-09-27 by Ralph from the
2026-09-27 directory sweep) on top of the draft generator's output:

- CC lists are trimmed to policy-change addresses only. Underwriter,
  renewal, audit, download, and customer-service addresses found in the
  same record are NOT policy-change routes and are dropped.
- Records whose summary says "Policy Changes = Missing" ship with NO
  route even when other emails exist in the record (#148 PIE, #155
  Quantum, #186 Travelers/CNA, #188 Tuscano).
- Fax-alias addresses (*fax@*) are not emailable -> no route (#154).
- "MAIL ONLY DO NOT EMAIL" (#129) -> no route.
- Phone/fax-only rows -> type "phone" with a note (worker cannot email
  these; they surface in the manual-action queue).
- Portal-preferred rows ("do online", "done on website", "they don't like
  email") -> type "portal". #39 Cabrillo ("email is best") stays email.

The output file is what the worker ships at
robie_job_engine/data/carrier_policy_change_routes.json. Nicole's
directory fill-in: re-run extract -> draft -> curate, review the diff.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path


def R(label, rtype, email=None, cc=(), phone=None, url=None, notes=""):
    return {
        "label": label,
        "type": rtype,
        "email": email,
        "cc_emails": list(cc),
        "phone": phone,
        "url": url,
        "notes": notes,
    }


# record_id -> (route_status, [routes])
# route_status:
#   "ok"      — safe for the worker to EMAIL (verified policy-change address).
#   "manual"  — the directory lists a route but it needs an agent:
#               portal-only, phone-only, fax-only, or an ambiguous address
#               the worker must never auto-send to. Surfaces in the
#               manual-action queue, never emailed.
#   "missing" — no actionable policy-change route; Nicole's fill-in queue.
OVERRIDES = {
    # --- wrong-primary / over-merged CC lists --------------------------------
    "14": ("ok", [R("Policy Changes", "email",
                    email="requestchange@americanspecialty.com",
                    phone="(260) 755-7265",
                    notes="named person bhohlbein@americanspecialty.com also listed")]),
    "16": ("ok", [R("Policy Changes", "email",
                    email="personalumbrella@amqts.com",
                    phone="(800) 234-6977",
                    notes="endorsements@amqts.com is the RENEWAL policy-changes address")]),
    "39": ("ok", [R("Policy Changes/Endorsements", "email",
                    email="wecare@cabgen.com",
                    notes="directory: 'You can do online but email is best'")]),
    "71": ("ok", [R("Policy Changes — Personal", "email",
                    email="plunderwriting@fmiweb.com"),
                  R("Policy Changes — Commercial", "email",
                    email="CLUnderwriting@fmiweb.com")]),
    "72": ("ok", [R("Policy Changes — Commercial", "email",
                    email="jmoscato@ftpins.com"),
                  R("Policy Changes — Personal", "email",
                    email="nmay@ftpins.com")]),
    "108": ("ok", [R("Policy Changes", "email",
                     email="mcanning@jimcor.com",
                     cc=["TGiambrone@jimcor.com"])]),
    "110": ("manual", [R("Policy Changes", "portal",
                     email="pldocs@kingstoneic.com",
                     url="https://goo.gl/woUNvK",
                     notes="directory: 'Done Online via System'; email as fallback")]),
    "122": ("ok", [R("Policy Changes", "email",
                     email="brokerchanges@tuscano.com",
                     cc=["processing@tuscano.com"])]),
    "124": ("ok", [R("Policy Changes", "email",
                     email="BHerman@morstan.com",
                     cc=["agoldfarb@morstan.com", "kmarkoe@morstan.com"])]),
    "126": ("ok", [R("Policy Changes", "email",
                     email="nbicunderwriting@nbic.com")]),
    "127": ("manual", [R("Policy Changes", "email",
                     email="caipcsr@nationalcontinental.com",
                     phone="8009372247",
                     notes="directory: address in testing phase per National — "
                           "agent verifies before use; worker never auto-sends")]),
    "131": ("ok", [R("Policy Change — Personal", "email",
                     email="erequest@nationwide.com",
                     phone="(877) 669-6877"),
                  R("Policy Change — Commercial", "email",
                    email="CLAmendment@nationwide.com")]),
    "143": ("ok", [R("Policy Change", "email",
                     email="crystal@paramounttx.com",
                     cc=["endorsement@paramounttx.com"],
                     phone="(866) 514-2200 ext. 540")]),
    "145": ("ok", [R("Endorsements", "email",
                     email="cnucera@patnat.com",
                     phone="(609) 291-1575",
                     notes="'Policy Changes' row empty; Endorsements row used")]),
    "147": ("manual", [R("Policy Changes", "phone",
                     phone="(510) 903-3313",
                     notes="phone-only via Rosemary Tolmer, Underwriter, Endorsements")]),
    "150": ("ok", [R("Policy Changes", "email",
                     email="policyservices@primeis.com",
                     phone="(877) 243-8181")]),
    "152": ("ok", [R("Policy Changes", "email",
                     email="commercialauto@email.progressive.com",
                     cc=["Upload@progressiveagent.com"])]),
    "157": ("ok", [R("Policy Changes", "email",
                     email="RPS.SanDiego-2.CommEnd@rpsins.com",
                     notes="endorsements desk; named UW addresses not used")]),
    "159": ("ok", [R("Policy Changes", "email",
                     email="Gabe.McGavock@rlig.com")]),
    "164": ("ok", [R("Policy Changes", "email",
                     email="Endorsements@sisinsure.com",
                     notes="'Policy Changes' row empty; Endorsements row used")]),
    "170": ("manual", [R("Policy Changes", "portal",
                     notes="directory: 'Agents do their own endorsements in the portal'")]),
    "172": ("ok", [R("Policy Changes", "email",
                     email="ins@stillwater.com")]),
    "174": ("ok", [R("Policy Changes", "email",
                     email="Endorsements@gotapco.com")]),
    "178": ("ok", [R("Policy Changes", "email",
                     email="agency.service@thehartford.com")]),
    "179": ("ok", [R("Policy Change — Commercial", "email",
                     email="MSACLProcessing@msagroup.com")]),
    "185": ("ok", [R("Policy Change — Commercial", "email",
                     email="scenter@travelers.com",
                     phone="(800) 252-2268"),
                  R("Policy Change — Personal", "email",
                    email="piservice@travelers.com",
                    phone="(800) 842-8574",
                    notes="directory: 'DO it online' for personal; email as fallback")]),
    "187": ("ok", [R("Policy Changes", "email",
                     email="endorsements@trinityunderwriters.net")]),
    "200": ("ok", [R("Policy Change — Commercial", "email",
                     email="cl@uticafirst.com",
                     phone="(800) 456-4556"),
                  R("Policy Change — Personal", "email",
                    email="PL@uticafirst.com")]),
    # --- portal/phone/fax-only or ambiguous: manual queue, never emailed ----
    "8": ("manual", [R("POLICY CHANGES", "portal",
                   notes="directory prefers portal; email listed only if present")]),
    "61": ("manual", [R("Endorsements/Policy Changes", "portal",
                    email="encuwnortheast@encompassins.com",
                    notes="directory prefers portal; worker never auto-sends the email")]),
    "68": ("manual", [R("Policy Changes/Endorsement", "portal",
                    email="policyupdate@foremost.com",
                    phone="(800) 527-3905",
                    notes="directory prefers portal; worker never auto-sends the email")]),
    "90": ("manual", [R("Routes: Policy Changes = Portal", "portal",
                    url="https://www.contacthiscox.com",
                    notes="directory prefers portal; email listed only if present")]),
    "102": ("manual", [R("Routes: Policy Changes = Ambiguous", "fax",
                     phone="(201) 368-8649",
                     notes="Fax Only — not a callable phone route; agent faxes")]),
    # --- records with NO emailable route --------------------------------------
    "148": ("missing", []),  # summary: Policy Changes = Missing (UW/renewal addrs only)
    "154": ("missing", []),  # policy-change addresses are fax aliases
    "155": ("missing", []),  # summary: Policy Changes = Missing (UW addr only)
    "186": ("missing", []),  # summary: Policy Changes = Missing (UW addrs only)
    "188": ("missing", []),  # summary: Policy Changes = Missing (UW addr only)
}


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: {argv[0]} <draft.json> <out.json>", file=sys.stderr)
        return 2
    draft = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    carriers = []
    for c in draft["carriers"]:
        rid = c["record_id"]
        entry = {
            "record_id": rid,
            "name": c["name"],
            "route_status": c["route_status"],
            "routes": copy.deepcopy(c["routes"]),
        }
        if rid in OVERRIDES:
            status, routes = OVERRIDES[rid]
            entry["route_status"] = status
            entry["routes"] = routes
        # Normalize: drop empty CCs, lowercase-compare dedupe already done.
        for r in entry["routes"]:
            r["cc_emails"] = [e for e in r["cc_emails"] if e]
        carriers.append(entry)
    ok = sum(1 for c in carriers if c["route_status"] == "ok")
    manual = sum(1 for c in carriers if c["route_status"] == "manual")
    missing = sum(1 for c in carriers if c["route_status"] == "missing")
    out = {
        "generated_from": "carrier-directory-full-sweep-20260927.md",
        "curation": "human-reviewed 2026-09-27; see tools/curate_carrier_routes.py OVERRIDES",
        "route_status_meanings": {
            "ok": "safe for the worker to email (verified policy-change address)",
            "manual": "directory lists a route that needs an agent "
                      "(portal/phone/fax-only or ambiguous); never emailed",
            "missing": "no actionable policy-change route; Nicole's fill-in queue",
        },
        "reconciliation": (
            "The 2026-09-27 directory sweep counted 103 present / 103 missing "
            "under a loose bar (any route-keyword row with an email, phone, "
            "URL, or annotation, including heuristic-derived rows). This table "
            "applies the worker's stricter bar: only routes an agent can act "
            "on (email, portal, phone, fax with real contact info). "
            "actionable = ok + manual; the rest ship as missing. Records the "
            "sweep counted as present but which carry no actionable contact "
            "are listed in loose_bar_present_but_unactionable."
        ),
        "loose_bar_present_but_unactionable": [
            {"record_id": "41",
             "reason": "sweep: ambiguous ('Put UW email'); no contact info"},
            {"record_id": "91",
             "reason": "sweep: Ambiguous (notes reference endorsements but "
                       "truncated, no address)"},
            {"record_id": "195",
             "reason": "sweep: present via video link only; no contact info"},
        ],
        "refresh": ("Re-run tools/extract_carrier_policy_change_routes.py then "
                    "tools/draft_carrier_routes.py then this script when Nicole's "
                    "directory fill-in adds policy-change contacts; review the diff."),
        "carriers": carriers,
    }
    Path(argv[2]).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"{len(carriers)} carriers: {ok} ok, {manual} manual, "
          f"{missing} missing -> {argv[2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
