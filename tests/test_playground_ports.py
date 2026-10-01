import os
from unittest.mock import patch, Mock
import pytest
from robie_job_engine.playground_ports import PlaygroundPorts
from robie_job_engine.playground_guardrails import Proposal, classify_playground_request

class Reader:
 def __init__(self):self.calls=[]
 def policy_by_number(self,n):self.calls.append(n);return {'status':'success','data':[{'PolicyNumber':n,'ApplicantId':'26356199'}]}
 def documents_for_applicant(self,n):self.calls.append(n);return [{'id':'10','name':'Declarations.pdf'}]
class Discussions:
 def get_discussions(self,n):return [{'id':'12','title':'Existing Renewal'}]
 def get_discussion(self,n):return {'discussionId':n,'applicantId':'26356199','title':'Existing Renewal'}

def test_documents_use_explicit_applicant_and_never_write():
 r=Reader();p=PlaygroundPorts(r)
 x=p.read(Proposal(kind='lookup',applicant_id='26356199',body='find documents'))
 assert 'Declarations.pdf' in x and 'not read or compared' in x and 'document ID 10' in x
 assert r.calls==['26356199']

def test_unresolved_name_has_no_network_request():
 r=Reader();assert 'applicant ID or policy number' in PlaygroundPorts(r).read(Proposal(kind='lookup',client='Example LLC',body='find loss runs'))
 assert not r.calls

def test_policy_number_is_read_not_coverage_claim():
 r=Reader();p=PlaygroundPorts(r);x=p.read(Proposal(kind='lookup',body='lookup policy TEST-12345'))
 assert 'Coverage details still need' in x
 assert r.calls==['TEST-12345']

def test_policy_owner_mismatch_fails_closed():
 r=Reader();assert PlaygroundPorts(r).read(Proposal(kind='lookup',applicant_id='99999999',body='lookup policy TEST-12345')) is None

def test_no_policy_document_is_used_as_current_field_value():
 r=Reader();assert PlaygroundPorts(r).read(Proposal(kind='simple_edit',applicant_id='26356199',field='phone')) is None
 assert not r.calls

def test_discussion_ids_preserved():
 assert PlaygroundPorts(discussion_client=Discussions()).discussions(Proposal(kind='note',applicant_id='26356199'))==[{'id':'12','title':'Existing Renewal'}]

@pytest.mark.parametrize('applicant,enabled',[('99999999','1'),('26356199','0')])
def test_note_write_guard_precedes_network(applicant,enabled):
 writer=Mock();p=PlaygroundPorts(note_writer=writer)
 with patch.dict(os.environ,{'ROBIE_PLAYGROUND_LIVE_WRITES':enabled}):
  with pytest.raises(PermissionError):p.file_note(Proposal(kind='note',applicant_id=applicant),'Existing Renewal','Exact note')
 assert not writer.called

def test_note_receipt_without_independent_readback_is_not_success():
 w=Mock(return_value={'status':'filed','note_id':'20'});p=PlaygroundPorts(discussion_client=Discussions(),note_writer=w)
 with patch.dict(os.environ,{'ROBIE_PLAYGROUND_LIVE_WRITES':'1'}):assert p.file_note(Proposal(kind='note',applicant_id='26356199'),'Existing Renewal','Exact note')==''

def test_verified_note_id_returned():
 w=Mock(return_value={'status':'filed','note_id':'20','read_back':True});p=PlaygroundPorts(discussion_client=Discussions(),note_writer=w)
 with patch.dict(os.environ,{'ROBIE_PLAYGROUND_LIVE_WRITES':'1'}):assert p.file_note(Proposal(kind='note',applicant_id='26356199'),'Existing Renewal','Exact note')=='20'

def test_lookup_retains_original_query_for_typed_reader():
 d=classify_playground_request('Find documents for Buster Brown applicant 26356199')
 assert d.proposal.body=='Find documents for Buster Brown applicant 26356199'


def test_missing_note_text_clarifies():
 d=classify_playground_request('File a note on the existing Renewal discussion for Buster Brown')
 assert d.intent=='vague' and 'exact text' in d.question

def test_exact_note_text_retained_not_command_label():
 d=classify_playground_request('File a note on the existing Renewal discussion for Buster Brown saying Called; no answer.')
 assert d.proposal.body=='Called; no answer.'

def test_live_note_uses_guarded_destination_port():
 from contextlib import nullcontext
 from robie_job_engine.playground_execute import default_apply
 proposal=Proposal(kind='note',applicant_id='26356199',discussion_title='Renewal',body='Called; no answer.',new_value='note on Renewal')
 with patch.dict(os.environ,{'ROBIE_PLAYGROUND_LIVE_WRITES':'1'}), patch('robie_job_engine.playground_execute.with_ezlynx_lock',return_value=nullcontext()), patch('robie_job_engine.playground_ports.runtime_ports') as ports:
  ports.return_value.file_note.return_value='10'
  assert default_apply(proposal).applied
  ports.return_value.file_note.assert_called_once_with(proposal,'Renewal','Called; no answer.')
  ports.return_value.file_note.return_value=''
  assert not default_apply(proposal).applied


def test_off_space_does_not_construct_ports():
 from robie_job_engine.playground_ports import chat_port_kwargs
 with patch('robie_job_engine.playground_ports.runtime_ports') as factory:
  assert chat_port_kwargs('spaces/other')=={}
  assert not factory.called

def test_discussion_read_error_fails_closed():
 d=Mock();d.get_discussions.side_effect=RuntimeError('unavailable')
 assert PlaygroundPorts(discussion_client=d).discussions(Proposal(kind='note',applicant_id='26356199'))==[]


def test_note_confirmation_displays_destination_and_exact_body():
 from robie_job_engine.playground_reply import confirmation_reply
 reply=confirmation_reply(Proposal(kind='note',client='Buster Brown',discussion_title='Renewal',body='Called; no answer.'),job_id='fixture')
 assert 'Renewal' in reply and 'Called; no answer.' in reply and 'say go' in reply


def test_adapter_supplies_all_four_ports():
 import ast
 from pathlib import Path
 tree=ast.parse(Path('integrations/google_chat/adapter.py').read_text())
 calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='chat_port_kwargs']
 assert len(calls)==1

def test_runtime_ports_are_lazy_and_sop_index_is_explicit():
 from robie_job_engine.playground_ports import chat_port_kwargs
 with patch.dict(os.environ,{'ROBIE_PLAYGROUND':'1','ROBIE_PLAYGROUND_SPACE_ID':'fixture'}), patch('robie_job_engine.playground_sop.load_index',return_value=[{'title':'Fixture SOP'}]):
  ports=chat_port_kwargs('spaces/fixture')
  assert set(ports)=={'read','discussions','file_note','sop_docs'}
  assert ports['sop_docs']==[{'title':'Fixture SOP'}]
  assert ports['read'].__self__.read_port is None

def test_policy_lookup_needs_unambiguous_exact_match():
 r=Mock();r.policy_by_number.return_value={'status':'success','data':[{'PolicyNumber':'TEST-12345'},{'PolicyNumber':'TEST-12345'}]}
 assert PlaygroundPorts(r).read(Proposal(kind='lookup',body='lookup policy TEST-12345')) is None

def test_lookup_exception_does_not_crash_chat_or_claim_success():
 from durable_temp import durable_temporary_directory
 from robie_job_engine.playground_service import handle_playground_chat
 with durable_temporary_directory() as tmp, patch.dict(os.environ,{'ROBIE_PLAYGROUND':'1','ROBIE_PLAYGROUND_SPACE_ID':'fixture'}):
  from pathlib import Path
  replies=handle_playground_chat(str(Path(tmp)/'jobs.db'),'Find documents for Buster Brown applicant 26356199',conversation_id='spaces/fixture',thread_id='fixture',message_id='fixture',requested_by='Fixture user',requester_user_id='fixture',read=Mock(side_effect=RuntimeError('unavailable')))
 assert replies and "couldn't" in replies[0].lower()

@pytest.mark.parametrize('selector,expected',[('',None),('live','PRODUCTION')])
def test_read_and_discussion_source_selection_agree_without_env_change(selector,expected):
 from robie_job_engine.playground_ports import PlaygroundPorts
 with patch.dict(os.environ,{'ROBIE_ENV':'TEST','ROBIE_EZLYNX_DISCUSSION_API':selector}), patch('robie_job_engine.ezlynx_api.load_ezlynx_api_config') as config, patch('robie_job_engine.ezlynx_api.EzlynxApiClient') as client:
  port=PlaygroundPorts()._read_port()
  config.assert_called_once_with(environment=expected)
  client.assert_called_once_with(config.return_value)
  assert os.environ['ROBIE_ENV']=='TEST'


def test_bad_source_selector_fails_before_credentials_or_network():
 with patch.dict(os.environ,{'ROBIE_EZLYNX_DISCUSSION_API':'production-guess'}), patch('robie_job_engine.ezlynx_api.load_ezlynx_api_config') as config:
  with pytest.raises(RuntimeError):PlaygroundPorts()._read_port()
  config.assert_not_called()

@pytest.mark.parametrize('record',[
 {'discussionId':'12','applicantId':'99999999','title':'Existing Renewal'},
 {'discussionId':'12','title':'Existing Renewal'},
 {'discussionId':'13','applicantId':'26356199','title':'Existing Renewal'},
 {'discussionId':'12','applicantId':'26356199','title':'Changed title'},
])
def test_note_destination_mismatch_never_calls_writer(record):
 d=Mock();d.get_discussions.return_value=[{'id':'12','title':'Existing Renewal'}];d.get_discussion.return_value=record
 w=Mock();ports=PlaygroundPorts(discussion_client=d,note_writer=w)
 with patch.dict(os.environ,{'ROBIE_PLAYGROUND_LIVE_WRITES':'1'}):
  with pytest.raises(ValueError):ports.file_note(Proposal(kind='note',applicant_id='26356199'),'Existing Renewal','Exact note')
 w.assert_not_called()

@pytest.mark.parametrize('rows',[
 [{'id':'12','title':'Existing Renewal extended'}],
 [{'id':'12','title':'Existing Renewal'},{'id':'13','title':'Existing Renewal'}],
])
def test_note_substring_or_duplicate_title_never_calls_writer(rows):
 d=Mock();d.get_discussions.return_value=rows;w=Mock()
 with patch.dict(os.environ,{'ROBIE_PLAYGROUND_LIVE_WRITES':'1'}):
  with pytest.raises(ValueError):PlaygroundPorts(discussion_client=d,note_writer=w).file_note(Proposal(kind='note',applicant_id='26356199'),'Existing Renewal','Exact note')
 w.assert_not_called();d.get_discussion.assert_not_called()

def test_pinned_writer_cannot_redirect_or_rewrite():
 d=Mock();d.get_discussions.return_value=[{'id':'12','title':'Existing Renewal'}];d.get_discussion.return_value={'discussionId':'12','applicantId':'26356199','title':'Existing Renewal'}
 def writer(applicant,body,**kwargs):
  pinned=kwargs['discussion_client']
  assert pinned.get_discussions(applicant)==[d.get_discussion.return_value]
  with pytest.raises(ValueError):pinned.append_note('13',body)
  with pytest.raises(ValueError):pinned.append_note('12','different text')
  pinned.append_note('12',body)
  return {'status':'filed','note_id':'20','read_back':True}
 with patch.dict(os.environ,{'ROBIE_PLAYGROUND_LIVE_WRITES':'1'}):
  assert PlaygroundPorts(discussion_client=d,note_writer=writer).file_note(Proposal(kind='note',applicant_id='26356199'),'Existing Renewal','Exact note')=='20'
 d.append_note.assert_called_once_with('12','Exact note',note_type='Note')

def test_declarations_names_are_candidates_not_verified_policy_docs():
 r=Mock();r.documents_for_applicant.return_value=[{'id':'1','name':'Declarations Page.pdf'},{'id':'2','name':'NOC.pdf'}]
 answer=PlaygroundPorts(r).read(Proposal(kind='lookup',applicant_id='26356199',body='need the dec page'))
 assert 'Name-matched candidates' in answer and 'document ID 1' in answer and 'NOC.pdf' not in answer
 assert 'not read or compared' in answer


def test_no_matching_document_is_not_a_claim_of_absence():
 r=Mock();r.documents_for_applicant.return_value=[{'id':'1','name':'NOC.pdf'}]
 answer=PlaygroundPorts(r).read(Proposal(kind='lookup',applicant_id='26356199',body='find loss runs'))
 assert "doesn't prove the document is absent" in answer and 'Which policy' in answer


def test_document_list_exposes_truncation_and_ignores_missing_ids():
 r=Mock();r.documents_for_applicant.return_value=[{'id':str(n),'name':f'Doc {n}.pdf'} for n in range(1,8)]+[{'name':'No ID.pdf'}]
 answer=PlaygroundPorts(r).read(Proposal(kind='lookup',applicant_id='26356199',body='find documents'))
 assert 'Showing 5 of 7' in answer and 'No ID.pdf' not in answer and 'document ID 6' not in answer


def test_loss_run_name_filter_does_not_claim_coverage_read():
 r=Mock();r.documents_for_applicant.return_value=[{'id':'7','name':'Loss-Runs.pdf'},{'id':'8','name':'Loss notice.pdf'}]
 answer=PlaygroundPorts(r).read(Proposal(kind='lookup',applicant_id='26356199',body='find loss runs'))
 assert 'document ID 7' in answer and 'Loss notice' not in answer and 'Name-matched candidates' in answer
