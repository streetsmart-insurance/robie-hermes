"""Label/lead route to phone Controls. Test-only; no live transport mode.

Source cards are untrusted data. Labels request preparation, not approval.
Scripts/recipient choices come from a separately reviewed bound Plan. This
runner never turns note prose into a tool instruction or an approval.
"""
import re
from datetime import datetime
from .phone_controls import Plan,Refused

OUTREACH=('Robie client outreach','Robie cancellation','Robie audit','Robie returned mail','Robie e-sign','Robie additional info','Robie recommendations','Robie unresponsive','Robie renewal reach-out')
def slug(s):return re.sub('[^a-z0-9]','',str(s).lower())

def route(card):
    note=card.get('discussionNote')
    if not isinstance(note,dict):raise Refused('source note required')
    labels=note.get('noteLabels',[])
    if not isinstance(labels,list):raise Refused('source org labels required')
    names=[v.get('labelName','') if isinstance(v,dict) else v for v in labels]
    tokens={slug(n) for n in names}
    outreach={slug(n):n for n in OUTREACH}
    matches=sorted(tokens.intersection(outreach))
    if len(matches)>1:raise Refused('conflicting outreach labels')
    if matches:return 'client_outreach',outreach[matches[0]]
    if 'robieleadfollowup' in tokens:return 'client_followup','Robie lead follow-up'
    if 'robiecall' in tokens:return 'robie_call','Robie Call'
    raise Refused('no supported Robie org label')

class LabelRunner:
    def __init__(self,controls,*,environment,hostname,clock,max_age_hours=48):
        if environment!='TEST' or hostname.split('.')[0]!='hermes-test-01':raise Refused('Test runner only')
        if not 0<max_age_hours<=48:raise Refused('bounded label freshness required')
        self.controls=controls;self.clock=clock;self.max_age_hours=max_age_hours
    def prepare(self,card,plan):
        kind,label=route(card);note=card['discussionNote']
        note_id=str(note.get('id') or note.get('noteId') or '')
        discussion=str(card.get('id') or card.get('discussionId') or '')
        applicant=str(card.get('applicantId') or '')
        modified=note.get('modifiedAt') or note.get('createdAt')
        if not note_id or not modified:raise Refused('source note id and timestamp required')
        timestamp=datetime.fromisoformat(modified.replace('Z','+00:00'));now=self.clock()
        if timestamp.tzinfo is None or not 0<=(now-timestamp).total_seconds()<=self.max_age_hours*3600:raise Refused('stale or future note')
        if applicant!=plan.applicant_id or discussion!=plan.discussion_id:raise Refused('source destination differs from plan')
        # Stable note+route campaign identity prevents repeat watcher events.
        identity=f'{applicant}:{discussion}:{note_id}:{slug(label)}'
        if plan.campaign!=identity:raise Refused('campaign must bind source note and route')
        if kind in ('client_followup','client_outreach') and plan.audience!='client':raise Refused('client label requires reviewed client route')
        if kind=='robie_call' and plan.audience not in ('carrier','finance','client'):raise Refused('unsupported call audience')
        # No notes read as instructions. No arbitrary phone overrides.
        return {'route':kind,'label':label,'plan_digest':plan.digest(),'approval_required':True,'campaign':identity}
    def preview(self,card,plan):
        result=self.prepare(card,plan)
        return dict(result,status='prepared_not_dispatched',script=plan.script,voicemail_script=plan.voicemail_script,target=plan.target)
    def run_with_test_ports(self,card,plan,grant_id):
        self.prepare(card,plan)
        # This class has no live adapter factory. The caller is responsible
        # for supplying synthetic Test ports; a label grants no permission.
        return self.controls.start(plan,grant_id)
    def complete_with_test_ports(self,plan,call_id,detail):
        return self.controls.finish(plan,call_id,detail)
