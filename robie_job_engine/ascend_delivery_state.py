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
    def send(self, event: dict, key: str) -> dict: ...
    def readback(self, ids: dict) -> dict: ...

REQUIRED = {'cancellation': ('note_id', 'task_id'),
            'agreement_signed': ('note_id', 'task_id'),
            'accounting_issue': ('task_id',),
            'commission_payout': ('deposit_id',)}

def verified(event, receipt, port):
    ids = receipt.get('ids', {})
    needed = REQUIRED.get(event['kind'], ())
    if not needed or not all(ids.get(k) for k in needed):
        return None
    read = port.readback(ids)
    # Readback must bind every ID to this source key and intended applicant.
    for k in needed:
        obj = read.get(k) or {}
        if (str(obj.get('id')) != str(ids[k]) or
            obj.get('source_key') != event['key'] or
            obj.get('applicant_id') != event.get('applicant_id')):
            return None
    return {k: str(ids[k]) for k in needed}

class ReliableDelivery:
    def __init__(self, ledger, destination=None):
        self.ledger = ledger
        self.destination = destination

    def process(self, event):
        try:
            return self._process(event)
        finally:
            key = event['key']
            if key in self.ledger:
                self.ledger[key] = self.ledger[key]

    def _process(self, event):
        key = event['key']
        state = self.ledger.setdefault(key, Delivery())
        if state.delivered:
            return 'delivered_skip'
        kind = event['kind']
        if kind == 'supplier_payout':
            state.reason = 'supplier_accounting_disabled'
            return state.reason
        if kind not in REQUIRED:
            state.reason = 'unsupported_type_status'
            return state.reason
        if kind in ('cancellation', 'agreement_signed') and not event.get('applicant_id'):
            state.reason = 'unmatched_retryable'
            return state.reason
        # Legacy FLAGGED/task/unknown attempts may have landed despite no receipt.
        if event.get('legacy_uncertain'):
            state.attempted = True
        state.matched = True
        state.staged = True
        if self.destination is None:
            state.reason = 'staged_no_live_destination'
            return state.reason
        port = self.destination
        try:
            existing = port.find_by_key(key)
            if existing:
                ids = verified(event, existing, port)
                if ids:
                    state.delivered = True
                    state.destination_ids = ids
                    state.reason = 'delivered_readback'
                    return state.reason
                state.reason = 'existing_destination_unverified'
                state.attempted = True
                return state.reason
            if state.attempted:
                state.reason = 'attempt_uncertain_recovery_only'
                return state.reason
            # Persist before sending. No write retry after timeout/unknown receipt.
            state.attempted = True
            self.ledger[key] = state
            if not isinstance(self.ledger, DurableLedger) and not getattr(port, 'synthetic', False):
                state.reason = 'durable_storage_required'
                return state.reason
            receipt = port.send(event, key)
            ids = verified(event, receipt, port)
            if not ids:
                state.reason = 'destination_unverified'
                return state.reason
            state.delivered = True
            state.destination_ids = ids
            state.reason = 'delivered_readback'
            return state.reason
        except Exception as exc:
            state.reason = 'destination_error_' + type(exc).__name__
            return state.reason


def paginate(get_page, path, page_size=50, max_pages=100):
    """Read every cursor page; reject loops, bad shape and cross-origin links.

    get_page(path, query) is a read-only callable. Never follows an arbitrary URL.
    """
    result, seen, cursor = [], set(), None
    for _ in range(max_pages):
        query = {'page_size': page_size}
        if cursor is not None: query['starting_after'] = cursor
        page = get_page(path, query)
        data = page.get('data')
        if not isinstance(data, list): raise ValueError('invalid_page_data')
        result.extend(data)
        meta = page.get('pagination') or page.get('meta') or {}
        nxt = meta.get('next_cursor') or page.get('next_cursor')
        more = meta.get('has_more', page.get('has_more', False))
        if page.get('next') or meta.get('next_url'):
            raise ValueError('url_pagination_requires_vendor_binding')
        if not nxt:
            if more: raise ValueError('missing_next_cursor')
            if len(data) >= page_size and not meta and 'has_more' not in page:
                raise ValueError('pagination_contract_unverified_full_page')
            return result
        if nxt in seen or not isinstance(nxt, str) or '://' in nxt:
            raise ValueError('unsafe_or_repeated_cursor')
        seen.add(nxt); cursor = nxt
    raise ValueError('page_limit_exceeded')

def candidate_events(api):
    """Read-only normalization, never constructors of write-capable ports."""
    events = []
    for item in api.fetch_cancelation_returns():
        key = item.get('id')
        if not key: raise ValueError('cancellation_missing_id')
        bill = item.get('billable') or {}
        policy = bill.get('policy_number')
        name = None
        if bill.get('id'):
            detail = api.fetch_billable(bill['id'])
            policy = detail.get('policy_number') or detail.get('billable_identifier')
            if detail.get('program_id'):
                prog = api.fetch_program(detail['program_id'])
                name = (prog.get('insured') or {}).get('business_name')
        events.append({'key': key, 'kind': 'cancellation',
                       'policy_number': policy, 'insured_name': name})
    for prog in api.fetch_programs():
        if str(prog.get('status', '')).lower() not in ('checked_out', 'purchased'): continue
        pid = prog.get('id')
        if not pid: raise ValueError('signed_missing_id')
        bills = prog.get('billables') or api.fetch_program_billables(pid)
        first = bills[0] if bills else {}
        events.append({'key': 'signed_'+pid, 'kind': 'agreement_signed',
                       'policy_number': first.get('policy_number') or first.get('billable_identifier'),
                       'insured_name': (prog.get('insured') or {}).get('business_name')})
    for p in api.fetch_payouts():
        pid = p.get('id')
        if not pid: raise ValueError('payout_missing_id')
        typ, status = p.get('payout_type'), str(p.get('status', '')).lower()
        if typ == 'supplier' and status == 'paid': kind = 'supplier_payout'
        elif typ == 'commission' and status == 'paid': kind = 'commission_payout'
        elif typ == 'supplier' and status in ('failed', 'unpaid'): kind = 'accounting_issue'
        else: kind = 'unsupported'
        events.append({'key': 'payout_'+pid, 'kind': kind,
                       'source_type': typ, 'source_status': status})
    return events
