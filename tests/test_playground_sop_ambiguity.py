from pathlib import Path
from unittest.mock import patch
import os
from robie_job_engine.playground_sop import SopHit, ambiguous_sop_hits, retrieve_sop
from robie_job_engine.playground_service import handle_playground_chat
from robie_job_engine.store import JobStore
from durable_temp import durable_temporary_directory


def test_citation_preserves_source_id_and_modified_version():
 hit=SopHit('Fixture SOP','core','Text','fixture-1','2026-09-30T00:00:00Z')
 assert 'source ID fixture-1' in hit.citation and 'modified 2026-09-30' in hit.citation


def test_tied_different_excerpts_require_review():
 hits=retrieve_sop('driver procedure',[
 {'doc_id':'1','title':'Driver procedure','text':'Driver requires agent review.'},
 {'doc_id':'2','title':'Driver procedure','text':'Driver requires manager review.'}],limit=2)
 assert ambiguous_sop_hits(hits)


def test_identical_excerpt_copies_are_not_ambiguity():
 assert not ambiguous_sop_hits([SopHit('A','core','same text','1',match_score=2),SopHit('B','core','Same  text','2',match_score=2)])


def test_unequal_rank_is_not_claimed_as_semantic_conflict_detection():
 assert not ambiguous_sop_hits([SopHit('A','core','A','1',match_score=2),SopHit('B','core','B','2',match_score=1)])
 assert not ambiguous_sop_hits([])


def test_handler_holds_tied_sop_and_records_source_ids_without_answering():
 with durable_temporary_directory() as tmp,patch.dict(os.environ,{'ROBIE_PLAYGROUND':'1','ROBIE_PLAYGROUND_SPACE_ID':'fixture'}):
  db=str(Path(tmp)/'jobs.db')
  replies=handle_playground_chat(db,'How do we handle driver procedure?',conversation_id='spaces/fixture',thread_id='fixture-thread',message_id='fixture-msg',requested_by='Fixture',requester_user_id='fixture',sop_docs=[
   {'doc_id':'fixture-1','title':'Driver procedure','text':'Driver requires agent review.'},
   {'doc_id':'fixture-2','title':'Driver procedure','text':'Driver requires manager review.'}])
  assert 'Which approved source' in replies[0] and 'source ID fixture-1' in replies[0]
  store=JobStore(db);rows=store.list_jobs_by_status({'NEEDS_CLARIFICATION'});assert len(rows)==1
  record=store.get_checkpoint(rows[0]['id'],'sop_ambiguity');assert record['source_ids']==['fixture-1','fixture-2']
  assert 'Driver requires agent review.' not in replies[0]
