"""Pure Test-only review calculation. No source authentication or money action.

Candidate crosswalks are never clearance. Evidence assertions supplied by an
adapter must be independently grounded before a real report is actionable.
"""
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation

class FeeReportError(ValueError):
    pass

def money(value):
    if isinstance(value, bool):
        raise FeeReportError('invalid money')
    try:
        v = Decimal(str(value))
        if not v.is_finite() or v < 0 or v != v.quantize(Decimal('0.01')):
            raise FeeReportError('nonnegative exact cents required')
    except (InvalidOperation, ValueError, TypeError):
        raise FeeReportError('invalid money')
    return v

def day(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise FeeReportError('ISO calendar date required')

def report(crosswalk, payments, returns, evidence, *, as_of, environment='TEST'):
    if environment != 'TEST':
        raise FeeReportError('Test only')
    now = day(as_of)
    exceptions = []
    blockers = []
    def issue(code, ref=None, blocking=True):
        exceptions.append({'code': code, 'reference': ref, 'blocking': blocking})
        if blocking:
            blockers.append(code)
    if crosswalk.get('mode') != 'test_candidate_review_only':
        issue('unsupported crosswalk')
    findings = crosswalk.get('findings', [])
    ids = [f.get('email_transfer_id') for f in findings]
    if any(not i for i in ids) or len(set(ids)) != len(ids):
        issue('missing or duplicate payout ID')
    candidates = {f.get('email_transfer_id'): f for f in findings}
    for f in findings:
        if f.get('status') != 'candidate_review_only':
            issue('payout crosswalk requires review', f.get('email_transfer_id'))
    for scope in ('earnings', 'returns', 'clearance', 'trust_capacity'):
        e = evidence.get(scope, {})
        if e.get('complete') is not True or e.get('as_of_date') != as_of or not e.get('source_reference'):
            issue('missing stale or incomplete '+scope+' evidence')
    try:
        capacity = money(evidence.get('trust_capacity', {}).get('earned_fee_capacity'))
    except FeeReportError:
        capacity = Decimal(0)
        issue('earned fee capacity unproved after carrier obligations and prior sweeps')
    pspcounts = Counter(p.get('psp_ref') for p in payments)
    if any(not p for p in pspcounts) or any(n != 1 for n in pspcounts.values()):
        issue('missing or duplicate payment PSP')
    rids = Counter(r.get('return_id') for r in returns)
    if any(not i for i in rids) or any(n != 1 for n in rids.values()):
        issue('missing or duplicate return ID')
    pmap = {p.get('psp_ref'): p for p in payments}
    blocked_psps = set()
    liabilities = Decimal(0)
    return_audit = []
    for r in returns:
        psp = r.get('psp_ref')
        blocked_psps.add(psp)
        if psp not in pmap:
            issue('unmapped return PSP', r.get('return_id'))
        if r.get('kind') not in ('refund', 'chargeback', 'ach_return') or r.get('state') not in ('open', 'reconciled'):
            issue('unsupported return kind or state', r.get('return_id'))
        try:
            if day(r.get('event_date')) > now:
                issue('future return event date', r.get('return_id'))
        except FeeReportError:
            issue('return event date missing', r.get('return_id'))
        if not r.get('source_reference'):
            issue('return source missing', r.get('return_id'))
        if r.get('method') != pmap.get(psp, {}).get('method'):
            issue('return payment method mismatch', r.get('return_id'))
        if r.get('method') == 'ach' and r.get('state') == 'open':
            issue('open ACH return blocks figure', r.get('return_id'))
        try:
            gross = money(r.get('gross_liability'))
            applied = money(r.get('already_debited'))
            remaining = money(r.get('remaining_liability'))
            if gross != applied + remaining:
                issue('return liability does not tie', r.get('return_id'))
            if applied and not r.get('debit_proof_reference'):
                issue('return debit proof missing', r.get('return_id'))
            if r.get('state') == 'reconciled' and remaining:
                issue('reconciled return has remaining liability', r.get('return_id'))
            liabilities += remaining
        except FeeReportError:
            remaining = None
            issue('return liability unknown or invalid', r.get('return_id'))
        return_audit.append({**r, 'remaining_liability': str(remaining) if remaining is not None else None})
        issue('return or dispute flagged; associated fees excluded', r.get('return_id'), False)
    for f in findings:
        for r in f.get('psp_return_chain', []):
            if r.get('psp_ref') not in blocked_psps:
                issue('crosswalk return chain absent from reconciled ledger', f.get('email_transfer_id'))
    mature = Decimal(0)
    held = Decimal(0)
    excluded = Decimal(0)
    audit = []
    payment_totals = {}
    for p in payments:
        psp = p.get('psp_ref')
        payout = p.get('payout_id')
        reasons = []
        if payout not in candidates:
            issue('payment payout unmapped', psp)
        if p.get('method') not in ('ach', 'card'):
            issue('unknown payment method; wallet needs verified underlying rail', psp)
        if not p.get('source_reference') or not p.get('fee_source_reference') or p.get('earned_verified') is not True or p.get('fee_kind') != 'agency_earned':
            issue('agency earned fee evidence missing; processor fee is not earnings', psp)
        c = p.get('clearance', {})
        if c.get('verified') is not True or not c.get('source_reference') or not c.get('bank_transaction_id') or c.get('account_last4') != '3021':
            issue('candidate is not cleared; independent Trust3021 proof missing', psp)
        f = candidates.get(payout, {})
        if c.get('email_transfer_id') != payout or c.get('bank_trn') != f.get('bank_trn'):
            issue('clearance binding mismatch', psp)
        try:
            earned, swept = money(p.get('earned_fee')), money(p.get('already_swept'))
            if swept > earned:
                raise FeeReportError('swept exceeds earned')
            if swept and not p.get('sweep_source_reference'):
                issue('prior sweep proof missing', psp)
            fee = earned - swept
            principal = money(p.get('net_payment'))
            if earned > principal:
                issue('earned fee exceeds payment', psp)
            payment_totals[payout] = payment_totals.get(payout, Decimal(0)) + principal
        except FeeReportError:
            fee = Decimal(0)
            issue('invalid earned fee payment or prior sweep', psp)
        try:
            dates = [day(p.get('payment_date'))]
            # Preserve a disagreement and hold until the later source date has aged.
            if p.get('alternate_payment_date'):
                dates.append(day(p['alternate_payment_date']))
            age = (now-max(dates)).days
            if age < 0:
                issue('future payment date', psp)
            if len(set(dates)) > 1:
                issue('payment date disagreement; later date used for age', psp, False)
        except FeeReportError:
            age = None
            issue('payment date missing or invalid', psp)
        if psp in blocked_psps:
            excluded += fee
            reasons.append('return-associated fee excluded')
        elif p.get('method') == 'ach' and (age is None or age < 7):
            held += fee
            reasons.append('ACH seven calendar days not elapsed')
        elif p.get('method') in ('ach', 'card') and age is not None and age >= 0:
            mature += fee
            reasons.append('card no hold' if p['method'] == 'card' else 'ACH seven calendar days elapsed')
        audit.append({**p, 'age_calendar_days': age, 'unswept_fee': str(fee), 'reasons': reasons})
    # Each payout needs an adapter-proved line net (including return adjustments).
    for pid in candidates:
        bound = evidence.get('payout_line_nets', {}).get(pid, {})
        try:
            expected = money(bound.get('expected_positive_payment_net'))
            if not bound.get('source_reference') or payment_totals.get(pid) != expected:
                issue('payout payment line coverage or arithmetic unproved', pid)
        except FeeReportError:
            issue('payout payment line net missing', pid)
    net = mature - liabilities
    if net < 0:
        issue('liabilities exceed mature earned fees', blocking=False)
    proposed = min(max(net, Decimal(0)), capacity)
    if proposed < max(net, Decimal(0)):
        issue('figure capped by reconciled available earned fee capacity', blocking=False)
    return {'mode': 'test_review_only', 'as_of': as_of,
            'policy': {'ach_calendar_days': 7, 'card_calendar_days': 0, 'ach_no_open_returns': True},
            'status': 'withheld_unknown' if blockers else 'reviewable_not_authorized',
            'movable_earned_fee': None if blockers else f'{proposed:.2f}',
            'diagnostics_only': {'mature_unswept_fees': f'{mature:.2f}', 'held_ach_fees': f'{held:.2f}',
                'excluded_return_fees': f'{excluded:.2f}', 'uncovered_return_liability': f'{liabilities:.2f}',
                'fee_minus_liability': f'{net:.2f}'},
            'exceptions': exceptions, 'payments': audit, 'return_ledger': return_audit,
            'candidate_findings': findings, 'bank_actions': 0, 'transfers': 0,
            'qbo_posts': 0, 'ezlynx_writes': 0, 'transfer_allowed': False}
