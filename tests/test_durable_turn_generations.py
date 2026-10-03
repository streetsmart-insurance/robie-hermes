"""Synthetic generation/receipt lifetimes; no cloud, Chat or EZLynx requests."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import Context, copy_context
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from robie_job_engine import turn_finalization as turn
from robie_job_engine.store import JobStore


@pytest.fixture
def state(tmp_path):
    store = JobStore(tmp_path/'jobs.sqlite')
    job = store.create_job('hermes.google_chat_task', {'text': 'synthetic'})['id']
    yield store, job
    turn.finish_model_generation(job)
    turn._GENERATION_CONTEXT.set(None)


def test_successive_generations_cannot_reuse_confirmed_write(state):
    store, job = state
    first = turn.begin_model_generation(job, store=store)
    assert turn.record_turn_write(store, job, note_id='note-1')
    assert turn.confirmed_write_this_turn(store, job)
    turn.finish_model_generation(job)
    second = turn.begin_model_generation(job, store=store)
    assert first != second
    assert not turn.confirmed_write_this_turn(store, job)
    assert turn.suppress_model_reply(store, job, 'I added the note.')
    assert store.get_checkpoint(job, 'turn_write_log')['generation'] == first


def test_same_generation_retries_and_duplicate_completion_keep_receipt(state):
    store, job = state
    token = turn.begin_model_generation(job, store=store)
    assert turn.record_turn_write(store, job, note_id='note-1')
    original = store.get_checkpoint(job, 'turn_write_log')
    assert turn.record_turn_write(store, job, note_id='note-1')
    turn.note_generation_delivered(job)
    turn.note_generation_delivered(job)
    assert store.get_checkpoint(job, 'turn_write_log') == original
    assert turn.current_model_generation(job) == ''
    assert not turn.confirmed_write_this_turn(store, job)
    assert turn.resume_model_generation(store, job, token)
    assert turn.confirmed_write_this_turn(store, job)
    assert not turn.model_generation_is_running(job)


def test_restart_requires_explicit_matching_token_and_never_executes_write(state):
    store, job = state
    token = turn.begin_model_generation(job, store=store)
    turn.record_turn_write(store, job, note_id='note-1')
    code = '''
import json,sys
from robie_job_engine.store import JobStore
from robie_job_engine import turn_finalization as t
s=JobStore(sys.argv[1]); job=sys.argv[2]; token=sys.argv[3]
assert not t.confirmed_write_this_turn(s,job)
assert not t.resume_model_generation(s,job,'wrong-token')
assert t.resume_model_generation(s,job,token)
assert t.confirmed_write_this_turn(s,job)
assert s.get_job(job)['attempt_count']==0
print(json.dumps(s.get_checkpoint(job,'turn_write_log')))
'''
    result = subprocess.run([sys.executable, '-c', code, store.path, job, token],
                            cwd=Path(__file__).resolve().parents[1],
                            env={**os.environ, 'PYTHONPATH': '.'}, check=True,
                            capture_output=True, text=True, timeout=15)
    assert json.loads(result.stdout)['generation'] == token
    next_token = turn.begin_model_generation(job, store=store)
    assert not turn.resume_model_generation(store, job, token)
    assert turn.current_model_generation(job) == next_token


def test_new_generation_after_process_state_loss_does_not_adopt_old_receipt(state):
    store, job = state
    first = turn.begin_model_generation(job, store=store)
    turn.record_turn_write(store, job, note_id='note-1')
    def fresh_context():
        assert turn.current_model_generation(job) == ''
        assert not turn.confirmed_write_this_turn(store, job)
        token = turn.begin_model_generation(job, store=JobStore(store.path))
        assert token != first
        assert not turn.confirmed_write_this_turn(store, job)
        turn.finish_model_generation(job)
    Context().run(fresh_context)


def test_late_callback_and_write_cannot_finish_or_overwrite_successor(state):
    store, job = state
    first = turn.begin_model_generation(job, store=store)
    old = copy_context()
    second = turn.begin_model_generation(job, store=store)
    turn.record_turn_write(store, job, note_id='new-note')
    old.run(turn.finish_model_generation, job)
    assert not old.run(turn.record_turn_write, store, job, note_id='late-old-note')
    assert old.run(turn.suppress_model_reply, store, job, 'Which discussion?')
    assert turn.model_generation_is_running(job)
    assert store.get_checkpoint(job, 'model_generation')['generation'] == second
    assert store.get_checkpoint(job, 'turn_write_log')['note_id'] == 'new-note'
    assert not turn.resume_model_generation(store, job, first)


def test_concurrent_begins_have_unique_tokens_and_only_current_can_record(state):
    store, job = state
    gate = threading.Barrier(8)
    def worker(_):
        token = turn.begin_model_generation(job, store=JobStore(store.path))
        gate.wait(timeout=10)
        return token, turn.record_turn_write(store, job, note_id='note-'+token)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(8)))
    assert len({token for token, _ in results}) == 8
    assert sum(accepted for _, accepted in results) == 1
    current = store.get_checkpoint(job, 'model_generation')['generation']
    assert store.get_checkpoint(job, 'turn_write_log')['generation'] == current
    assert Context().run(turn.resume_model_generation, store, job, current)


@pytest.mark.parametrize('log', [
    {'confirmed': True, 'note_id': 'legacy', 'generation': 1},
    {'confirmed': True, 'note_id': 'legacy'},
    {'confirmed': True, 'note_id': 'legacy', 'generation': 0},
])
def test_legacy_or_missing_generation_receipt_is_not_authority(state, log):
    store, job = state
    store.checkpoint(job, 'turn_write_log', log)
    assert not turn.confirmed_write_this_turn(store, job)
    turn.begin_model_generation(job, store=store)
    assert not turn.confirmed_write_this_turn(store, job)


def test_approval_or_old_question_does_not_become_a_new_generation_write(state):
    store, job = state
    first = turn.begin_model_generation(job, store=store)
    approval = {'approved': True, 'generation': first, 'thread_id': 'original-thread'}
    store.checkpoint(job, 'confirmation', approval)
    store.checkpoint(job, 'chat_outcome_sent', {'text': 'Want me to add it again?', 'generation': first})
    assert turn.engine_question_already_sent(store, job)
    second = turn.begin_model_generation(job, store=store)
    assert second != first
    assert not turn.engine_question_already_sent(store, job)
    assert not turn.confirmed_write_this_turn(store, job)
    assert store.get_checkpoint(job, 'confirmation') == approval
    store.checkpoint(job, 'chat_outcome_sent', {'text': 'Which discussion?', 'generation': second})
    assert turn.engine_question_already_sent(store, job)


def test_async_context_keeps_original_token_across_successor_begin(state):
    store, job = state
    async def scenario():
        old = turn.begin_model_generation(job, store=store)
        ready = asyncio.Event()
        async def callback():
            await ready.wait()
            assert turn.current_model_generation(job) == old
            assert not await asyncio.to_thread(turn.record_turn_write, store, job, note_id='late')
            turn.note_generation_delivered(job)
        task = asyncio.create_task(callback())
        new = turn.begin_model_generation(job, store=store)
        ready.set()
        await task
        assert turn.current_model_generation(job) == new
        assert turn.model_generation_is_running(job)
    asyncio.run(scenario())


def test_persistence_failure_prevents_generation_admission(state, monkeypatch):
    store, job = state
    def broken():
        raise OSError('synthetic disk unavailable')
    monkeypatch.setattr(store, 'transaction', broken)
    with pytest.raises(OSError):
        turn.begin_model_generation(job, store=store)
    assert not turn.current_model_generation(job)
    assert not turn.model_generation_is_running(job)


def test_dispatch_scope_clears_listener_identity_but_keeps_child_identity(state):
    store, job = state
    assert turn.current_model_generation(job) == ''
    with turn.isolated_model_generation_context():
        token = turn.begin_model_generation(job, store=store)
        child = copy_context()
    assert turn.current_model_generation(job) == ''
    assert child.run(turn.current_model_generation, job) == token
    child.run(turn.finish_model_generation, job)


def test_retained_context_job_beats_mutable_environment_for_late_receipt(state, monkeypatch):
    store, job = state
    turn.begin_model_generation(job, store=store)
    monkeypatch.setenv('ROBIE_CURRENT_JOB_ID', 'different-job')
    assert turn.current_model_job_id() == job
    assert turn.record_turn_write(store, job, note_id='actual-note')


def test_duplicate_recovery_after_epoch_resume_does_not_replay_write_or_delivery():
    from unittest.mock import patch
    from test_chat_reply_recovery import ReplyRecoveryTests
    fixture = ReplyRecoveryTests(methodName='runTest')
    fixture.setUp()
    try:
        token = turn.begin_model_generation(fixture.job, store=fixture.store)
        assert turn.record_turn_write(fixture.store, fixture.job, note_id='verified-note')
        before = fixture.store.get_checkpoint(fixture.job, 'turn_write_log')
        fixture.failed()
        turn.finish_model_generation(fixture.job)
        fixture.clock += 31
        def restarted():
            assert turn.resume_model_generation(JobStore(fixture.db), fixture.job, token)
            assert turn.confirmed_write_this_turn(fixture.store, fixture.job)
            chat = fixture.chat()
            with patch.object(chat, '_finish_sent_reply', side_effect=AssertionError('must not execute business work')), patch.object(
                    turn, 'record_turn_write', side_effect=AssertionError('must not write a new receipt')):
                fixture.run_async(chat._recover_pending_replies())
                fixture.run_async(chat._recover_pending_replies())
        Context().run(restarted)
        assert len(fixture.messages.accepted) == 1
        assert fixture.store.get_checkpoint(fixture.job, 'turn_write_log') == before
        assert fixture.store.get_job(fixture.job)['attempt_count'] == 0
    finally:
        turn.finish_model_generation(fixture.job)
        turn._GENERATION_CONTEXT.set(None)
        fixture.doCleanups()


def test_off_loop_start_binds_only_its_own_unsuperseded_token(state):
    store, job = state
    async def scenario():
        first = await asyncio.to_thread(turn.begin_model_generation, job, store=store)
        assert turn.current_model_generation(job) == ''
        second = await asyncio.to_thread(turn.begin_model_generation, job, store=store)
        assert not turn.bind_started_model_generation(job, first)
        assert turn.bind_started_model_generation(job, second)
        assert turn.current_model_generation(job) == second
        turn.finish_model_generation(job)
    asyncio.run(scenario())


def test_late_fallback_cannot_close_successor_after_old_context_is_cleared(state):
    store, job = state
    old = turn.begin_model_generation(job, store=store)
    turn.finish_model_generation(job)
    new = turn.begin_model_generation(job, store=store)
    # Simulate another process whose generation is not in this caller's live map.
    with turn._LOCK:
        turn._OPEN_GENERATION.pop(job, None)
    turn._GENERATION_CONTEXT.set(None)
    assert turn.visible_fallback_line(store.path, job, generation=old) is None
    turn.close_turn_after_visible_line(store.path, job, 'Old fallback.', generation=old)
    assert store.get_checkpoint(job, 'worker_response') is None
    assert store.get_job(job)['status'] == 'PENDING'
    assert store.get_checkpoint(job, 'model_generation')['generation'] == new


def test_old_generation_send_is_dropped_before_any_reply_guard_or_http(state):
    from unittest.mock import patch
    from robie_job_engine.chat_thread import bind_job_chat_thread
    from robie_job_engine.models import JobStatus
    from test_round10_reply_lifecycle import SPACE, THREAD, _adapter_module, _chat
    store, job = state
    store.transition(job, JobStatus.RUNNING, expected={JobStatus.PENDING})
    bind_job_chat_thread(store, job, THREAD)
    turn.begin_model_generation(job, store=store)
    old = copy_context()
    new = turn.begin_model_generation(job, store=store)
    adapter = _adapter_module()
    chat = _chat(store.path)
    chat._active_chat_job[SPACE] = job
    with patch.object(adapter, 'ROBIE_JOB_DB', store.path), patch.object(
            adapter, 'guard_chat_response', side_effect=AssertionError('must not finalize successor')):
        result = old.run(asyncio.run, chat.send(SPACE, 'The note has been filed.',
                                               metadata={'robie_job_id': job}))
    assert result.success and result.message_id is None
    assert chat._chat_api.messages.calls == []
    assert store.get_job(job)['status'] == 'RUNNING'
    assert store.get_checkpoint(job, 'model_generation')['generation'] == new
