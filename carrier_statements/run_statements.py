"""Read supplied source manifests; no network, path guessing, or external writes."""
import argparse
import json
from registry import parse_file
from review_contract import validate_source, statement_holds
from report import summarize

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--manifest',required=True)
    ap.add_argument('--agency-id',required=True)
    ap.add_argument('--month',required=True)
    args=ap.parse_args()
    seen=set();failed=False
    for source in json.load(open(args.manifest))['sources']:
        problems=validate_source(source,agency_id=args.agency_id,month=args.month)
        key=(source.get('agency_id'),source.get('carrier_id'),source.get('account_no'),source.get('printed_period'),source.get('sha256'))
        if key in seen:
            print(json.dumps({'source_id':source.get('source_id'),'status':'DUPLICATE_SOURCE_SKIPPED'}));continue
        seen.add(key)
        if problems:
            failed=True;print(json.dumps({'source_id':source.get('source_id'),'status':'HELD','holds':problems}));continue
        try:
            stmt=parse_file(source['parser'],source['path'])
            holds=statement_holds(stmt,source,agency_id=args.agency_id,month=args.month)
            print(json.dumps({'source_id':source['source_id'],'sha256':source['sha256'],'status':'HELD' if holds else 'REVIEW_ONLY','holds':holds}))
            print(summarize(stmt));failed=failed or bool(holds)
        except Exception as exc:
            failed=True;print(json.dumps({'source_id':source['source_id'],'status':'PARSE_FAILED','error_type':type(exc).__name__}))
    return 1 if failed else 0

if __name__=='__main__':raise SystemExit(main())
