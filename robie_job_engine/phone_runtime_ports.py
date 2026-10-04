"""Test runtime ports. Signed approval verification; explicit directory input.

No live write/call methods are exposed by this draft. Real source reads can
be connected independently; fixture replay is the only dispatch transport.
"""
from datetime import datetime,timezone
import hashlib,hmac,json
from pathlib import Path
from .phone_controls import Grant,Refused

class SignedApprovalResolver:
    """Verify records created by a separately authenticated owner-channel bridge.
    There is no mint API here. Possessing a name/message ID is not approval.
    Provisioning the bridge/signing key is separate work; fail closed without it.
    """
    def __init__(self,records,key,clock):
        if not isinstance(key,bytes) or len(key)<32:raise Refused('trusted approval bridge key missing')
        self.records=records;self.key=key;self.clock=clock
    def resolve(self,grant_id):
        record=self.records.get(grant_id)
        if not isinstance(record,dict):raise Refused('signed approval missing')
        payload=record.get('payload');signature=record.get('signature')
        if not isinstance(payload,dict) or not isinstance(signature,str):raise Refused('approval shape invalid')
        raw=json.dumps(payload,sort_keys=True,separators=(',',':')).encode()
        expected=hmac.new(self.key,raw,hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature,expected):raise Refused('approval signature invalid')
        fields={'grant_id','evidence_id','principal','channel','plan_digest','expires','allow_client','environment'}
        if set(payload)!=fields or payload['grant_id']!=grant_id or payload['environment']!='TEST':raise Refused('approval scope invalid')
        if payload['principal']!='owner' or payload['channel'] not in ('iMessage','WhatsApp','agent_chat','mobile_chat'):raise Refused('trusted owner channel required')
        if not isinstance(payload['allow_client'],bool) or not payload['evidence_id']:raise Refused('approval evidence incomplete')
        expires=datetime.fromisoformat(payload['expires'])
        if expires.tzinfo is None or expires<=self.clock():raise Refused('approval expired')
        return Grant(payload['evidence_id'],payload['plan_digest'],expires,payload['allow_client'])

class VerifiedDirectory:
    """Explicit current record lookup. Caller cannot supply a source label only."""
    def __init__(self,reader,clock,max_age_hours):
        if not 0<max_age_hours<=24:raise Refused('bounded directory freshness required')
        self.reader=reader;self.clock=clock;self.max_age=max_age_hours*3600
    def resolve(self,record_id):
        r=self.reader(record_id)
        if not isinstance(r,dict) or r.get('id')!=record_id or r.get('audience') not in ('carrier','finance') or not r.get('source_ref'):raise Refused('verified directory record required')
        checked=datetime.fromisoformat(r['verified_at'])
        if checked.tzinfo is None or not 0<=(self.clock()-checked).total_seconds()<=self.max_age:raise Refused('directory stale')
        return {'audience':r['audience'],'phone':r['phone']}

class DiscussionSource:
    """Reads source cards using an injected authenticated Test reader.
    Unknown shapes or read errors are blockers, never an empty successful scan.
    The lower-level reader must retrieve portal card labels without swallowing
    HTTP errors. The current OAuth discussions helper is NOT used implicitly.
    """
    def __init__(self,reader,allowed_applicants):self.reader=reader;self.allowed=set(allowed_applicants)
    def cards(self,applicant):
        if applicant not in self.allowed:raise Refused('Test read applicant not allowlisted')
        rows=self.reader(applicant)
        if not isinstance(rows,list):raise Refused('unverified source-card schema')
        for card in rows:
            if not isinstance(card,dict) or str(card.get('applicantId'))!=applicant or not isinstance(card.get('discussionNote'),dict):raise Refused('unverified source-card schema')
        return rows

class LiveEffectsDisabled:
    def __call__(self,body):raise Refused('live calls disabled: no per-call execute grant in this Test assignment')
    def append(self,*args):raise Refused('real EZLynx note writes disabled in this Test assignment')
