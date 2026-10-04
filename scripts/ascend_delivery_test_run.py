#!/usr/bin/env python3
"""Run the Ascend Test delivery once. Dry run unless --live.

Reads Ascend (sandbox on ROBIE_ENV=TEST), maps every event, and with
--live sends only events mapped to an allowed applicant (Buster Brown by
default) through the EZLynx port; commission payouts only with
--include-payouts and a QBO sandbox config. Prints reason counts and the
COMPLETE list (delivered_readback only).

  ROBIE_ENV=TEST PYTHONPATH=. python3 scripts/ascend_delivery_test_run.py --ledger /tmp/ascend-test.db
"""

from __future__ import annotations

import argparse
import json
import sys


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--include-payouts", action="store_true")
    ap.add_argument("--applicant", action="append", default=None)
    args = ap.parse_args(argv)

    from robie_job_engine.ascend_sync import AscendEZLynxSyncManager, AscendApiClient, AscendSyncStore
    from robie_job_engine.ezlynx_api import EzlynxApiClient, load_ezlynx_api_config
    from robie_job_engine.ezlynx_api_only_writes import LIVE_DISCUSSION_API, discussion_api_target

    live_ez = discussion_api_target() == LIVE_DISCUSSION_API
    policy_client = EzlynxApiClient(load_ezlynx_api_config(environment="PRODUCTION" if live_ez else None))
    realm_id, destination, qb = None, None, None
    if args.include_payouts:
        from robie_job_engine.quickbooks_api import QuickBooksApiClient
        qb = QuickBooksApiClient()
        if qb.is_configured and not qb.config.is_production:
            realm_id = qb.config.realm_id
    if args.live:
        from robie_job_engine.ascend_destinations import (
            CompositeDestination, EZLynxAscendDestination, QBODepositDestination)
        destination = CompositeDestination(
            ezlynx=EZLynxAscendDestination(),
            qbo=QBODepositDestination(qb) if realm_id else None)
    manager = AscendEZLynxSyncManager(api_client=AscendApiClient(),
                                      store=AscendSyncStore(args.ledger + ".legacy-unused"))
    summary = manager.deliver_test_once(
        ledger_path=args.ledger, destination=destination, ezlynx_client=policy_client,
        realm_id=realm_id, allow_applicants=tuple(args.applicant or ["26356199"]),
        include_payouts=bool(realm_id))
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
