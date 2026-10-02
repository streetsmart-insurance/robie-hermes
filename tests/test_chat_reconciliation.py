"""Selective #725 regressions on #727; all destinations and transports are fakes.

Source scenarios: test_live_round_725_nogo (stop/session ownership),
test_progress_signout_and_stuck_tab (progress only), and
 test_round27_prod_failures (cancel links, fixture IDs, write-boundary driver).
Browser recovery and safety-seal fixtures are deliberately outside this milestone.
"""
import asyncio
import json
import os
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from robie_job_engine.chat_environment import route_message
from robie_job_engine.chat_clarification import register_question, deliver_reply
from robie_job_engine.chat_guard import guard_chat_response
from robie_job_engine.chat_queue import DurableChatEventQueue
from robie_job_engine.chat_thread import bind_job_chat_thread
from robie_job_engine.chat_turn_control import (
    fail_cancelled_chat_job, agent_output_blocked, refuse_current_tool_call,
    stop_session_keys, is_tool_progress_text, is_refused_tool_text,
)
from robie_job_engine.models import JobStatus
from robie_job_engine.store import JobStore
from robie_job_engine.turn_finalization import (
    begin_model_generation, isolated_model_generation_context,
)
from test_round10_reply_lifecycle import _chat, _Event, _adapter_module, SPACE, THREAD, _outbound_text


@pytest.fixture
def tmp_path():
    from durable_temp import durable_temporary_directory
    with durable_temporary_directory() as tmp:
        yield Path(tmp)


@pytest.fixture(autouse=True)
def clean_context(monkeypatch):
    for key in ('ROBIE_JOB_ID', 'ROBIE_CURRENT_JOB_ID', 'ROBIE_JOB_DB', 'JOB_ID'):
        monkeypatch.delenv(key, raising=False)
    with isolated_model_generation_context():
        yield


def running(store, text='Add a note for Buster Brown', **extra):
    job = store.create_job('hermes.google_chat_task', {
        'text': text, 'request_text': text, 'original_text': text,
        'conversation_id': SPACE, 'requested_by': 'Carlo', 'test_request_id': uuid.uuid4().hex, **extra,
    })
    store.transition(job['id'], JobStatus.RUNNING, expected={JobStatus.PENDING})
    bind_job_chat_thread(store, job['id'], THREAD)
    return job['id']


def test_top_level_stop_finds_exact_thread_not_similar_tail(tmp_path):
    store = JobStore(str(tmp_path / 'jobs.db'))
    owner = running(store)
    chat = _chat(str(store.path))
    expected = f'agent:main:google_chat:dm:{SPACE}/threads/job'
    other = expected + '-other'
    chat.gateway_runner._running_agents = {other: object(), expected: object()}
    resolved, derived, keys = stop_session_keys(chat, _Event(None, '/stop'), owner, store)
    assert resolved == expected
    assert other not in keys
    assert resolved != derived


def test_cancelled_database_row_still_interrupts_live_agent(tmp_path):
    store = JobStore(str(tmp_path / 'jobs.db'))
    owner = running(store)
    store.transition(owner, JobStatus.CANCELLED, expected={JobStatus.RUNNING})
    chat = _chat(str(store.path))
    chat.gateway_runner._running_agents[f'agent:main:google_chat:dm:{SPACE}/threads/job'] = object()
    stopped = []
    async def terminate(event, job_id, **kwargs):
        stopped.append(job_id)
    chat._terminate_running_agent = terminate
    with patch.object(_adapter_module(), 'ROBIE_JOB_DB', str(store.path)):
        asyncio.run(chat._apply_chat_stop(_Event(THREAD, '/stop')))
    assert stopped == [owner]
    assert _outbound_text(chat)
    assert all('already finished' not in text for text in _outbound_text(chat))


def test_cancel_deactivates_link_and_preserves_row(tmp_path):
    store = JobStore(str(tmp_path / 'jobs.db'))
    owner = running(store)
    queue = DurableChatEventQueue(str(store.path))
    queue.link_conversation_job(conversation_id=SPACE, job_id=owner, message_id='one', event_id='one', relation='CREATED')
    fail_cancelled_chat_job(store, owner)
    assert queue.active_conversation_job(SPACE) is None
    assert store.get_job(owner)['status'] == 'CANCELLED'


@pytest.mark.parametrize('progress', [
    '\n'.join(f'🎭 playwright_exec: "step {i}"' for i in range(30)),
    '\n'.join(['Working…', '🎭 playwright_exec: "step"'] * 8),
    'PLAYWRIGHT_BLOCKED: do not guess an EZLynx search URL. Type the name into input#applicantSearch.',
])
def test_progress_and_intermediate_refusal_never_finalize(tmp_path, progress):
    store = JobStore(str(tmp_path / 'jobs.db'))
    owner = running(store, 'What is the GL policy number and carrier for Buster Brown?')
    assert guard_chat_response(str(store.path), owner, progress) == ''
    assert store.get_job(owner)['status'] == 'RUNNING'
    assert store.get_checkpoint(owner, 'worker_response') is None


def test_terminal_tools_refused_but_final_reply_not_stopped(tmp_path, monkeypatch):
    store = JobStore(str(tmp_path / 'jobs.db'))
    owner = running(store)
    begin_model_generation(owner, store=store)
    store.transition(owner, JobStatus.UNVERIFIED, expected={JobStatus.RUNNING})
    other = running(store)
    monkeypatch.setenv('ROBIE_JOB_ID', other)
    assert refuse_current_tool_call({'job_id': other, 'db_path': str(store.path)})
    assert agent_output_blocked(owner, store) is None


@pytest.mark.parametrize('environment,attrs,text,allowed', [
    ('PRODUCTION', {}, 'hello', True), ('TEST', {}, 'hello', False),
    ('PRODUCTION', {}, '[[robie-test]] hello', False),
    ('TEST', {}, '<users/bot> [[robie-test]] hello', True),
    ('TEST', {'robie_env': 'test'}, 'hello', True),
    ('PRODUCTION', {'robie_env': 'test'}, 'hello', False),
    ('TEST', {'robie_env': 'prod'}, '[[robie-test]] hello', False),
    ('TEST', {'robie_env': 'invalid'}, 'hello', False),
    ('', {}, 'hello', False),
])
def test_actual_dispatch_routes_before_job_or_attachment_work(tmp_path, monkeypatch, environment, attrs, text, allowed):
    adapter = _adapter_module()
    chat = _chat(str(tmp_path / 'jobs.db'))
    built = []
    async def build(msg, envelope):
        built.append(msg)
        return None  # Any accepted event reached the real event-building boundary.
    chat._build_message_event = build
    monkeypatch.setenv('ROBIE_ENV', environment)
    asyncio.run(chat._dispatch_message({'text': text}, {}, routing_attributes=attrs))
    assert bool(built) == allowed
    if built:
        assert built[0]['text'] == 'hello'


def test_both_text_fields_normalized_without_touching_original():
    msg = {'text': '<users/bot> [[robie-test]] /stop', 'argumentText': 'robie-test: /stop'}
    route, clean = route_message(msg)
    assert route == 'test'
    assert clean == {'text': '/stop', 'argumentText': '/stop'}
    assert '[[robie-test]]' in msg['text']


def question(store):
    owner = running(store)
    generation = begin_model_generation(owner, store=store)
    store.checkpoint(owner, 'chat_request_owner', {'actor': 'users/carlo', 'environment': 'test'})
    assert register_question(store, owner, generation, 'clarify-one')
    return owner, generation


def answer(store, resolve, **override):
    args = dict(thread=THREAD, actor='users/carlo', environment='test', message_id='reply-one', text='use follw up 1', resolve=resolve)
    args.update(override)
    return deliver_reply(store, **args)


def test_clarification_wakes_one_owner_generation_once(tmp_path):
    store = JobStore(str(tmp_path / 'jobs.db'))
    owner, generation = question(store)
    resolve = Mock(return_value=True)
    assert answer(store, resolve) == 'delivered'
    assert answer(store, resolve) == 'delivered'
    resolve.assert_called_once_with('clarify-one', 'use follw up 1')
    assert store.get_checkpoint(owner, 'clarification_reply')['generation'] == generation
    assert len(store.list_jobs_by_status(set(JobStatus))) == 1


@pytest.mark.parametrize('override', [{'actor': 'users/other'}, {'environment': 'prod'}, {'clarify_id': 'another'}])
def test_unauthorized_clarification_cannot_wake(tmp_path, override):
    store = JobStore(str(tmp_path / 'jobs.db'))
    question(store)
    resolve = Mock(return_value=True)
    assert answer(store, resolve, **override) == 'refused'
    resolve.assert_not_called()


def test_stale_or_cancelled_clarification_cannot_wake(tmp_path):
    store = JobStore(str(tmp_path / 'jobs.db'))
    owner, _ = question(store)
    begin_model_generation(owner, store=store)
    resolve = Mock(return_value=True)
    assert answer(store, resolve) == 'refused'
    store.transition(owner, JobStatus.CANCELLED, expected={JobStatus.RUNNING})
    assert answer(store, resolve, clarify_id='clarify-one') == 'refused'
    assert answer(store, resolve, text='Start a new request') == 'absent'
    resolve.assert_not_called()


def transport_client(kind):
    if kind == 'discussion':
        from test_ezlynx_discussions import make_client, token_routes
        client = make_client(token_routes([('/notes', {'noteId': 'note-one'})]), session_headers=lambda url: {})
        client.get_token()
        return client, lambda: client._post('v8/discussions/123/notes', {'text': 'test'})
    from test_ezlynx_api import _config, FakeResponse
    from robie_job_engine.ezlynx_api import EzlynxApiClient
    client = EzlynxApiClient(_config(), urlopen=Mock(return_value=FakeResponse(b'{}')))
    client._token = 'synthetic'
    client._token_expires_at = float('inf')
    if kind == 'json':
        return client, lambda: client.post_json('/DiscussionApi/discussions/v1/notes', {})
    return client, lambda: client._request_bytes('POST', 'https://example.test/DocumentApi/upload', data=b'doc', headers={})


@pytest.mark.parametrize('kind', ['discussion', 'json', 'document'])
def test_driver_rechecked_at_actual_transport(kind, monkeypatch):
    from robie_job_engine.ezlynx_driver_gate import EzlynxDriverGateRefused
    client, write = transport_client(kind)
    transport = Mock(side_effect=AssertionError('write crossed boundary'))
    client._urlopen = transport
    with patch('robie_job_engine.ezlynx_driver_gate.require_driver_in', side_effect=EzlynxDriverGateRefused('OUT')):
        with pytest.raises(EzlynxDriverGateRefused):
            write()
    transport.assert_not_called()


@pytest.mark.parametrize('kind', ['discussion', 'json', 'document'])
def test_cancelled_owner_cannot_borrow_newer_job_at_transport(tmp_path, monkeypatch, kind):
    store = JobStore(str(tmp_path / 'jobs.db'))
    owner = running(store)
    begin_model_generation(owner, store=store)
    store.transition(owner, JobStatus.CANCELLED, expected={JobStatus.RUNNING})
    other = running(store)
    monkeypatch.setenv('ROBIE_JOB_ID', other)
    monkeypatch.setenv('ROBIE_JOB_DB', str(store.path))
    client, write = transport_client(kind)
    client._urlopen = Mock()
    with pytest.raises(RuntimeError, match='EZLYNX_WRITE_REFUSED'):
        write()
    client._urlopen.assert_not_called()


def test_missing_or_unreadable_write_context_fails_closed(tmp_path, monkeypatch):
    from robie_job_engine.chat_write_boundary import assert_chat_write_allowed
    begin_model_generation('owner-without-store')
    with pytest.raises(RuntimeError, match='missing bound'):
        assert_chat_write_allowed()
    store = JobStore(str(tmp_path / 'jobs.db'))
    owner = running(store)
    begin_model_generation(owner, store=store)
    with patch.object(JobStore, 'get_job', side_effect=OSError('unavailable')):
        with pytest.raises(RuntimeError, match='state unavailable'):
            assert_chat_write_allowed()


@pytest.mark.parametrize('request_text', ['Add a note for Buster Brown', 'add a note for buster brown'])
def test_fixture_applicant_and_invented_discussion_refused(tmp_path, request_text):
    from robie_job_engine.write_verification_loop import refuse_tool_write
    from robie_job_engine.ezlynx_discussions import select_discussion_for_note, DiscussionSelectionError
    store = JobStore(str(tmp_path / 'jobs.db'))
    owner = running(store, request_text, applicant_id='220250093')
    begin_model_generation(owner, store=store)
    refused = refuse_tool_write({'applicant_id': '220250093'}, {})
    assert 'EZLYNX_APPLICANT_UNTRUSTED' in refused
    with pytest.raises(DiscussionSelectionError):
        select_discussion_for_note([
            {'discussionId': '1', 'title': 'Message Received by Robie'},
            {'discussionId': '2', 'title': 'follw up 1'},
        ], title_hint='Message Received by Robie')


def test_name_only_request_drops_fixture_and_binds_only_authorized_search(tmp_path):
    from robie_job_engine.client_name_lookup import prepare_named_write_client, set_client_name_searcher, trusted_applicant_ids
    store = JobStore(str(tmp_path / 'jobs.db'))
    owner = running(store, applicant_id='220250093')
    try:
        set_client_name_searcher(None)
        assert prepare_named_write_client(store, owner)
        assert not store.get_job(owner)['payload'].get('applicant_id')
        set_client_name_searcher(lambda name: {'status': 'ok', 'matches': [{'name': 'Buster Brown', 'applicant_id': '26356199'}]})
        assert prepare_named_write_client(store, owner) is None
        assert trusted_applicant_ids(store, store.get_job(owner)) == ['26356199']
    finally:
        set_client_name_searcher(None)


def test_clarification_store_error_propagates_for_nack(tmp_path):
    store = JobStore(str(tmp_path / 'jobs.db'))
    question(store)
    resolve = Mock()
    with patch.object(store, 'transaction', side_effect=OSError('database down')):
        with pytest.raises(OSError):
            answer(store, resolve)
    resolve.assert_not_called()


def test_actual_dispatch_consumes_clarification_without_second_session(tmp_path, monkeypatch):
    import sys
    import types
    adapter = _adapter_module()
    store = JobStore(str(tmp_path / 'jobs.db'))
    owner, generation = question(store)
    chat = _chat(str(store.path))
    async def build(msg, envelope):
        return _Event(THREAD, msg['text'])
    chat._build_message_event = build
    chat._open_and_run_chat_job = Mock(side_effect=AssertionError('second session'))
    module = types.ModuleType('tools.clarify_gateway')
    module.resolve_gateway_clarify = Mock(return_value=True)
    monkeypatch.setitem(sys.modules, 'tools.clarify_gateway', module)
    monkeypatch.setenv('ROBIE_ENV', 'TEST')
    monkeypatch.setattr(adapter, 'ROBIE_JOB_DB', str(store.path))
    asyncio.run(chat._dispatch_message({'text': '[[robie-test]] use follw up 1'}, {}))
    module.resolve_gateway_clarify.assert_called_once_with('clarify-one', 'use follw up 1')
    chat._open_and_run_chat_job.assert_not_called()
    assert len(store.list_jobs_by_status(set(JobStatus))) == 1


def test_gateway_pubsub_duplicate_ack_waits_for_single_handoff(tmp_path, monkeypatch):
    from robie_job_engine.pubsub_ack import PubSubAckCoordinator
    adapter = _adapter_module()
    chat = _chat(str(tmp_path / 'jobs.db'))
    chat._shutting_down = False
    store = JobStore(str(tmp_path / 'jobs.db'))
    accepted = []
    replies = []
    class Dedup:
        seen = set()
        def contains(self, key): return key in self.seen
        def is_duplicate(self, key): self.seen.add(key)
        def discard(self, key): self.seen.discard(key)
    coordinator = PubSubAckCoordinator(Dedup())
    tasks = []
    def schedule(coro, message, message_id=''):
        def submit(coro):
            task = asyncio.create_task(coro)
            tasks.append(task)
            return task
        coordinator.schedule(coro=coro, message=message, message_id=message_id, submit=submit)
    chat._schedule_pubsub_processing = schedule
    async def build(msg, envelope):
        accepted.append(msg['text'])
        running(store, msg['text'])
        replies.append('synthetic reply')
        return None
    chat._build_message_event = build
    event = {'type': 'MESSAGE', 'space': {'name': SPACE}, 'message': {
        'name': SPACE + '/messages/one', 'sender': {'type': 'HUMAN'}, 'text': 'hello',
    }}
    def delivery():
        return type('Delivery', (), {'data': json.dumps(event).encode(), 'attributes': {}, 'ack': Mock(), 'nack': Mock()})()
    first, duplicate = delivery(), delivery()
    monkeypatch.setenv('ROBIE_ENV', 'PRODUCTION')
    async def run():
        chat._on_pubsub_message(first)
        chat._on_pubsub_message(duplicate)
        first.ack.assert_not_called()
        duplicate.ack.assert_not_called()
        await asyncio.gather(*tasks)
        await asyncio.sleep(0)
    asyncio.run(run())
    assert accepted == ['hello']
    assert replies == ['synthetic reply']
    assert len(store.list_jobs_by_status(set(JobStatus))) == 1
    first.ack.assert_called_once()
    duplicate.ack.assert_called_once()
    first.nack.assert_not_called()


def test_exact_receipt_survives_later_failure_but_not_next_generation(tmp_path):
    from robie_job_engine.turn_finalization import record_turn_write, confirmed_write_this_turn
    store = JobStore(str(tmp_path / 'jobs.db'))
    owner = running(store)
    begin_model_generation(owner, store=store)
    assert not record_turn_write(store, owner, note_id='')
    assert not confirmed_write_this_turn(store, owner)
    assert record_turn_write(store, owner, note_id='independently-read-back-note')
    store.transition(owner, JobStatus.FAILED, expected={JobStatus.RUNNING}, error='later unrelated step')
    assert confirmed_write_this_turn(store, owner)
    begin_model_generation(owner, store=store)
    assert not confirmed_write_this_turn(store, owner)


def test_note_entry_refuses_fixture_before_lookup_or_post(tmp_path):
    from robie_job_engine.ezlynx_discussions import file_note_to_existing_discussion
    store = JobStore(str(tmp_path / 'jobs.db'))
    owner = running(store, applicant_id='220250093')
    begin_model_generation(owner, store=store)
    client = Mock()
    with pytest.raises(RuntimeError, match='EZLYNX_APPLICANT_UNTRUSTED'):
        file_note_to_existing_discussion(client, '220250093', 'Synthetic note', ledger_path=str(tmp_path / 'ledger.json'))
    client.get_discussions.assert_not_called()
    client.append_note.assert_not_called()


@pytest.mark.parametrize('lease', [
    {'version': 1, 'state': 'IN', 'holder': 'TEST', 'expires_at': '2099-01-01T00:00:00Z'},
    {'version': 1, 'state': 'OUT', 'holder': 'PRODUCTION', 'expires_at': '2099-01-01T00:00:00Z'},
    {'version': 1, 'state': 'IN', 'holder': 'PRODUCTION', 'expires_at': '2000-01-01T00:00:00Z'},
    {},
])
def test_actual_driver_lease_rejects_api_post(lease, monkeypatch):
    from robie_job_engine.ezlynx_driver_gate import EzlynxDriverGateRefused
    client, write = transport_client('discussion')
    client._urlopen = Mock()
    monkeypatch.setenv('ROBIE_EZLYNX_DRIVER_GATE_REQUIRED', '1')
    monkeypatch.setenv('ROBIE_EZLYNX_DRIVER_HOLDER', 'PRODUCTION')
    with patch('robie_job_engine.ezlynx_driver_gate.read_metadata', return_value=json.dumps(lease)):
        with pytest.raises(EzlynxDriverGateRefused):
            write()
    client._urlopen.assert_not_called()


def test_card_clarification_requires_actor_and_keeps_thread(tmp_path, monkeypatch):
    import sys
    import types
    store = JobStore(str(tmp_path / 'jobs.db'))
    owner, _ = question(store)
    chat = _chat(str(store.path))
    chat._job_db_path = str(store.path)
    chat._clarify_state = {'clarify-one': 'owner-session'}
    module = types.ModuleType('tools.clarify_gateway')
    module.resolve_gateway_clarify = Mock(return_value=True)
    monkeypatch.setitem(sys.modules, 'tools.clarify_gateway', module)
    monkeypatch.setenv('ROBIE_ENV', 'TEST')
    card = {
        'user': {'name': 'users/other', 'type': 'HUMAN'},
        'message': {'name': SPACE + '/messages/card', 'thread': {'name': THREAD}},
        'common': {'invokedFunction': 'hermes_clarify', 'parameters': {
            'robie_env': 'test', 'clarify_id': 'clarify-one', 'choice': 'follw up 1',
        }},
    }
    asyncio.run(chat._handle_card_event(card, notify=False))
    module.resolve_gateway_clarify.assert_not_called()
    assert store.get_checkpoint(owner, 'chat_live_question')['open']
    card['user']['name'] = 'users/carlo'
    result = asyncio.run(chat._handle_card_event(card, notify=False))
    assert 'Choice recorded' in result
    module.resolve_gateway_clarify.assert_called_once_with('clarify-one', 'follw up 1')
    assert store.get_checkpoint(owner, 'chat_thread')['thread_name'] == THREAD


@pytest.mark.parametrize('stamp', ['', 'unknown', 'TEST', 'prod'])
def test_invalid_or_foreign_card_environment_is_refused(stamp, monkeypatch):
    monkeypatch.setenv('ROBIE_ENV', 'TEST')
    assert not _adapter_module()._click_routed_to_this_gateway({'robie_env': stamp})
    monkeypatch.setenv('ROBIE_ENV', 'PRODUCTION')
    if stamp != 'prod':
        assert not _adapter_module()._click_routed_to_this_gateway({'robie_env': stamp})
