"""Exactly one owner-reviewed Test call. Separate from carrier campaigns.

No default voice, caller identity or implicit execute. Permanent atomic claim
before dispatch; HTTP ambiguity never retries. No retry/voicemail/SMS/webhook.
"""
from __future__ import annotations
from datetime import date
import json
from pathlib import Path
import sqlite3
import socket
import urllib.request

TARGET = "+17326688161"
CALLER = "+17322986745"
SECRET = "projects/streetsmart-hermes-poc/secrets/robie-test-bland-api-key/versions/1"
OPENING = "Hi Jake. This is Eva, an AI assistant with StreetSmart Insurance. Carlo asked me to make this test call. Can you hear me clearly?"
FOLLOWUP = "I'm testing the connection only. I won't discuss clients or make any changes. Does my voice sound clear and natural?"
CLOSE = "Thanks, Jake. That completes the test. Goodbye."
TASK = ("Speak slowly and evenly. Your only purpose is this owner-approved connection test. "
        "After the opening, wait for Jake's response, then say exactly: " + FOLLOWUP +
        " Wait for his reply, then say exactly: " + CLOSE +
        " End the call. Do not discuss clients, insurance advice, money or any other task. "
        "If a screening assistant asks who/why, identify yourself as Eva, an AI assistant with "
        "StreetSmart Insurance, making a test call requested by Carlo; then wait silently. "
        "If voicemail is detected, hang up immediately without a message. Do not transfer or text.")


class TestCallRefused(ValueError):
    pass


def payload(*, target, voice_id, caller_id, max_minutes=1):
    if target != TARGET or caller_id != CALLER:
        raise TestCallRefused("exact approved Jake number and verified caller ID required")
    if not isinstance(voice_id,str) or not voice_id.strip():
        raise TestCallRefused("Jake-approved voice ID required; never choose a default")
    if type(max_minutes) is not int or max_minutes != 1:
        raise TestCallRefused("this connection proof is limited to one minute")
    return {"phone_number":target,"from":caller_id,"voice":voice_id,
            "first_sentence":OPENING,"task":TASK,"max_duration":1,"record":False,
            "voicemail":{"action":"hangup"},"wait_for_greeting":False,
            "metadata":{"purpose":"one-shot Jake connection test","redial":"disabled"}}


def dispatch_once(*, db_path, test_id, approved_day, today, hostname, body, api_key, request):
    if hostname.split('.')[0] != 'hermes-test-01':
        raise TestCallRefused("Test host only")
    if today != approved_day:
        raise TestCallRefused("test permission is limited to its approved day")
    if not test_id or not api_key:
        raise TestCallRefused("unique reviewed test ID and key required")
    expected=payload(target=TARGET,voice_id=body.get('voice'),caller_id=CALLER)
    if body != expected:
        raise TestCallRefused("payload differs from reviewed single-call contract")
    path=Path(db_path)
    if not path.is_absolute() or '/tmp/' in str(path) or path.is_symlink() or not path.parent.is_dir():
        raise TestCallRefused("existing private durable state directory required")
    if path.parent.stat().st_mode & 0o077:
        raise TestCallRefused("state directory must be private")
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS calls(test_id TEXT PRIMARY KEY, state TEXT NOT NULL, call_id TEXT)')
        try:
            conn.execute('INSERT INTO calls VALUES (?, ?, NULL)',(test_id,'claimed'))
        except sqlite3.IntegrityError:
            return {'state':'already_claimed','call_id':conn.execute('SELECT call_id FROM calls WHERE test_id=?',(test_id,)).fetchone()[0],'new_dispatch':False}
    path.chmod(0o600)
    # Claim is committed before this single POST. Ambiguous failures stay claimed.
    try:
        result=request('POST','/v1/calls',body,api_key)
    except Exception:
        with sqlite3.connect(path) as conn:conn.execute('UPDATE calls SET state=? WHERE test_id=?',('dispatch_unknown',test_id))
        return {'state':'dispatch_unknown','call_id':None,'new_dispatch':True,'retry_allowed':False}
    call_id=result.get('call_id')
    if not isinstance(call_id,str) or not call_id:
        with sqlite3.connect(path) as conn:conn.execute('UPDATE calls SET state=? WHERE test_id=?',('no_call_id_do_not_retry',test_id))
        return {'state':'no_call_id_do_not_retry','call_id':None,'new_dispatch':True,'retry_allowed':False}
    with sqlite3.connect(path) as conn:conn.execute('UPDATE calls SET state=?, call_id=? WHERE test_id=?',('accepted',call_id,test_id))
    try:
        detail=request('GET','/v1/calls/'+call_id,None,api_key)
        state=detail.get('status') or detail.get('queue_status') or 'accepted_unverified'
    except Exception:state='accepted_readback_unverified'
    return {'state':state,'call_id':call_id,'new_dispatch':True,'retry_allowed':False}


def http(method,path,body,key):
    data=json.dumps(body).encode() if body is not None else None
    req=urllib.request.Request('https://api.bland.ai'+path,data=data,method=method,
                               headers={'Authorization':key,'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=30) as response: return json.load(response)


def main():
    import argparse, base64
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--voice-id',required=True)
    parser.add_argument('--test-id',required=True)
    parser.add_argument('--approved-day',required=True,type=date.fromisoformat)
    parser.add_argument('--state-dir',required=True,type=Path)
    parser.add_argument('--execute',action='store_true')
    args=parser.parse_args()
    body=payload(target=TARGET,voice_id=args.voice_id,caller_id=CALLER)
    if not args.execute:
        print(json.dumps({'dry_run':True,'payload':body}));return
    if socket.gethostname().split('.')[0]!='hermes-test-01':raise TestCallRefused('Test host only')
    # VM identity needs separately authorized secret access; no IAM mutation here.
    meta=urllib.request.Request('http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token',headers={'Metadata-Flavor':'Google'})
    with urllib.request.urlopen(meta,timeout=10) as response: token=json.load(response)['access_token']
    req=urllib.request.Request('https://secretmanager.googleapis.com/v1/'+SECRET+':access',headers={'Authorization':'Bearer '+token})
    with urllib.request.urlopen(req,timeout=15) as response:key=base64.b64decode(json.load(response)['payload']['data']).decode().strip()
    from datetime import datetime
    from zoneinfo import ZoneInfo
    result=dispatch_once(db_path=args.state_dir/'one-shot.sqlite',test_id=args.test_id,
                         approved_day=args.approved_day,today=datetime.now(ZoneInfo('America/New_York')).date(),
                         hostname=socket.gethostname(),body=body,api_key=key,request=http)
    print(json.dumps(result))


if __name__=='__main__':main()
