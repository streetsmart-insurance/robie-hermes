"""Offline Bland voice quality scoring. No calls or source-system writes.

Inputs are independently reviewed test artifacts. Transcript wording is never
proof that a call reached someone, that an address is correct, or that a write
landed. Caller must supply call IDs, owned number permission, audio-review
status, and source-system readback evidence before grading an effect.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import re
from typing import Any, Mapping, Sequence


_ALLOWED_CONFIRMS = frozenset({'yes', 'yes that is correct', 'that is correct', 'correct'})
_NON_SOURCE = frozenset({'bland transcript', 'bland', 'call transcript', 'call dispatch'})


def _address(value: str) -> str:
    """Normalize typography only; do not autocorrect a street or city."""
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', str(value).lower())).strip()


def grade_address(case: Mapping[str, Any]) -> dict:
    required=('case_id','expected_address','heard_address','repeat_back','confirmation')
    if any(not case.get(k) for k in required):
        return {'case_id':case.get('case_id',''), 'exact_match':False,
                'confirmation_valid':False,'safe_for_write':False,'reason':'missing evidence'}
    expected=_address(case['expected_address'])
    heard=_address(case['heard_address'])
    readback=_address(case['repeat_back'])
    exact=bool(expected and expected == heard == readback)
    confirmation=str(case['confirmation']).strip().lower().rstrip('.!') in _ALLOWED_CONFIRMS
    audio=case.get('audio_reviewed') is True
    return {'case_id':case['case_id'],'exact_match':exact,
            'confirmation_valid':bool(confirmation and readback==expected),
            'safe_for_write':bool(exact and confirmation and audio),
            'reason':('pass - human-reviewed audio' if exact and confirmation and audio else
                      'needs independent audio/address review or correction')}


def build_test_script(case_id: str, address: str) -> str:
    if not case_id or not address:
        raise ValueError('case ID and invented test address required')
    return (f'Test {case_id}: say the address "{address}" once, then ask the agent '
            'to repeat the street, city, state and ZIP exactly. Answer "yes" only '
            'if the repeat-back is exactly right; otherwise correct it and retry. '
            'Do not request or claim a customer-record update. End the test call.')


def validate_test_plan(plan: Mapping[str, Any]) -> bool:
    number=str(plan.get('number') or '')
    return bool(re.fullmatch(r'\+[1-9]\d{7,14}',number) and
                plan.get('owner_confirmed') is True and
                plan.get('case_ids') and len(set(plan['case_ids']))==len(plan['case_ids']))


def score_calls(calls: Sequence[Mapping[str, Any]]) -> dict:
    seen=set();counts=Counter()
    for call in calls:
        cid=call.get('call_id')
        if not cid or cid in seen:
            raise ValueError('missing or duplicate call ID')
        seen.add(cid)
        status=str(call.get('status','')).lower()
        if status not in ('completed','complete'):
            counts['unconfirmed']+=1;continue
        answered=str(call.get('answered_by','')).lower()
        if answered=='human' and call.get('human_conversation') is True:
            counts['human_reached']+=1
        elif answered=='voicemail':
            if call.get('voicemail_message_confirmed') is True:
                counts['voicemail_left']+=1
            else:
                counts['died_at_voicemail']+=1
        else:
            counts['unconfirmed']+=1
    n=len(calls)
    return {'total':n, **{k:counts[k] for k in
            ('human_reached','voicemail_left','died_at_voicemail','unconfirmed')},
            'effective_contact_rate':(counts['human_reached']+counts['voicemail_left'])/n if n else None}


def _time(s):
    try:
        return datetime.fromisoformat(str(s).replace('Z','+00:00'))
    except ValueError:
        return None


def verify_claims(claims: Sequence[Mapping[str, Any]],
                  readbacks: Sequence[Mapping[str, Any]]) -> dict:
    counts=Counter();details=[]
    for claim in claims:
        relevant=[]
        for r in readbacks:
            if not r.get('source_id') or str(r.get('source','')).lower() in _NON_SOURCE:
                continue
            if r.get('target') != claim.get('target') or r.get('action') != claim.get('action'):
                continue
            t0=_time(claim.get('claimed_at'));t1=_time(r.get('observed_at'))
            if not t0 or not t1 or not t0.tzinfo or not t1.tzinfo or t1<t0:
                continue
            relevant.append(r)
        if not relevant:
            status='unverified'
        elif any(_address(r.get('value',''))==_address(claim.get('value','')) for r in relevant):
            status='verified'
        else:
            status='mismatched'
        counts[status]+=1
        details.append({'call_id':claim.get('call_id'), 'target':claim.get('target'),
                        'status':status,'source_ids':[r['source_id'] for r in relevant]})
    return {'verified':counts['verified'],'mismatched':counts['mismatched'],
            'unverified':counts['unverified'],'details':details}
