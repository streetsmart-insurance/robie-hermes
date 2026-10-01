"""Fail-closed Test phone controls. No vendor credentials or live adapters.

Approval records must come from a trusted owner-channel resolver, never a
caller-supplied boolean, vendor metadata, repository comment or peer message.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

class Refused(ValueError): pass

def fingerprint(value):
    return sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

@dataclass(frozen=True)
class Plan:
    campaign: str
    target: str
    audience: str
    directory_id: str
    script: str
    voicemail_script: str
    applicant_id: str
    discussion_id: str
    caller_id: str
    max_attempts: int = 1
    def digest(self): return fingerprint(self.__dict__)

@dataclass(frozen=True)
class Grant:
    evidence_id: str
    plan_digest: str
    expires: datetime
    allow_client: bool = False

@dataclass(frozen=True)
class Window:
    timezone: str
    start_hour: int
    end_hour: int
    def check(self, now):
        if now.tzinfo is None: raise Refused('aware clock required')
        if not 0 <= self.start_hour < self.end_hour <= 24: raise Refused('invalid window')
        local=now.astimezone(ZoneInfo(self.timezone))
        if local.weekday()>=5 or not self.start_hour<=local.hour<self.end_hour:
            raise Refused('weekday calling window closed')

SCHEMA='''CREATE TABLE IF NOT EXISTS campaigns(id TEXT PRIMARY KEY,target TEXT,digest TEXT,claimed TEXT,grant_id TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS attempts(campaign TEXT,seq INTEGER,status TEXT,call_id TEXT UNIQUE,PRIMARY KEY(campaign,seq));
CREATE TABLE IF NOT EXISTS notes(call_id TEXT PRIMARY KEY,applicant TEXT,discussion TEXT,body TEXT,status TEXT,note_id TEXT);'''

class Controls:
    def __init__(self,path,window,approvals,directory,dispatch,notes,clock):
        path=Path(path)
        if not path.is_absolute() or path.is_symlink() or not path.parent.is_dir() or path.parent.stat().st_mode & 0o077:
            raise Refused('private persistent directory required')
        if str(path).startswith('/tmp/'): raise Refused('persistent storage required')
        self.path=path;self.window=window;self.approvals=approvals;self.directory=directory
        self.dispatch=dispatch;self.notes=notes;self.clock=clock
        with self.db() as c:c.executescript(SCHEMA)
        path.chmod(0o600)
    def db(self):return sqlite3.connect(self.path)
    def validate(self,plan,grant_id):
        now=self.clock();self.window.check(now)
        if not plan.target.startswith('+') or not plan.target[1:].isdigit() or not 8<=len(plan.target)<=16:raise Refused('E164 target required')
        if not all([plan.campaign,plan.script,plan.applicant_id,plan.discussion_id,plan.caller_id]):raise Refused('script and bound note destination required')
        if plan.max_attempts not in (1,2):raise Refused('one or two attempts only')
        grant=self.approvals.resolve(grant_id)
        if not isinstance(grant,Grant) or not grant.evidence_id or grant.plan_digest!=plan.digest() or grant.expires<=now:raise Refused('exact recipient/scripts approval required')
        if plan.audience in ('carrier','finance'):
            entry=self.directory.resolve(plan.directory_id)
            if entry!={'audience':plan.audience,'phone':plan.target}:raise Refused('verified directory binding required')
        elif plan.audience=='client':
            if not grant.allow_client or plan.max_attempts!=1:raise Refused('single reviewed client exception required')
        else:raise Refused('audience refused')
        return grant,now
    def start(self,plan,grant_id):
        grant,now=self.validate(plan,grant_id)
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            prior=c.execute('SELECT digest FROM campaigns WHERE id=?',(plan.campaign,)).fetchone()
            if prior:
                if prior[0]!=plan.digest():raise Refused('campaign changed')
                return {'status':'already_claimed','dispatch':False}
            # Compare parsed timestamps, not textual ordering across offsets.
            recent=c.execute('SELECT claimed FROM campaigns WHERE target=?',(plan.target,)).fetchall()
            if any(datetime.fromisoformat(r[0])>now-timedelta(hours=24) for r in recent):raise Refused('24-hour target cooldown')
            if c.execute("SELECT 1 FROM attempts a LEFT JOIN notes n ON a.call_id=n.call_id WHERE a.status='unknown' OR (a.call_id IS NOT NULL AND (n.status IS NULL OR n.status!='verified'))").fetchone():raise Refused('unresolved call or post-call note blocks next campaign')
            c.execute('INSERT INTO campaigns VALUES(?,?,?,?,?)',(plan.campaign,plan.target,plan.digest(),now.isoformat(),grant.evidence_id))
            c.execute('INSERT INTO attempts VALUES(?,?,?,NULL)',(plan.campaign,1,'claimed'))
        return self._send(plan,1)
    def _send(self,plan,seq):
        # The committed claim consumes the exact per-call exception even if the
        # response is lost. No blind resubmission after a transport ambiguity.
        body={'phone_number':plan.target,'from':plan.caller_id,'task':plan.script,
              'voicemail':{'action':'leave_message','message':plan.voicemail_script} if seq==2 and plan.voicemail_script else {'action':'hangup'},'record':False}
        try:
            result=self.dispatch(body);call_id=result.get('call_id')
            status='accepted' if isinstance(call_id,str) and call_id else 'unknown'
        except Exception:call_id=None;status='unknown'
        with self.db() as c:c.execute('UPDATE attempts SET status=?,call_id=? WHERE campaign=? AND seq=?',(status,call_id,plan.campaign,seq))
        return {'status':status,'call_id':call_id,'dispatch':True,'retry_allowed':False}
    def redial(self,plan,grant_id,first_detail):
        _,now=self.validate(plan,grant_id)
        if plan.max_attempts!=2:raise Refused('redial not reviewed')
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            campaign=c.execute('SELECT digest,claimed FROM campaigns WHERE id=?',(plan.campaign,)).fetchone()
            first=c.execute('SELECT status,call_id FROM attempts WHERE campaign=? AND seq=1',(plan.campaign,)).fetchone()
            if not campaign or campaign[0]!=plan.digest() or not first or first[0]!='accepted':raise Refused('no confirmed first dispatch')
            # The caller must provide fresh provider readback, never POST status.
            if first_detail.get('call_id')!=first[1] or first_detail.get('outcome')!='voicemail_no_message' or not first_detail.get('ended_at'):raise Refused('conclusive first voicemail required')
            ended=datetime.fromisoformat(first_detail['ended_at'])
            if ended.tzinfo is None or not 10<= (now-ended).total_seconds() or (now-datetime.fromisoformat(campaign[1])).total_seconds()>180:raise Refused('redial window closed')
            try:c.execute('INSERT INTO attempts VALUES(?,?,?,NULL)',(plan.campaign,2,'claimed'))
            except sqlite3.IntegrityError:return {'status':'already_claimed','dispatch':False}
        return self._send(plan,2)
    def finish(self,plan,call_id,detail):
        # Read-back port is required for notes; no UI note write is supported.
        if detail.get('call_id')!=call_id or not detail.get('ended_at') or detail.get('outcome') not in ('human_reached','voicemail_no_message','voicemail_message_left','no_answer','busy','failed','screener_declined'):raise Refused('terminal call evidence required')
        ended=datetime.fromisoformat(detail['ended_at'])
        if ended.tzinfo is None or ended>self.clock():raise Refused('valid ended timestamp required')
        with self.db() as c:
            if not c.execute('SELECT 1 FROM attempts WHERE campaign=? AND call_id=?',(plan.campaign,call_id)).fetchone():raise Refused('call not bound to campaign')
            row=c.execute('SELECT digest FROM campaigns WHERE id=?',(plan.campaign,)).fetchone()
            if row[0]!=plan.digest():raise Refused('note destination changed')
            body=f"Call ended {detail['ended_at']}. Call ID: {call_id}. Outcome: {detail['outcome']}."
            c.execute('INSERT OR IGNORE INTO notes VALUES(?,?,?,?,?,NULL)',(call_id,plan.applicant_id,plan.discussion_id,body,'pending'))
            row=c.execute('SELECT status,note_id FROM notes WHERE call_id=?',(call_id,)).fetchone()
            if row[0]=='verified':return {'status':'verified','note_id':row[1]}
        # Lookup first makes reconciliation after an ambiguous note POST safe.
        found=self.notes.find(call_id,plan.applicant_id,plan.discussion_id)
        if not found:
            with self.db() as c:
                c.execute('BEGIN IMMEDIATE')
                state=c.execute('SELECT status FROM notes WHERE call_id=?',(call_id,)).fetchone()[0]
                if state!='pending':return {'status':'note_unknown','note_id':None}
                c.execute('UPDATE notes SET status=? WHERE call_id=?',('posting',call_id))
            try:found=self.notes.append(plan.applicant_id,plan.discussion_id,body,call_id)
            except Exception:return {'status':'note_unknown','note_id':None}
        read=self.notes.read(found)
        if read!={'applicant_id':plan.applicant_id,'discussion_id':plan.discussion_id,'body':body}:raise Refused('note readback mismatch')
        with self.db() as c:c.execute('UPDATE notes SET status=?,note_id=? WHERE call_id=?',('verified',found,call_id))
        return {'status':'verified','note_id':found}
