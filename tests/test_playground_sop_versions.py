"""Synthetic SOP copies, no agency documents or live Drive calls."""
from unittest.mock import patch
import json
from robie_job_engine.playground_sop import ingest_sops,load_index,retrieve_sop,ambiguous_sop_hits

class Drive:
 def export_text(self,file_id,mime):
  return {'old':'Driver procedure uses agent review.','new':'Driver procedure uses manager review.'}[file_id]


def metadata():
 return [{'id':'old','name':'Driver procedure','modifiedTime':'2025-01-01T00:00:00Z'},
         {'id':'new','name':'Copy of Driver procedure','modifiedTime':'2026-01-01T00:00:00Z'}]


def test_distinct_same_title_versions_survive_import_and_require_review(tmp_path):
 target=tmp_path/'index.json'
 with patch('robie_job_engine.playground_sop.collect_documents',return_value=metadata()):
  assert ingest_sops(Drive(),str(target))['count']==2
 docs=load_index(str(target));assert [d['doc_id'] for d in docs]==['new','old']
 assert all(d['version_review_required'] for d in docs)
 hits=retrieve_sop('driver procedure manager',docs,limit=2,include_ties=True)
 assert len(hits)==2 and ambiguous_sop_hits(hits)


def test_equivalent_bodies_keep_latest_modified_copy(tmp_path):
 target=tmp_path/'index.json'
 drive=Drive()
 with patch('robie_job_engine.playground_sop.collect_documents',return_value=metadata()),patch.object(drive,'export_text',return_value='Same approved fixture body.'):
  assert ingest_sops(drive,str(target))['count']==1
 docs=load_index(str(target));assert docs[0]['doc_id']=='new'
 assert 'version_review_required' not in docs[0]


def test_different_title_procedures_do_not_become_version_conflict(tmp_path):
 records=metadata();records[1]['name']='Other workflow'
 with patch('robie_job_engine.playground_sop.collect_documents',return_value=records):
  ingest_sops(Drive(),str(tmp_path/'index.json'))
 assert all('version_review_required' not in d for d in load_index(str(tmp_path/'index.json')))


def _handler_hold_case(tmp_path):
 import os
 from robie_job_engine.playground_service import handle_playground_chat
 from robie_job_engine.store import JobStore
 target=tmp_path/'index.json'
 with patch('robie_job_engine.playground_sop.collect_documents',return_value=metadata()):ingest_sops(Drive(),str(target))
 with patch.dict(os.environ,{'ROBIE_PLAYGROUND':'1','ROBIE_PLAYGROUND_SPACE_ID':'fixture'}):
  db=str(tmp_path/'jobs.db')
  reply=handle_playground_chat(db,'What is our driver procedure for manager review?',conversation_id='spaces/fixture',
   thread_id='fixture-thread',message_id='fixture-msg',requested_by='Fixture',requester_user_id='fixture',sop_docs=load_index(str(target)))[0]
 assert 'Which approved source' in reply and 'source ID new' in reply and 'source ID old' in reply
 assert 'uses manager review' not in reply
 store=JobStore(db);rows=store.list_jobs_by_status({'NEEDS_CLARIFICATION'});assert len(rows)==1
 assert set(store.get_checkpoint(rows[0]['id'],'sop_ambiguity')['source_ids'])=={'new','old'}


def test_handler_holds_imported_conflicting_versions_even_when_scores_differ():
 from pathlib import Path
 from durable_temp import durable_temporary_directory
 with durable_temporary_directory() as directory:
  _handler_hold_case(Path(directory))
