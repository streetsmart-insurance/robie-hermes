"""Offline report renderer for already-collected read-only source snapshots.

This CLI does not authenticate or fetch sources and never writes a source system.
Its stdout is a candidate report, not a verified 5:30 AM briefing.
"""
import argparse
import json
from .daily_accounting_checks import build_daily_report
from .daily_accounting_inputs import load_ascend_export, load_applied_batches, load_ezlynx_tasks


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ascend', help='Exhaustive Ascend collector JSON export')
    parser.add_argument('--applied-pay', help='Complete Applied Pay batch collector JSON export')
    parser.add_argument('--ezlynx', help='Complete Accounting Team task collector JSON export')
    args = parser.parse_args(argv)
    snapshots = {}
    for path, reader in ((args.ascend, load_ascend_export),
                         (args.applied_pay, load_applied_batches), (args.ezlynx, load_ezlynx_tasks)):
        if path:
            snapshot = reader(path)
            snapshots[snapshot.source] = snapshot
    print(json.dumps(build_daily_report(snapshots), indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
