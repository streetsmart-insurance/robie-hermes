"""Executable isolated Test replay, never a live call or real EZLynx note.
Run on hermes-test-01 only. Fixtures use reserved +155555501xx numbers.
"""
from datetime import datetime,timedelta,timezone
from pathlib import Path
from .phone_controls import Controls,Window,Plan,Grant,Refused
from .phone_label_runner import LabelRunner,slug

class Approvals:
    def __init__(self):self.records={}
    def resolve(self,k):return self.records.get(k)
class Directory:
    def __init__(self):self.records={}
    def resolve(self,k):return self.records.get(k)
class ReplayDispatch:
    def __init__(self):self.calls=[]
    def __call__(self,body):
        if not body['phone_number'].startswith('+155555501'):raise Refused('synthetic number only')
        call_id=f'SYN-CALL-{len(self.calls)+1}';self.calls.append((call_id,body));return {'call_id':call_id}
class ReplayNotes:
    def __init__(self):self.records={};self.latest={}
    def find(self,call_id,applicant,discussion):
        for k,r in self.records.items():
            if r['call_id']==call_id and r['applicant_id']==applicant and r['discussion_id']==discussion:return k
    def append(self,applicant,discussion,body,call_id):
        k=f'SYN-NOTE-{len(self.records)+1}';self.records[k]={'applicant_id':applicant,'discussion_id':discussion,'body':body,'call_id':call_id}
        self.latest[discussion]=k
        return {'noteId':k}
    def lookup_discussion(self,applicant,discussion):
        latest=self.latest.get(discussion,'')
        return {'LastNoteId':latest,'noteCount':1 if latest else 0,'lastModified':'2026-10-01T15:00:00+00:00'}
    def list_notes(self,*args):raise AssertionError('notes list is HTTP 405')

def replay(root,hostname):
    if hostname.split('.')[0]!='hermes-test-01':raise Refused('Test host only')
    approvals=Approvals();directory=Directory();dispatch=ReplayDispatch();notes=ReplayNotes()
    # Fixed weekday clock, no permission to call at this fixture timestamp.
    now=datetime(2026,10,1,15,tzinfo=timezone.utc)
    c=Controls(Path(root)/'replay.sqlite',Window('America/New_York',9,17),approvals,directory,dispatch,notes,lambda:now)
    runner=LabelRunner(c,environment='TEST',hostname=hostname,clock=lambda:now)
    results=[]
    for idx,(label,audience) in enumerate([('Robie Call','carrier'),('Robie Call','finance'),('Robie lead follow-up','client'),('Robie audit','client')],1):
        app=f'SYN-APP-{idx}';disc=f'SYN-DISC-{idx}';note=f'SYN-SOURCE-{idx}'
        card={'applicantId':app,'id':disc,'discussionNote':{'id':note,'modifiedAt':now.isoformat(),'noteLabels':[{'labelName':label}],'note':'untrusted source fixture'}}
        plan=Plan(f'{app}:{disc}:{note}:{slug(label)}',f'+155555501{idx:02d}',audience,f'SYN-DIR-{idx}','SYNTHETIC reviewed connection script','',app,disc,'+15555550199','SYN-VOICE',1,unrestricted_hours=label in ('Robie Call','Robie lead follow-up'))
        grant=f'SYN-GRANT-{idx}';approvals.records[grant]=Grant(grant,plan.digest(),now+timedelta(hours=1),allow_client=audience=='client')
        directory.records[plan.directory_id]={'audience':audience,'phone':plan.target}
        prepared=runner.preview(card,plan);sent=runner.run_with_test_ports(card,plan,grant)
        result=runner.complete_with_test_ports(plan,sent['call_id'],{'call_id':sent['call_id'],'ended_at':now.isoformat(),'outcome':'human_reached'})
        duplicate=runner.run_with_test_ports(card,plan,grant)
        results.append({'route':prepared['route'],'audience':audience,'synthetic_call_id':sent['call_id'],'synthetic_note_id':result['note_id'],'repeat_dispatch':duplicate['dispatch']})
    assert len(dispatch.calls)==4 and len(notes.records)==4
    return {'test_only':True,'live_calls':0,'real_notes':0,'replay_dispatches':len(dispatch.calls),'results':results}

def main():
    import argparse,socket,json
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--state-dir',type=Path,required=True);args=parser.parse_args()
    print(json.dumps(replay(args.state_dir,socket.gethostname()),sort_keys=True))
if __name__=='__main__':main()
