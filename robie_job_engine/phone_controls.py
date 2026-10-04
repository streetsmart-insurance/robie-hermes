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

def _require_call_bounds(plan):
    if not isinstance(plan.voice_id,str) or not plan.voice_id.strip():
        raise Refused('reviewed voice ID required')
    # Bland max_duration is minutes. Test cap is one minute; no default voice or duration.
    if type(plan.max_duration_minutes) is not int or plan.max_duration_minutes>1 or plan.max_duration_minutes<1:
        raise Refused('max duration must be one minute')

_LAST_NOTE_KEYS=('LastNoteId','lastNoteId','mostRecentNoteId','MostRecentNoteId')
_NOTE_COUNT_KEYS=('noteCount','NoteCount','note_count')
_NOTE_MODIFIED_KEYS=('lastModified','LastModified','last-modified','last_modified')
_NOTE_TEXT_KEYS=('body','note','text','noteText','lastNoteText')
_CREATED_NOTE_KEYS=('note_id','noteId','NoteId','id','Id')
_TITLE_KEYS=('title','Title','subject','Subject')
_APPLICANT_KEYS=('applicantId','ApplicantId','applicant_id')
CALL_OUTCOMES=('human_reached','voicemail_no_message','voicemail_message_left','no_answer','busy','failed','screener_declined')
DRY_OUTCOMES=('dry_run','no_call')

def _normalize_note_id(value):
    # Live GET v8/discussions/{id} returns the latest note id as a JSON number.
    # Bool is an int subclass and is never a note id.
    if isinstance(value,bool) or value is None:return ''
    if isinstance(value,int):return str(value)
    if isinstance(value,str):return value.strip()
    return ''

def _created_note_id(result):
    """Note id from a write, or None when the live response carries none."""
    if not isinstance(result,dict):
        found=_normalize_note_id(result)
        return found or None
    for key in _CREATED_NOTE_KEYS:
        if key not in result:continue
        found=_normalize_note_id(result.get(key))
        if found:return found
    return None

def _lookup_value(snapshot, keys):
    if not isinstance(snapshot,dict):return None
    for key in keys:
        if key in snapshot:return snapshot.get(key)
    return None

def _latest_note_id(snapshot, required=True):
    found=_normalize_note_id(_lookup_value(snapshot,_LAST_NOTE_KEYS))
    if not found:
        if required:raise Refused('discussion lookup missing latest note id')
        return ''
    return found

def _note_count(snapshot, required=False):
    value=_lookup_value(snapshot,_NOTE_COUNT_KEYS)
    if value is None:
        if required:raise Refused('note readback mismatch')
        return None
    if type(value) is not int:raise Refused('note readback mismatch')
    return value

def _note_modified(snapshot):
    value=_lookup_value(snapshot,_NOTE_MODIFIED_KEYS)
    if value is None:return None
    if not isinstance(value,str) or not value.strip():raise Refused('note readback mismatch')
    return value.strip()

def _plain_field(snapshot, keys):
    value=_lookup_value(snapshot, keys)
    if isinstance(value,bool) or value is None:return ''
    if isinstance(value,int):return str(value)
    if isinstance(value,str):return value.strip()
    return ''

def _confirm_returned_note(note_id, snapshot, created, body):
    if _latest_note_id(snapshot)!=note_id:raise Refused('note readback mismatch')
    count=_note_count(snapshot)
    if count is not None and count<1:raise Refused('note readback mismatch')
    _note_modified(snapshot)
    for text in _note_texts(created, snapshot):
        if text!=body:raise Refused('note readback mismatch')

def _confirm_added_note(before, after, again, plan, created, body):
    """One new note, a new latest id, same title and applicant, second read agrees.

    This is the live path: POST v8/discussions/{id}/notes often returns 2xx
    with no note id. The matched id is the new latest note id.
    """
    before_count=_note_count(before, required=True)
    after_count=_note_count(after, required=True)
    if after_count!=before_count+1:raise Refused('note readback mismatch')
    before_latest=_latest_note_id(before, required=False)
    after_latest=_latest_note_id(after)
    if after_latest==before_latest:raise Refused('note readback mismatch')
    before_title=_plain_field(before,_TITLE_KEYS)
    after_title=_plain_field(after,_TITLE_KEYS)
    if not after_title or after_title!=before_title:raise Refused('note readback mismatch')
    want=str(plan.applicant_id or '').strip()
    before_applicant=_plain_field(before,_APPLICANT_KEYS)
    after_applicant=_plain_field(after,_APPLICANT_KEYS)
    if not after_applicant or after_applicant!=before_applicant or after_applicant!=want:
        raise Refused('note readback mismatch')
    after_modified=_note_modified(after)
    _note_modified(before)
    for text in _note_texts(created, after):
        if text!=body:raise Refused('note readback mismatch')
    if _note_count(again, required=True)!=after_count:raise Refused('note readback mismatch')
    if _latest_note_id(again)!=after_latest:raise Refused('note readback mismatch')
    if _plain_field(again,_TITLE_KEYS)!=after_title:raise Refused('note readback mismatch')
    if _plain_field(again,_APPLICANT_KEYS)!=after_applicant:raise Refused('note readback mismatch')
    if _note_modified(again)!=after_modified:raise Refused('note readback mismatch')
    for text in _note_texts(None, again):
        if text!=body:raise Refused('note readback mismatch')
    return after_latest

def _note_texts(created, snapshot):
    texts=[]
    for source in (created, snapshot):
        if not isinstance(source,dict):continue
        for key in _NOTE_TEXT_KEYS:
            if key not in source:continue
            value=source.get(key)
            if value is None:continue
            texts.append(value)
    return texts

def dry_run_note(test_id):
    return f"Robie phone Test: dry run only, no call placed. Ref: {test_id}."

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
    voice_id: str
    max_duration_minutes: int
    max_attempts: int = 1
    unrestricted_hours: bool = False
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
        now=self.clock()
        if now.tzinfo is None:raise Refused("aware clock required")
        if type(plan.unrestricted_hours) is not bool:raise Refused("explicit hours policy required")
        if not plan.unrestricted_hours:self.window.check(now)
        if not plan.target.startswith('+') or not plan.target[1:].isdigit() or not 8<=len(plan.target)<=16:raise Refused('E164 target required')
        if not all([plan.campaign,plan.script,plan.applicant_id,plan.discussion_id,plan.caller_id]):raise Refused('script and bound note destination required')
        _require_call_bounds(plan)
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
        _require_call_bounds(plan)
        body={'phone_number':plan.target,'from':plan.caller_id,'voice':plan.voice_id,
              'max_duration':plan.max_duration_minutes,'task':plan.script,
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
        # Read-back matches a note id (number or string) to a fresh discussion
        # lookup. When the write has no id, the discussion was read before the
        # post and is confirmed by count, latest id, title and applicant.
        # GET .../notes is HTTP 405, so this path never lists notes.
        outcome=detail.get('outcome')
        if outcome in DRY_OUTCOMES:
            if call_id or detail.get('call_id'):raise Refused('dry run must not carry a call id')
            test_id=str(detail.get('test_id') or '').strip()
            if not test_id:raise Refused('dry run test id required')
            body=dry_run_note(test_id)
            if 'Call ended' in body or 'Call ID' in body:raise Refused('dry run note must not look like a call')
            with self.db() as c:
                row=c.execute('SELECT digest FROM campaigns WHERE id=?',(plan.campaign,)).fetchone()
                if row and row[0]!=plan.digest():raise Refused('note destination changed')
            return self._file_note(plan,'dry-run:'+test_id,body)
        if not isinstance(call_id,str) or not call_id.strip():raise Refused('real dispatch call id required')
        if detail.get('call_id')!=call_id or not detail.get('ended_at') or outcome not in CALL_OUTCOMES:raise Refused('terminal call evidence required')
        ended=datetime.fromisoformat(detail['ended_at'])
        if ended.tzinfo is None or ended>self.clock():raise Refused('valid ended timestamp required')
        with self.db() as c:
            attempt=c.execute('SELECT status FROM attempts WHERE campaign=? AND call_id=?',(plan.campaign,call_id)).fetchone()
            if not attempt or attempt[0]!='accepted':raise Refused('real dispatch required')
            row=c.execute('SELECT digest FROM campaigns WHERE id=?',(plan.campaign,)).fetchone()
            if row[0]!=plan.digest():raise Refused('note destination changed')
        # Only an accepted dispatch may use the call-ended line.
        body=f"Call ended {detail['ended_at']}. Call ID: {call_id}. Outcome: {outcome}."
        return self._file_note(plan,call_id,body)
    def _fetch_discussion(self,plan,before_post):
        try:snapshot=self.notes.lookup_discussion(plan.applicant_id,plan.discussion_id)
        except Refused:raise
        except Exception:
            if before_post:raise Refused('discussion could not be read before the note was sent')
            raise Refused('note readback mismatch')
        if not isinstance(snapshot,dict):
            if before_post:raise Refused('discussion could not be read before the note was sent')
            raise Refused('note readback mismatch')
        # Parse before any write so a bad snapshot cannot post and then refuse.
        _note_count(snapshot)
        _latest_note_id(snapshot, required=False)
        _note_modified(snapshot)
        return snapshot
    def _file_note(self,plan,note_key,body):
        with self.db() as c:
            c.execute('INSERT OR IGNORE INTO notes VALUES(?,?,?,?,?,NULL)',(note_key,plan.applicant_id,plan.discussion_id,body,'pending'))
            row=c.execute('SELECT status,note_id FROM notes WHERE call_id=?',(note_key,)).fetchone()
            if row[0]=='verified':return {'status':'verified','note_id':row[1]}
        # Lookup by the id we already stored. Never list the discussion's notes.
        found=self.notes.find(note_key,plan.applicant_id,plan.discussion_id)
        created=found
        before=None
        if not found:
            with self.db() as c:
                c.execute('BEGIN IMMEDIATE')
                state=c.execute('SELECT status FROM notes WHERE call_id=?',(note_key,)).fetchone()[0]
                if state!='pending':return {'status':'note_unknown','note_id':None}
            # The write response often has no note id, so the before-image has
            # to exist before the post. A failed read does not post.
            before=self._fetch_discussion(plan,before_post=True)
            with self.db() as c:
                c.execute('BEGIN IMMEDIATE')
                state=c.execute('SELECT status FROM notes WHERE call_id=?',(note_key,)).fetchone()[0]
                if state!='pending':return {'status':'note_unknown','note_id':None}
                c.execute('UPDATE notes SET status=? WHERE call_id=?',('posting',note_key))
            try:created=self.notes.append(plan.applicant_id,plan.discussion_id,body,note_key)
            except Exception:return {'status':'note_unknown','note_id':None}
        note_id=_created_note_id(created)
        snapshot=self._fetch_discussion(plan,before_post=False)
        if note_id:_confirm_returned_note(note_id,snapshot,created,body)
        else:
            if before is None:raise Refused('append response did not include a note id')
            again=self._fetch_discussion(plan,before_post=False)
            note_id=_confirm_added_note(before,snapshot,again,plan,created,body)
        with self.db() as c:c.execute('UPDATE notes SET status=?,note_id=? WHERE call_id=?',('verified',note_id,note_key))
        return {'status':'verified','note_id':note_id}
