"""Only the original requester may approve their exact threaded proposal."""
from pathlib import Path
from unittest import mock
import os
from durable_temp import durable_temporary_directory
from tests.test_playground import ADDRESS, SPACE, WHEN, Reader, Writer, _env
from robie_job_engine.playground_service import handle_playground_chat
from robie_job_engine.playground_voice import line

def test_approval_identity_and_exact_thread():
    with durable_temporary_directory() as tmp, mock.patch.dict(os.environ, _env(), clear=False), mock.patch('robie_job_engine.ezlynx_write_scope.ALLOWED_EZLYNX_WRITE_APPLICANT_IDS',frozenset({'26356199'})):
        db=str(Path(tmp)/'jobs.db'); reader=Reader(); writer=Writer(reader)
        def turn(text,thread,user,message):
            return handle_playground_chat(db,text,conversation_id=SPACE,thread_id=thread,requested_by=user,message_id=message,now=WHEN,apply=writer,read=reader)
        turn(ADDRESS,'t1','owner','ask1')
        turn('go','t1','other','bad-user')
        turn('go','','owner','missing-thread')
        turn('go','t2','owner','wrong-thread')
        turn('what can you do','t1','other','other-question')
        assert writer.calls==[]
        turn('go','t1','owner','good')
        assert len(writer.calls)==1
        turn('go','t1','owner','replay')
        assert len(writer.calls)==1

def test_blank_pending_thread_cannot_be_approved():
    with durable_temporary_directory() as tmp, mock.patch.dict(os.environ,_env(),clear=False), mock.patch('robie_job_engine.ezlynx_write_scope.ALLOWED_EZLYNX_WRITE_APPLICANT_IDS',frozenset({'26356199'})):
        db=str(Path(tmp)/'jobs.db');reader=Reader();writer=Writer(reader)
        for text,tid,mid in [(ADDRESS,'','ask'),('go','t1','reply'),('go','','blank')]:
            handle_playground_chat(db,text,conversation_id=SPACE,thread_id=tid,message_id=mid,requested_by='owner',now=WHEN,apply=writer,read=reader)
        assert writer.calls==[]

def test_menu_does_not_claim_unconnected_fields():
    menu=line('help_menu')
    assert 'staff to make' in menu
    assert '- Change an address' not in menu
    assert '- Add a driver' not in menu
