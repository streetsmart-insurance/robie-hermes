"""Read-only source collection contracts for a candidate daily accounting briefing.

Ascend uses the installed authenticated GET client. Portal and task surfaces
must be supplied by separately verified readers; an unverified UI dump is
not a complete source. No source-system write, scheduler or message send.
"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs
from typing import Any, Iterable

from .daily_accounting_checks import SourceSnapshot, build_daily_report
from .daily_accounting_inputs import ASCEND_COLLECTIONS, load_ascend_export, load_applied_batches, load_ezlynx_tasks
import json
import tempfile
from pathlib import Path


class CoverageError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _page_number(next_value: str, *, path: str, origin: str) -> int:
    if not isinstance(next_value, str) or not next_value:
        raise CoverageError('bad next link')
    url = urlparse(next_value)
    base = urlparse(origin)
    if url.scheme or url.netloc:
        if (url.scheme, url.netloc) != (base.scheme, base.netloc):
            raise CoverageError('next link outside source origin')
    if url.path != path:
        raise CoverageError('next link outside collection')
    query = parse_qs(url.query)
    if set(query) != {'page'} or len(query['page']) != 1:
        raise CoverageError('ambiguous next page')
    try:
        page = int(query['page'][0])
    except ValueError as exc:
        raise CoverageError('bad next page') from exc
    if page < 1:
        raise CoverageError('bad next page')
    return page


def _ascend_pages(client, name, *, max_pages=500):
    path = f'/v1/{name}'
    page = 1; seen_pages=set();seen_ids=set();records=[]
    origin = str(client.origin).rstrip('/')
    if urlparse(origin).scheme != 'https':
        raise CoverageError('Ascend HTTPS origin required')
    for _ in range(max_pages):
        if page in seen_pages:
            raise CoverageError('pagination loop')
        seen_pages.add(page)
        reply = client.get(path, {'page':page})
        if not isinstance(reply, dict) or not isinstance(reply.get('data'), list) or not isinstance(reply.get('meta'), dict):
            raise CoverageError('missing data or pagination metadata')
        data=reply['data'];meta=reply['meta']
        if not isinstance(meta.get('count'), int) or isinstance(meta['count'],bool) or meta['count'] != len(data):
            raise CoverageError('page count mismatch')
        for item in data:
            if not isinstance(item,dict) or not item.get('id') or item['id'] in seen_ids:
                raise CoverageError('invalid or duplicate record ID')
            seen_ids.add(item['id'])
        records.extend(data)
        if 'next' not in meta:
            raise CoverageError('missing explicit next marker')
        nxt=meta['next']
        if nxt is None:
            return records, len(seen_pages)
        next_page=_page_number(nxt,path=path,origin=origin)
        if next_page != page+1:
            raise CoverageError('non-sequential next page')
        page=next_page
    raise CoverageError('pagination safety cap')


def collect_ascend(client, *, max_pages=500):
    """GET all documented collections and refuse partial or uncertain results."""
    stamp=_now();collections={};pages={}
    try:
        for name in ASCEND_COLLECTIONS:
            rows,count=_ascend_pages(client,name,max_pages=max_pages)
            collections[name]={'records':rows,'exhausted':True}
            pages[name]=count
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'snapshot.json'
            path.write_text(json.dumps({'as_of':stamp,'collections':collections}))
            snap=load_ascend_export(path)
        return SourceSnapshot(snap.source,snap.items,snap.complete,snap.error,stamp,
                              'Ascend GET page + explicit meta.next; pages='+json.dumps(pages,sort_keys=True))
    except (CoverageError,KeyError,TypeError,ValueError,RuntimeError) as exc:
        return SourceSnapshot('Ascend',[],False,error=f'collection incomplete: {type(exc).__name__}',as_of=stamp)


def collect_applied_portal(pages: Iterable[dict], *, scope: str):
    """Validate a fully expanded portal export. Not itself a live portal login."""
    stamp=_now();batches=[]; seen=set();last=None
    try:
        if not scope.startswith('all batches '):
            raise CoverageError('unverified portal scope')
        pages=list(pages)
        if not pages or len(pages)>500:
            raise CoverageError('missing or excessive pages')
        for index,p in enumerate(pages):
            if not isinstance(p,dict) or not p.get('as_of') or not isinstance(p.get('batches'),list):
                raise CoverageError('invalid page')
            if p.get('row_count') != len(p['batches']):
                raise CoverageError('batch row count mismatch')
            if 'next' not in p:
                raise CoverageError('missing page boundary')
            if index < len(pages)-1 and not p['next']:
                raise CoverageError('early terminal page')
            if index == len(pages)-1 and p['next'] is not None:
                raise CoverageError('unfetched page')
            for b in p['batches']:
                if not isinstance(b,dict) or not b.get('ref') or b['ref'] in seen or not isinstance(b.get('lines'),list):
                    raise CoverageError('duplicate or invalid batch')
                seen.add(b['ref'])
                if not b.get('lines'):
                    raise CoverageError('unexpanded batch')
                batches.append(b)
            last=p['as_of']
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'portal.json'
            path.write_text(json.dumps({'complete':True,'scope':scope,'as_of':last,'batches':batches}))
            snap=load_applied_batches(path)
        return SourceSnapshot(snap.source,snap.items,snap.complete,snap.error,last,
                              f'Applied Pay portal {scope}; {len(pages)} pages, {len(batches)} batches (reader supplied)')
    except (CoverageError,TypeError,KeyError,ValueError) as exc:
        return SourceSnapshot('Applied Pay',[],False,error=f'portal coverage incomplete: {type(exc).__name__}',as_of=stamp)


def collect_ezlynx_tasks(tasks: Iterable[dict], *, as_of: str, scope: str, exhausted: bool):
    """Reject UI summaries without stable task IDs or verified exhaustion."""
    try:
        if not exhausted or scope!='all Accounting Team open tasks' or not as_of:
            raise CoverageError('task scope incomplete')
        tasks=list(tasks)
        ids=[t['id'] for t in tasks]
        if any(not i for i in ids) or len(set(ids))!=len(ids):
            raise CoverageError('missing or duplicate task ID')
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'tasks.json';path.write_text(json.dumps({'complete':True,'scope':scope,'as_of':as_of,'tasks':tasks}))
            snap=load_ezlynx_tasks(path)
        return SourceSnapshot(snap.source,snap.items,snap.complete,snap.error,as_of,
                              'EZLynx task IDs and exhaustion supplied by verified reader')
    except (CoverageError,KeyError,TypeError,ValueError) as exc:
        return SourceSnapshot('EZLynx Accounting Team',[],False,error=f'task coverage incomplete: {type(exc).__name__}',as_of=as_of)


def briefing_payload(snapshots, *, as_of: str, previous=None, grades=()):
    """Structured briefing input; caller owns scheduling and user-facing delivery."""
    report=build_daily_report(snapshots,previous,grades)
    report['as_of']=as_of
    report['status']='candidate - source gaps' if not report['briefing_ready'] else 'candidate - independent recheck required'
    return report
