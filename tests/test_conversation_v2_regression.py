"""V2 independent findings converted into blocked-egress regression checks."""
from unittest.mock import patch
from test_conversation_reply import chat, no_network, event, execute, ACTOR
from test_round10_reply_lifecycle import SPACE, THREAD, _adapter_module
from test_conversation_policy import owned_job, complete
from robie_job_engine.conversation_policy import classify, INFORMATIONAL, CLARIFY, status_answer
from robie_job_engine.models import JobStatus, VerificationEvidence
from robie_job_engine.store import JobStore


def test_durable_hitl_value_resumes_before_ordinary_classification(chat):
    module = _adapter_module()
    chat._reply_in_existing_thread = {}
    store, job = owned_job(chat)
    store.transition(job['id'], JobStatus.RUNNING, expected={JobStatus.PENDING})
    queue = chat._chat_queue
    state = {'awaiting': 'human_input', 'field_name': 'year', 'accepts_value': True}
    queue.link_conversation_job(conversation_id=SPACE, job_id=job['id'], message_id='original', event_id='original', interaction_state=state)
    queue.park_direct_human_input(conversation_id=SPACE, job_id=job['id'], interaction_state=state, error='MISSING_REQUIRED_FIELD: year')
    inbound = event(text='2019')
    assert classify(inbound.text) == CLARIFY
    async def build(*args, **kwargs): return inbound
    resumed_worker=[]
    async def worker(_event, ident, *_):resumed_worker.append(ident)
    with patch.object(chat, '_resume_direct_generic_chat_job', worker), patch.object(chat, '_build_message_event', build), patch.object(chat, '_chat_job_attachment_kwargs', return_value={'expected_attachment_count': 0}), patch.object(queue, 'resume_human_input', wraps=queue.resume_human_input) as resume, patch.dict('os.environ', {'ROBIE_ENV':'TEST','GOOGLE_CHAT_ALLOWED_USERS': ACTOR}):
        execute(chat._dispatch_message(inbound.raw_message, {'space': {'name':SPACE,'type':'DIRECT_MESSAGE'}}, routing_attributes={'robie_env':'test'}))
        resume.assert_called_once()
    updated=store.get_job(job['id'])
    assert updated['status'] == 'RUNNING'
    assert updated['payload']['human_input_values']['year'] == '2019'
    assert resumed_worker == [job['id']]
    with store.connect() as conn: assert conn.execute('SELECT count(*) FROM jobs').fetchone()[0] == 1


def test_unambiguous_operational_status_never_enters_model_lane(chat):
    from robie_job_engine import turn_finalization as guard, chat_turn_control
    inbound = event(text='Is my policy renewed?')
    from robie_job_engine.conversation_reply import advance
    advance(chat, inbound)
    assert classify(inbound.text) == 'status'
    seen = []
    async def model(_event): seen.append(True)
    def tool_stub(): pass
    tool_stub._robie_conversation_tool_guard = True
    from types import SimpleNamespace
    with patch.object(chat, 'handle_message', model, create=True), patch.object(chat, '_chat_job_attachment_kwargs', return_value={'expected_attachment_count':0}), patch.object(guard, 'install_tool_call_text_guard'), patch.object(guard, '_agent_class', return_value=SimpleNamespace(_execute_tool_calls=tool_stub)), patch.object(chat_turn_control, 'session_is_busy', return_value=False), patch.dict('os.environ', {'ROBIE_ENV':'TEST','GOOGLE_CHAT_ALLOWED_USERS':ACTOR}):
        execute(chat._handle_conversation_only(inbound))
    assert seen == []
    assert 'not reporting the work as complete' in chat._chat_api.messages.calls[0]['body']['text']


def test_mismatched_receipt_with_valid_digest_never_reports_completion(chat):
    store, job = owned_job(chat)
    store.add_evidence(job['id'], True, VerificationEvidence('independent_readback','synthetic-destination',{'note_id':'expected'},{'note_id':'wrong'},True,'2026-10-03T16:00:00Z','fake://notes/wrong'))
    complete(store, job)
    answer = status_answer(chat._db, event(text='Status '+job['id']), environment='test')
    assert 'Not verified' in answer
    assert 'Recorded complete' not in answer

import pytest
from datetime import datetime, timedelta, timezone
from robie_job_engine.store import utc_now
from robie_job_engine.conversation_policy import STATUS, NO_STATUS
from test_round10_reply_lifecycle import OTHER

@pytest.mark.parametrize('question',[
    'Is my policy renewed?', 'Is the upload complete?', 'Was the document uploaded?',
    'Are the emails sent?', 'Has my client been emailed?', 'Is the payment processed?',
    'Is the endorsement issued?', 'Was the renewal bound?', 'Are my documents filed?',
    'Is the request verified?', 'Is my note saved?', 'Is the job done?',
    'Can you tell me if my policy is renewed?', 'What is the result of my renewal?',
    'What was the outcome of the upload?',
    'Is the document for '+('a long named client '*20)+'uploaded?',
])
def test_outcome_question_forms_route_to_owned_status(question):
    assert classify(question)==STATUS


def parked(chat, *, field='year', accepts=True):
    store,job=owned_job(chat)
    store.transition(job['id'],JobStatus.RUNNING,expected={JobStatus.PENDING})
    state={'awaiting':'human_input','field_name':field,'accepts_value':accepts}
    queue=chat._chat_queue
    queue.link_conversation_job(conversation_id=SPACE,job_id=job['id'],message_id='original',event_id='original',interaction_state=state)
    queue.park_direct_human_input(conversation_id=SPACE,job_id=job['id'],interaction_state=state,error='MISSING_REQUIRED_FIELD')
    chat._reply_in_existing_thread={}
    return store,job,queue


def dispatch_pending(chat,queue,inbound,*,worker=None):
    if inbound.source.thread_id is None:inbound.raw_message.pop('thread',None)
    async def build(*_,**__):return inbound
    async def no_worker(*_,**__):raise AssertionError('Unexpected worker start')
    with patch.object(chat,'_resume_direct_generic_chat_job',worker or no_worker),patch.object(chat,'_build_message_event',build),patch.object(chat,'_chat_job_attachment_kwargs',return_value={'expected_attachment_count':0}),patch.object(queue,'resume_human_input',wraps=queue.resume_human_input) as resume,patch.dict('os.environ',{'ROBIE_ENV':'TEST','GOOGLE_CHAT_ALLOWED_USERS':ACTOR+',foreign@streetsmart.insurance'}):
        execute(chat._dispatch_message(inbound.raw_message,{'space':{'name':SPACE,'type':'DIRECT_MESSAGE' if inbound.source.chat_type=='dm' else 'SPACE'}},routing_attributes={'robie_env':'test'}))
        return resume.call_count

@pytest.mark.parametrize('question',['Status','Is my policy renewed?','Explain it and email the client.'])
def test_question_while_pending_preserves_bind_and_never_becomes_field_value(chat,question):
    store,job,queue=parked(chat)
    assert dispatch_pending(chat,queue,event(text=question))==0
    assert store.get_job(job['id'])['status']=='AWAITING_HUMAN_INPUT'
    assert not store.get_job(job['id'])['payload'].get('human_input_values')
    assert queue.active_conversation_job(SPACE)['job_id']==job['id']
    assert chat._chat_api.messages.calls

@pytest.mark.parametrize('chat_type',['dm','group'])
@pytest.mark.parametrize('foreign',['actor','thread','environment','missing_owner','bot'])
def test_pending_input_cannot_resume_or_rebind_foreign_owner(chat,foreign,chat_type):
    store,job,queue=parked(chat)
    inbound=event(text='2019')
    if foreign=='actor':inbound=event(text='2019',actor='foreign@streetsmart.insurance')
    elif foreign=='thread':inbound=event(text='2019',thread=OTHER)
    elif foreign=='environment':store.checkpoint(job['id'],'chat_request_owner',{'actor':ACTOR,'environment':'prod'})
    elif foreign=='missing_owner':
        with store.connect() as conn:conn.execute("DELETE FROM checkpoints WHERE kind='chat_request_owner'")
    else:inbound.raw_message['sender']['type']='BOT'
    inbound.source.chat_type=chat_type
    assert dispatch_pending(chat,queue,inbound)==0
    assert store.get_job(job['id'])['status']=='AWAITING_HUMAN_INPUT'
    assert queue.active_conversation_job(SPACE)['job_id']==job['id']
    assert not store.get_job(job['id'])['payload'].get('human_input_values')
    assert store.get_checkpoint(job['id'],'chat_thread')['thread_name']==THREAD

@pytest.mark.parametrize('field,value',[('FEIN','not-a-fein'),('effective_date','not-a-date')])
def test_typed_invalid_value_uses_existing_missing_field_prompt(chat,field,value):
    store,job,queue=parked(chat,field=field)
    assert dispatch_pending(chat,queue,event(text=value))==0
    assert store.get_job(job['id'])['status']=='AWAITING_HUMAN_INPUT'
    assert 'does not look like a valid' in chat._chat_api.messages.calls[0]['body']['text']

@pytest.mark.parametrize('thread',[THREAD,None])
def test_owned_value_resumes_exact_job_from_side_or_main_dm(chat,thread):
    store,job,queue=parked(chat)
    resumed=[]
    async def worker(_event,ident,*_):resumed.append(ident)
    assert dispatch_pending(chat,queue,event(text='2019',thread=thread),worker=worker)==1
    assert resumed==[job['id']]
    assert store.get_job(job['id'])['payload']['human_input_values']['year']=='2019'

@pytest.mark.parametrize('bad',[
    'mismatch','unknown_outcome','before_job','future','unparseable_time',
    'wrong_target','missing_target','job_id_target','prior_action','prior_perform',
])
def test_hash_valid_receipt_uses_existing_completion_contract(chat,bad):
    store,job=owned_job(chat)
    locator='fake://notes/1';expected={'note_id':'1'};observed={'note_id':'1'}
    captured=utc_now()
    store.checkpoint(job['id'],'action',{'destination':{'locator':locator}})
    if bad=='mismatch':observed={'note_id':'wrong'}
    elif bad=='unknown_outcome':observed['unknown_outcome']=True
    elif bad=='before_job':captured='2001-01-01T00:00:00Z'
    elif bad=='future':captured=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
    elif bad=='unparseable_time':captured='not-a-timestamp'
    elif bad=='wrong_target':store.checkpoint(job['id'],'action',{'destination':{'locator':'fake://notes/other'}})
    elif bad=='missing_target':
        with store.connect() as conn:conn.execute("DELETE FROM checkpoints WHERE kind='action'")
    elif bad=='job_id_target':
        locator=job['id'];store.checkpoint(job['id'],'action',{'destination':{'locator':locator}})
    store.add_evidence(job['id'],True,VerificationEvidence('independent_readback','synthetic-destination',expected,observed,True,captured,locator))
    if bad=='prior_action':store.checkpoint(job['id'],'action',{'destination':{'locator':locator}})
    elif bad=='prior_perform':
        with store.connect() as conn:
            conn.execute("INSERT INTO attempts(job_id,phase,attempt_number,outcome,detail_json,created_at) VALUES(?,?,?,?,?,?)",(job['id'],'perform',1,'fixture-only','{}',utc_now()))
    complete(store,job)
    before=store.get_job(job['id'])
    answer=status_answer(chat._db,event(text='Status '+job['id']),environment='test')
    assert 'Not verified' in answer and 'Recorded complete' not in answer
    assert store.get_job(job['id'])==before

@pytest.mark.parametrize('field,value',[
    ('FEIN','12-3456789'),('NAICS','541611'),('effective_date','10/04/2026'),
    ('operator_response','A $1200000; B $120000; C $500000; D $500000; F $10000'),
])
def test_existing_typed_and_coverage_reply_shapes_resume_same_job(chat,field,value):
    store,job,queue=parked(chat,field=field)
    resumed=[]
    async def worker(_event,ident,*_):resumed.append(ident)
    assert dispatch_pending(chat,queue,event(text=value),worker=worker)==1
    assert resumed==[job['id']]
    assert store.get_job(job['id'])['payload']['human_input_values'][field]==value

@pytest.mark.parametrize('status',['FAILED','UNVERIFIED','COMPLETE','RUNNING'])
def test_stale_pending_correlation_is_released_before_generic_question_routing(chat,status):
    store,job,queue=parked(chat)
    with store.connect() as conn:conn.execute('UPDATE jobs SET status=? WHERE id=?',(status,job['id']))
    # RUNNING dead binds are caught by the existing resume exception path;
    # terminal stale binds are released before resume is attempted.
    assert dispatch_pending(chat,queue,event(text='2019'))==(1 if status=='RUNNING' else 0)
    assert store.get_job(job['id'])['status']==status
    assert queue.active_conversation_job(SPACE) is None
    assert 'Are you asking for an explanation' in chat._chat_api.messages.calls[0]['body']['text']
    with store.connect() as conn:assert conn.execute('SELECT count(*) FROM jobs').fetchone()[0]==1


def test_group_thread_pending_reply_requires_exact_owner_and_resumes(chat):
    store,job,queue=parked(chat)
    inbound=event(text='2019');inbound.source.chat_type='group'
    resumed=[]
    async def worker(_event,ident,*_):resumed.append(ident)
    assert dispatch_pending(chat,queue,inbound,worker=worker)==1
    assert resumed==[job['id']]


def test_unthreaded_group_pending_reply_refuses_resume_or_rebind(chat):
    store,job,queue=parked(chat)
    inbound=event(text='2019');inbound.source.chat_type='group';inbound.source.thread_id=None
    assert dispatch_pending(chat,queue,inbound)==0
    assert store.get_job(job['id'])['status']=='AWAITING_HUMAN_INPUT'
    assert not store.get_job(job['id'])['payload'].get('human_input_values')
    assert queue.active_conversation_job(SPACE)['job_id']==job['id']
    assert store.get_checkpoint(job['id'],'chat_thread')['thread_name']==THREAD
    assert not chat._chat_api.messages.calls
    with store.connect() as conn:assert conn.execute('SELECT count(*) FROM jobs').fetchone()[0]==1
