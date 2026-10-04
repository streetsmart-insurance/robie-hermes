"""Fail-closed Ascend delivery candidate. No live destination adapters.

Legacy source_seen/staged rows are not delivery receipts. Attempts without a
verified receipt are recovery-only: do not repeat a possibly landed write.
"""
from dataclasses import dataclass, field, asdict
import sqlite3, json
from collections.abc import MutableMapping
from typing import Protocol

@dataclass
class Delivery:
    source_seen: bool = True
    matched: bool = False
    staged: bool = False
    delivered: bool = False
    attempted: bool = False
    attempted_components: list = field(default_factory=list)
    destination_ids: dict = field(default_factory=dict)
    reason: str = 'source_seen'

class DurableLedger(MutableMapping):
    """Separate candidate DB. Transactions land before any send begins.

    Single worker only. This is not the legacy Ascend ledger and does not import
    it. Multiworker locks/leases remain a deployment gate.
    """
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS candidate_delivery (key TEXT PRIMARY KEY, state TEXT NOT NULL)")
        self.db.commit()
        self.cache = {}
    def __getitem__(self, key):
        if key in self.cache: return self.cache[key]
        row = self.db.execute("SELECT state FROM candidate_delivery WHERE key=?", (key,)).fetchone()
        if not row: raise KeyError(key)
        self.cache[key] = Delivery(**json.loads(row[0]))
        return self.cache[key]
    def __setitem__(self, key, state):
        self.cache[key] = state
        self.db.execute("INSERT OR REPLACE INTO candidate_delivery VALUES (?,?)", (key, json.dumps(asdict(state))))
        self.db.commit()
    def __delitem__(self, key):
        self.db.execute("DELETE FROM candidate_delivery WHERE key=?", (key,)); self.db.commit()
        self.cache.pop(key, None)
    def __iter__(self):
        return iter([r[0] for r in self.db.execute("SELECT key FROM candidate_delivery")])
    def __len__(self):
        return self.db.execute("SELECT count(*) FROM candidate_delivery").fetchone()[0]
    def close(self): self.db.close()

class Destination(Protocol):
    def find_by_key(self, key: str) -> dict: ...
    def send_component(self, event: dict, key: str, component: str) -> dict: ...
    def readback(self, ids: dict) -> dict: ...

REQUIRED = {'cancellation': ('note_id', 'task_id'),
            'agreement_signed': ('note_id', 'task_id'),
            'accounting_issue': ('task_id',),
            'commission_payout': ('deposit_id',)}

PAYOUT_BINDING = ('realm_id', 'account_id', 'income_account_id', 'payee_type', 'payee_id', 'amount_cents', 'currency')

def valid_binding(event):
    if event['kind'] == 'commission_payout':
        return all(event.get(k) is not None and event.get(k) != '' for k in PAYOUT_BINDING)
    return bool(event.get('applicant_id'))

def verified_components(event, receipt, port):
    if not valid_binding(event): return {}
    ids = receipt.get('ids', {})
    needed = REQUIRED.get(event['kind'], ())
    read = port.readback({k:v for k,v in ids.items() if k in needed})
    good = {}
    for k in needed:
        if not ids.get(k): continue
        obj = read.get(k) or {}
        if str(obj.get('id')) != str(ids[k]) or obj.get('source_key') != event['key']: continue
        binding = PAYOUT_BINDING if event['kind']=='commission_payout' else ('applicant_id',)
        if not all(obj.get(f)==event.get(f) for f in binding): continue
        good[k] = str(ids[k])
    return good

class ReliableDelivery:
    def __init__(self, ledger, destination=None):
        self.ledger = ledger
        self.destination = destination
    def process(self, event):
        try:
            return self._process(event)
        finally:
            key=event['key']
            if key in self.ledger: self.ledger[key]=self.ledger[key]
    def _process(self, event):
        key=event['key'];state=self.ledger.setdefault(key,Delivery());kind=event['kind']
        if kind=='supplier_payout': state.reason='supplier_accounting_disabled';return state.reason
        if kind not in REQUIRED: state.reason='unsupported_type_status';return state.reason
        if kind in ('cancellation','agreement_signed') and not event.get('applicant_id'):
            state.reason='unmatched_retryable';return state.reason
        state.matched=True;state.staged=True
        if self.destination is None:
            state.reason='staged_no_live_destination';return state.reason
        # No attribute on a port can waive durable storage.
        if not isinstance(self.ledger,DurableLedger):
            state.reason='durable_storage_required';return state.reason
        if not valid_binding(event):
            state.reason='destination_binding_required';return state.reason
        port=self.destination;needed=REQUIRED[kind]
        try:
            finder=getattr(port,'find_for_event',None)
            existing=(finder(event) if finder else port.find_by_key(key)) or {}
            good=verified_components(event,existing,port)
            state.destination_ids=good
            if len(good)==len(needed):
                state.delivered=True;state.reason='delivered_readback';return state.reason
            state.delivered=False
            # Only an authoritative, complete query scoped to source key and
            # destination binding can certify a component absent. A found note
            # does not establish that a missing task never landed.
            absent=existing.get('authoritative_absent',[])
            bound=existing.get('binding',{})
            fields=PAYOUT_BINDING if kind=='commission_payout' else ('applicant_id',)
            query_bound=(existing.get('source_key')==key and all(bound.get(f)==event.get(f) for f in fields))
            for component in needed:
                if component in good: continue
                if component in (existing.get('ids') or {}):
                    state.reason='existing_destination_unverified';return state.reason
                if (event.get('legacy_uncertain') or (state.attempted and not state.attempted_components)):
                    state.reason='attempt_uncertain_recovery_only';return state.reason
                if component in state.attempted_components:
                    state.reason='attempt_uncertain_recovery_only';return state.reason
                if not query_bound or component not in absent:
                    state.reason='component_absence_unverified';return state.reason
                state.attempted_components.append(component);state.attempted=True
                self.ledger[key]=state # commit before each component send
                receipt=port.send_component(event,key,component) or {}
                if receipt.get('not_sent') is True and not receipt.get('ids'):
                    # The port proved nothing left (refused before any
                    # request). Not an uncertain attempt: retry next run.
                    state.attempted_components.remove(component)
                    state.attempted=bool(state.attempted_components)
                    self.ledger[key]=state
                    state.reason='not_sent_retryable';return state.reason
                current=verified_components(event,receipt,port)
                if component not in current:
                    state.reason='destination_unverified';return state.reason
                good.update(current);state.destination_ids=dict(good)
                self.ledger[key]=state
            state.delivered=len(good)==len(needed)
            state.reason='delivered_readback' if state.delivered else 'partial_delivery'
            return state.reason
        except Exception as exc:
            state.reason='destination_error_'+type(exc).__name__;return state.reason


_CONTINUATION_KEYS = ('next', 'next_cursor', 'has_more', 'next_url')


def paginate(get_page, path, page_size=50, max_pages=500):
    """Read every page; reject loops, bad shape and cross-origin links.

    Two contracts are followed, nothing else:

    - Page number: ``meta.next`` is the next page number and is null on the
      last page. This is the contract proven live on Ascend ``/users``
      (Jake Ferrara on page 2 of 39), sent back as ``?page=N``.
    - Cursor: ``next_cursor`` with ``has_more``, sent back as
      ``starting_after``.

    A full page with no recognized continuation field raises instead of
    being read as the last page, and a record id seen on two pages raises.
    get_page(path, query) is a read-only callable. Never follows a URL.
    """
    result, seen_cursors, seen_ids = [], set(), set()
    cursor, page_no = None, None
    for _ in range(max_pages):
        query = {'page_size': page_size}
        if cursor is not None: query['starting_after'] = cursor
        if page_no is not None: query['page'] = page_no
        page = get_page(path, query)
        data = page.get('data')
        if not isinstance(data, list): raise ValueError('invalid_page_data')
        for row in data:
            rid = row.get('id') if isinstance(row, dict) else None
            if rid is not None:
                if rid in seen_ids: raise ValueError('duplicate_record_across_pages')
                seen_ids.add(rid)
        result.extend(data)
        meta = page.get('pagination') or page.get('meta') or {}
        if page.get('next') or meta.get('next_url'):
            raise ValueError('url_pagination_requires_vendor_binding')
        # Page-number contract (meta.next).
        if 'next' in meta and 'next_cursor' not in meta:
            nxt = meta.get('next')
            if nxt in (None, '', False):
                return result
            try:
                nxt_no = int(nxt)
            except (TypeError, ValueError):
                raise ValueError('url_pagination_requires_vendor_binding')
            if nxt_no <= (page_no or 1) or nxt_no in seen_cursors:
                raise ValueError('unsafe_or_repeated_cursor')
            seen_cursors.add(nxt_no); page_no = nxt_no
            continue
        nxt = meta.get('next_cursor') or page.get('next_cursor')
        more = meta.get('has_more', page.get('has_more', False))
        if not nxt:
            if more: raise ValueError('missing_next_cursor')
            known = any(k in meta or k in page for k in _CONTINUATION_KEYS)
            if len(data) >= page_size and not known:
                raise ValueError('pagination_contract_unverified_full_page')
            return result
        if nxt in seen_cursors or not isinstance(nxt, str) or '://' in nxt:
            raise ValueError('unsafe_or_repeated_cursor')
        seen_cursors.add(nxt); cursor = nxt
    raise ValueError('page_limit_exceeded')

def _due(days=1):
    from datetime import date, timedelta
    return (date.today() + timedelta(days=days)).isoformat()


def _program_policy(api, program_id):
    """(policy number, insured name) for a program. Read-only."""
    if not program_id:
        return None, None
    prog = api.fetch_program(program_id) or {}
    bills = prog.get('billables') or api.fetch_program_billables(program_id) or []
    first = bills[0] if bills else {}
    insured = prog.get('insured') or {}
    return (first.get('policy_number') or first.get('billable_identifier'),
            insured.get('business_name') or insured.get('first_name'))


def candidate_events(api):
    """Read-only normalization, never constructors of write-capable ports.

    Each event carries what a destination port needs to write and what
    ascend_destination_mapping needs to bind it. Amounts are Ascend
    ``*_cents`` fields (USD; Ascend does not send a currency on payouts).
    """
    events = []
    for item in api.fetch_cancelation_returns():
        key = item.get('id')
        if not key: raise ValueError('cancellation_missing_id')
        bill = item.get('billable') or {}
        policy = bill.get('policy_number')
        name = None
        program = None
        if bill.get('id'):
            detail = api.fetch_billable(bill['id'])
            policy = detail.get('policy_number') or detail.get('billable_identifier')
            if detail.get('program_id'):
                prog = api.fetch_program(detail['program_id'])
                program = prog
                name = (prog.get('insured') or {}).get('business_name')
        title = f"Ascend cancellation - policy {policy or 'unknown'}"
        events.append({'key': key, 'kind': 'cancellation',
                       'policy_number': policy, 'insured_name': name,
                       'program': program,
                       'title': title,
                       'note_text': f"{title}. Ascend cancellation return {key} for {name or 'the insured'}.",
                       'task_text': 'Review the Ascend cancellation and contact the insured.',
                       'due_date': _due(1)})
    for prog in api.fetch_programs():
        if str(prog.get('status', '')).lower() not in ('checked_out', 'purchased'): continue
        pid = prog.get('id')
        if not pid: raise ValueError('signed_missing_id')
        bills = prog.get('billables') or api.fetch_program_billables(pid)
        first = bills[0] if bills else {}
        policy = first.get('policy_number') or first.get('billable_identifier')
        name = (prog.get('insured') or {}).get('business_name')
        title = f"Ascend agreement signed - policy {policy or 'unknown'}"
        events.append({'key': 'signed_'+pid, 'kind': 'agreement_signed',
                       'policy_number': policy, 'insured_name': name,
                       'program': prog,
                       'title': title,
                       'note_text': f"{title}. Ascend program {pid} is {prog.get('status')}.",
                       'task_text': 'Bind the policy for the signed Ascend agreement.',
                       'due_date': _due(1)})
    for p in api.fetch_payouts():
        pid = p.get('id')
        if not pid: raise ValueError('payout_missing_id')
        typ, status = p.get('payout_type'), str(p.get('status', '')).lower()
        if typ == 'supplier' and status == 'paid': kind = 'supplier_payout'
        elif typ == 'commission' and status == 'paid': kind = 'commission_payout'
        elif typ == 'supplier' and status in ('failed', 'unpaid'): kind = 'accounting_issue'
        else: kind = 'unsupported'
        event = {'key': 'payout_'+pid, 'kind': kind, 'source_id': pid,
                 'source_type': typ, 'source_status': status}
        if kind == 'commission_payout':
            event.update({'amount_cents': p.get('net_payout_amount_cents'),
                          'currency': p.get('currency') or 'USD',
                          'txn_date': (p.get('paid_at') or '')[:10] or None})
        if kind == 'accounting_issue':
            policy, name = _program_policy(api, p.get('program_id'))
            amount = int(p.get('net_payout_amount_cents') or 0)
            title = f"Ascend supplier payout {status} - policy {policy or 'unknown'}"
            event.update({'policy_number': policy, 'insured_name': name, 'title': title,
                          'note_text': f"{title}: ${amount / 100:,.2f}.",
                          'task_text': 'Check the supplier remittance in Ascend.',
                          'due_date': _due(1)})
        events.append(event)
    return events
