"""Test-only empirical candidate crosswalk. Never qualifies cleared funds.

No network, financial posting, bank action, scheduling or source authentication.
Characters 7-11 are a correlation observed in one historical window, not a
vendor-guaranteed identifier. Keep full IDs and PSP return chains for review.
"""
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
import re

REF = re.compile(r'[A-Z0-9]{16}')
class CrosswalkError(ValueError): pass

def _money(value):
    try: result = Decimal(str(value))
    except (InvalidOperation, ValueError): raise CrosswalkError('invalid amount')
    if not result.is_finite() or result != result.quantize(Decimal('.01')):
        raise CrosswalkError('amount must be exact cents')
    return result

def _key(ref):
    return ref[6:11] if isinstance(ref, str) and REF.fullmatch(ref) else None

def _day(value):
    try: return date.fromisoformat(value)
    except (TypeError, ValueError): return None

def candidates(payouts, bank_rows, *, environment='TEST'):
    if environment != 'TEST': raise CrosswalkError('Test only')
    # Count across ALL supplied rows, including wrong accounts/status. Collisions
    # must not disappear because a later filter makes one seem convenient.
    pkeys=Counter(_key(p.get('email_transfer_id')) for p in payouts)
    bkeys=Counter(_key(b.get('bank_trn')) for b in bank_rows)
    pref=Counter(p.get('email_transfer_id') for p in payouts)
    bids=Counter(b.get('bank_transaction_id') for b in bank_rows if b.get('bank_transaction_id'))
    results=[]; used=set()
    for p in payouts:
        ref=p.get('email_transfer_id'); key=_key(ref); reasons=[]
        result={'email_transfer_id':ref,'email_source':p.get('source_reference'),
                'psp_return_chain':p.get('psp_return_chain',[]),'bank_trn':None,
                'bank_payout_descriptor':None,'bank_source':None,
                'status':'needs_review','review_reasons':reasons,
                'qualifies_cleared_funds':False,'posting_allowed':False}
        if not key: reasons.append('missing or unsupported email key')
        if key and (pkeys[key]!=1 or bkeys[key]>1): reasons.append('key collision or duplicate payout')
        if pref[ref]!=1: reasons.append('duplicate full email ID')
        if p.get('account_last4')!='3021': reasons.append('not Trust3021')
        if p.get('grouped') or p.get('lines_tie') is False: reasons.append('grouped or untied settlement')
        if not p.get('source_reference'): reasons.append('email source missing')
        if not isinstance(p.get('psp_return_chain',[]),list): reasons.append('invalid PSP return chain')
        matches=[(i,b) for i,b in enumerate(bank_rows) if key and _key(b.get('bank_trn'))==key]
        if len(matches)!=1: reasons.append('no unique bank key candidate')
        if len(matches)==1:
            i,b=matches[0];result.update(bank_trn=b.get('bank_trn'),bank_payout_descriptor=b.get('bank_payout_descriptor'),bank_source=b.get('source_reference'),bank_transaction_id=b.get('bank_transaction_id'))
            if b.get('account_last4')!='3021': reasons.append('bank account not Trust3021')
            if b.get('direction')!='credit' or b.get('bank_status')!='posted': reasons.append('not a posted positive credit')
            if b.get('grouped') or b.get('return_or_reversal_candidate'): reasons.append('grouped or return/reversal bank row')
            if not b.get('source_reference'): reasons.append('bank source missing')
            if b.get('bank_transaction_id') and bids[b['bank_transaction_id']]!=1: reasons.append('duplicate stable bank ID')
            try:
                a,z=_money(p.get('net_amount')),_money(b.get('amount'))
                if a<=0 or a!=z: reasons.append('amount mismatch or nonpositive settlement')
            except CrosswalkError: reasons.append('invalid amount')
            pd,bd=_day(p.get('settlement_date')),_day(b.get('bank_date'))
            lag=(bd-pd).days if pd and bd else None
            result['lag_calendar_days']=lag
            if lag is None or not 1<=lag<=5: reasons.append('date outside observed1-5day window')
            if i in used: reasons.append('bank row already used')
            if not reasons:
                used.add(i);result['status']='candidate_review_only'
                reasons.extend(['empirical key only; vendor crosswalk unverified','posted is not clearing proof'])
                if p.get('lines_tie') is not True: reasons.append('settlement line reconciliation unverified')
                if not b.get('bank_transaction_id'): reasons.append('stable bank transaction ID missing')
        results.append(result)
    return {'mode':'test_candidate_review_only','findings':results,
            'unmatched_bank_rows':[{'bank_trn':b.get('bank_trn'),'source_reference':b.get('source_reference')} for i,b in enumerate(bank_rows) if i not in used],
            'cleared_bank_deposits':[], 'bank_actions':0,'qbo_posts':0,'ezlynx_writes':0}
