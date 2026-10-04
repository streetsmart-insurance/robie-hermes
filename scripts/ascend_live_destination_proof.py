#!/usr/bin/env python3
"""Prove the Ascend live destination ports with one synthetic event.

Default is a dry run: prints the event and the components it would write.
No login, no HTTP.

--ezlynx (Test host, a person watching, Buster Brown only):
  one synthetic cancellation event -> note + task on Buster Brown's
  "Tasks by Robie" discussion through ReliableDelivery + the EZLynx port,
  then a second pass that must find both by source marker and send nothing.

--qbo-sandbox: one synthetic commission payout -> one Deposit in the QBO
  *sandbox* company with the mapped accounts/payee, read back. Refused when
  the QuickBooks config points at Production.

Exit 0 only when every requested pass ends in delivered_readback.

  ROBIE_ENV=TEST ROBIE_EZLYNX_DISCUSSION_API=live \\
  ROBIE_EZLYNX_WRITE_APPLICANT_IDS=26356199 \\
  ROBIE_EZLYNX_TASK_API_ACT_AS_USERNAME=<agency login> \\
  PYTHONPATH=. python3 scripts/ascend_live_destination_proof.py --ezlynx --ledger /tmp/ascend-proof.db
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone

from robie_job_engine.ascend_delivery_state import REQUIRED, DurableLedger, ReliableDelivery
from robie_job_engine.ezlynx_user_ids import ezlynx_user_id_for

BUSTER = "26356199"


class CountingPort:
    """Wraps a port and counts send_component calls."""

    def __init__(self, port):
        self.port, self.sends = port, []

    def find_for_event(self, event):
        return self.port.find_for_event(event)

    def send_component(self, event, key, component):
        self.sends.append(component)
        return self.port.send_component(event, key, component)

    def readback(self, ids):
        return self.port.readback(ids)


def ezlynx_event(stamp: str) -> dict:
    return {
        "key": f"robie-proof-{stamp}", "kind": "cancellation",
        "applicant_id": BUSTER, "assignee": "SSRobie",
        "assignee_user_id": ezlynx_user_id_for("SSRobie"),
        "title": f"[ROBIE TEST] Ascend destination proof {stamp}",
        "note_text": "[ROBIE TEST] Synthetic Ascend cancellation note. No real policy. Safe to ignore.",
        "task_text": "[ROBIE TEST] Synthetic Ascend task. Safe to close.",
        "due_date": (date.today() + timedelta(days=1)).isoformat(),
    }


def qbo_event(stamp: str, realm_id: str) -> dict:
    from robie_job_engine.ascend_destination_mapping import map_payout

    event = {"key": f"robie-proof-payout-{stamp}", "kind": "commission_payout",
             "source_id": f"proof-{stamp}", "amount_cents": 100, "currency": "USD",
             "txn_date": date.today().isoformat()}
    binding, why = map_payout(event, realm_id)
    if why:
        raise SystemExit(f"QBO mapping incomplete: {why}")
    event.update(binding)
    return event


def run(event: dict, port, ledger_path: str) -> dict:
    counting = CountingPort(port)
    first = ReliableDelivery(DurableLedger(ledger_path), counting).process(event)
    sent_first = list(counting.sends)
    # Fresh ledger: the second pass must find the writes by marker alone.
    second = ReliableDelivery(DurableLedger(ledger_path + ".second"), counting).process(event)
    ledger = DurableLedger(ledger_path)
    ids = ledger[event["key"]].destination_ids
    ledger.close()
    return {"key": event["key"], "first_pass": first, "sent_first_pass": sent_first,
            "second_pass_fresh_ledger": second, "sent_second_pass": counting.sends[len(sent_first):],
            "destination_ids": ids,
            "ok": first == second == "delivered_readback" and len(counting.sends) == len(sent_first)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ezlynx", action="store_true")
    ap.add_argument("--qbo-sandbox", action="store_true")
    ap.add_argument("--ledger", default="/tmp/ascend-destination-proof.db")
    args = ap.parse_args(argv)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if not (args.ezlynx or args.qbo_sandbox):
        event = ezlynx_event(stamp)
        print(json.dumps({"dry_run": True, "event": event,
                          "components": list(REQUIRED[event["kind"]])}, indent=2))
        return 0
    results, ok = [], True
    if args.ezlynx:
        from robie_job_engine.ascend_destinations import EZLynxAscendDestination
        r = run(ezlynx_event(stamp), EZLynxAscendDestination(), args.ledger)
        results.append({"ezlynx": r}); ok = ok and r["ok"]
    if args.qbo_sandbox:
        from robie_job_engine.ascend_destinations import QBODepositDestination
        from robie_job_engine.quickbooks_api import QuickBooksApiClient
        qb = QuickBooksApiClient()
        if not qb.is_configured or qb.config.is_production:
            print("QBO sandbox config not present (or points at Production); not writing.")
            return 2
        r = run(qbo_event(stamp, qb.config.realm_id), QBODepositDestination(qb), args.ledger + ".qbo")
        results.append({"qbo_sandbox": r}); ok = ok and r["ok"]
    print(json.dumps(results, indent=2, default=str))
    print("PROOF PASSED" if ok else "PROOF FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
