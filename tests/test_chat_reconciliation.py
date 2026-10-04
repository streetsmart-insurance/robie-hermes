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
    assert answer(store, resolve, text='Start a new request') == 'new_intent'
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


@pytest.mark.parametrize('wording', ['Add a note for Buster Brown', 'Add a note to Buster Brown’s account'])
def test_note_entry_refuses_fixture_before_lookup_or_post(tmp_path, wording):
    from robie_job_engine.ezlynx_discussions import file_note_to_existing_discussion
    store = JobStore(str(tmp_path / 'jobs.db'))
    owner = running(store, wording, applicant_id='220250093')
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


@pytest.mark.parametrize('flag', [True])
@pytest.mark.parametrize('attrs', [{}, {'robie_env': 'prod'}])
def test_foreign_thread_stop_refused_before_build(tmp_path, monkeypatch, flag, attrs):
    store = JobStore(str(tmp_path / 'prod.db'))
    owner = running(store)
    chat = _chat(str(store.path))
    monkeypatch.setattr(_adapter_module(), 'ROBIE_JOB_DB', str(store.path))
    monkeypatch.setenv('ROBIE_ENV', 'PRODUCTION')
    chat._active_chat_job[SPACE] = owner
    chat._job_db_path = str(store.path)
    chat._build_message_event = Mock(side_effect=AssertionError('must not build'))
    msg = {'text': '/stop', 'space': {'name': SPACE}, 'thread': {'name': SPACE + '/threads/test-only'}}
    if flag is not None:
        msg['threadReply'] = flag
    asyncio.run(chat._dispatch_message(msg, {}, routing_attributes=attrs))
    chat._build_message_event.assert_not_called()
    assert store.get_job(owner)['status'] == 'RUNNING'


def test_omitted_thread_reply_reaches_the_builder(tmp_path, monkeypatch):
    """A real top-level message has a thread and no threadReply field."""
    store = JobStore(str(tmp_path / 'prod.db'))
    chat = _chat(str(store.path))
    monkeypatch.setattr(_adapter_module(), 'ROBIE_JOB_DB', str(store.path))
    monkeypatch.setenv('ROBIE_ENV', 'PRODUCTION')
    from unittest.mock import AsyncMock
    chat._build_message_event = AsyncMock(return_value=None)
    space = 'spaces/AAQAZbLJO78'
    msg = {
        'name': space + '/messages/HjADXxDpgPk.HjADXxDpgPk',
        'text': 'hello',
        'space': {'name': space},
        'thread': {'name': space + '/threads/HjADXxDpgPk'},
    }
    assert 'threadReply' not in msg
    asyncio.run(chat._dispatch_message(msg, {}))
    chat._build_message_event.assert_called_once()


def test_thread_contract_and_read_failure(tmp_path):
    from robie_job_engine.chat_environment import thread_ingress_refusal, ThreadOwnershipUnavailable
    store = JobStore(str(tmp_path / 'prod.db'))
    owner = running(store)
    message = {'thread': {'name': THREAD}, 'threadReply': True}
    kwargs = dict(space=SPACE, environment='prod', db_path=str(store.path))
    assert thread_ingress_refusal(message, **kwargs) is None
    store.checkpoint(owner, 'chat_request_owner', {'environment': 'test'})
    assert thread_ingress_refusal(message, **kwargs) == 'CHAT_THREAD_ENVIRONMENT_MISMATCH'
    assert thread_ingress_refusal({**message, 'threadReply': False}, **kwargs) is None
    assert thread_ingress_refusal({**message, 'threadReply': 'false'}, **kwargs) == 'CHAT_THREAD_METADATA_INVALID'
    # Live top-level shape: Google omits threadReply. The message id is <tid>.<tid>.
    top_level = {
        'name': 'spaces/AAQAZbLJO78/messages/HjADXxDpgPk.HjADXxDpgPk',
        'thread': {'name': 'spaces/AAQAZbLJO78/threads/HjADXxDpgPk'},
    }
    assert 'threadReply' not in top_level
    assert thread_ingress_refusal(
        top_level, space='spaces/AAQAZbLJO78', environment='test', db_path=str(store.path),
    ) is None
    owned_reply = {**top_level, 'threadReply': True}
    assert thread_ingress_refusal(
        owned_reply, space='spaces/AAQAZbLJO78', environment='test', db_path=str(store.path),
    ) == 'CHAT_THREAD_NOT_OWNED'
    assert thread_ingress_refusal(
        {**top_level, 'threadReply': False},
        space='spaces/AAQAZbLJO78', environment='test', db_path=str(store.path),
    ) is None
    missing = tmp_path / 'missing.db'
    with pytest.raises(ThreadOwnershipUnavailable):
        thread_ingress_refusal(message, **{**kwargs, 'db_path': str(missing)})
    assert not missing.exists()


@pytest.mark.parametrize('body', ['/stop', 'use follw up 1', 'hello'])
@pytest.mark.parametrize('field', ['text', 'argumentText'])
def test_multiple_mentions_cannot_hide_test_marker(body, field):
    message = {field: '<users/one> <users/two> @robie [[robie-test]] ' + body}
    route, clean = route_message(message)
    assert route == 'test'
    assert clean[field] == body
    assert route_message(message, {'robie_env': 'prod'})[0] == 'invalid'


def test_cancelled_old_stop_cannot_touch_new_shared_agent(tmp_path):
    from robie_job_engine.chat_turn_control import terminate_gateway_agent, job_turn_is_alive
    store = JobStore(str(tmp_path / 'jobs.db'))
    old = running(store)
    store.transition(old, JobStatus.CANCELLED, expected={JobStatus.RUNNING})
    new = running(store)
    chat = _chat(str(store.path))
    key = f'agent:main:google_chat:dm:{SPACE}'
    agent = Mock()
    lease = Mock()
    chat.gateway_runner._running_agents[key] = agent
    chat.gateway_runner._active_session_leases = {key: lease}
    chat.gateway_runner._session_history = {key: ['new turn']}
    chat.gateway_runner._interrupt_and_clear_session = Mock()
    chat._gateway_turns[(SPACE, '')] = {'job_id': new, 'task': None}
    chat._active_chat_job[SPACE] = new
    assert not job_turn_is_alive(chat, _Event(THREAD, '/stop'), old, store)
    asyncio.run(terminate_gateway_agent(chat, _Event(THREAD, '/stop'), old, reason='/stop', store=store))
    agent.assert_not_called()
    assert agent.method_calls == []
    lease.release.assert_not_called()
    chat.gateway_runner._interrupt_and_clear_session.assert_not_called()
    assert chat.gateway_runner._session_history[key] == ['new turn']
    assert store.get_job(new)['status'] == 'RUNNING'


def test_stop_generation_and_actual_agent_identity(tmp_path):
    from robie_job_engine.chat_turn_control import remember_session_owner, job_turn_is_alive
    store = JobStore(str(tmp_path / 'jobs.db'))
    owner = running(store)
    generation = begin_model_generation(owner, store=store)
    chat = _chat(str(store.path))
    key = f'agent:main:google_chat:dm:{SPACE}'
    chat.gateway_runner._running_agents[key] = object()
    event = _Event(None, '/stop')
    assert not job_turn_is_alive(chat, event, owner, store)
    remember_session_owner(chat, event, owner, generation)
    assert job_turn_is_alive(chat, event, owner, store)
    chat.gateway_runner._running_agents[key] = object()
    assert not job_turn_is_alive(chat, event, owner, store)


@pytest.mark.parametrize('terminal', [True, False])
def test_current_clarification_ignores_historical_candidates(tmp_path, terminal):
    store = JobStore(str(tmp_path / 'jobs.db'))
    old, _ = question(store)
    if terminal:
        store.transition(old, JobStatus.CANCELLED, expected={JobStatus.RUNNING})
    else:
        begin_model_generation(old, store=store)
    current, generation = question(store)
    register_question(store, current, generation, 'current-question')
    resolve = Mock(return_value=True)
    assert answer(store, resolve, clarify_id='current-question') == 'delivered'
    assert answer(store, resolve, clarify_id='current-question') == 'delivered'
    resolve.assert_called_once_with('current-question', 'use follw up 1')
    assert answer(store, resolve, clarify_id='clarify-one', message_id='other') == 'refused'


def test_new_request_does_not_answer_question(tmp_path):
    from robie_job_engine.chat_job_controls import should_bind_waiting_reply
    store = JobStore(str(tmp_path / 'jobs.db'))
    owner, generation = question(store)
    before = store.get_job(owner)['payload']
    resolve = Mock(return_value=True)
    assert answer(store, resolve, text='New request: check a different client') == 'new_intent'
    resolve.assert_not_called()
    assert store.get_checkpoint(owner, 'chat_live_question')['open']
    assert store.get_job(owner)['payload'] == before


@pytest.mark.parametrize('text', ["Add a note to Buster Brown’s account", "Add a note to Buster Brown's account", 'Write this down'])
def test_every_bound_write_requires_applicant_provenance(tmp_path, text):
    from robie_job_engine.chat_write_boundary import assert_chat_applicant
    store = JobStore(str(tmp_path / 'jobs.db'))
    owner = running(store, text, applicant_id='220250093')
    begin_model_generation(owner, store=store)
    with pytest.raises(RuntimeError, match='EZLYNX_APPLICANT_UNTRUSTED'):
        assert_chat_applicant('220250093')
    client, write = transport_client('discussion')
    client._urlopen = Mock()
    with pytest.raises(RuntimeError, match='EZLYNX_APPLICANT_UNTRUSTED'):
        write()
    client._urlopen.assert_not_called()


@pytest.mark.parametrize('text,hint,allowed', [
    ('Add the note to Follow Up', 'Follow Up', True),
    ('Do not use Follow Up', 'Follow Up', False),
    ('Don’t use Follow Up', 'Follow Up', False),
    ('Use Follow Up Later', 'Follow Up', False),
    ('Follow Up was mentioned earlier', 'Follow Up', False),
    ('Use Follow', 'Follow', False),
])
def test_bound_discussion_selection_is_exact_and_affirmative(tmp_path, text, hint, allowed):
    from robie_job_engine.ezlynx_discussions import select_discussion_for_note, DiscussionSelectionError
    store = JobStore(str(tmp_path / 'jobs.db'))
    owner = running(store, text)
    begin_model_generation(owner, store=store)
    rows = [{'discussionId': '1', 'title': 'Follow Up'}, {'discussionId': '2', 'title': 'Other'}]
    if allowed:
        assert select_discussion_for_note(rows, title_hint=hint)['discussionId'] == '1'
    else:
        with pytest.raises(DiscussionSelectionError):
            select_discussion_for_note(rows, title_hint=hint)


def test_http_card_missing_runtime_refuses_before_local_resolution(tmp_path, monkeypatch):
    chat = _chat(str(tmp_path / 'jobs.db'))
    monkeypatch.delenv('ROBIE_ENV', raising=False)
    envelope = {'type': 'CARD_CLICKED', 'action': {'actionMethodName': 'robie_decision', 'parameters': []}}
    assert asyncio.run(chat.dispatch_http_event(envelope)) == {}


@pytest.mark.parametrize('flag', [True, None, False])
def test_real_builder_retains_owned_reply_after_restart(tmp_path, monkeypatch, flag):
    from types import SimpleNamespace
    adapter = _adapter_module()
    monkeypatch.setattr(adapter, 'MessageEvent', SimpleNamespace)
    chat = _chat(str(tmp_path / 'jobs.db'))
    chat._last_sender_by_chat = {}
    chat._reply_in_existing_thread = {}
    chat._thread_count_store.incr = lambda *args: 0
    chat.build_source = lambda **kwargs: SimpleNamespace(**kwargs)
    msg = {'name': SPACE + '/messages/reply', 'thread': {'name': THREAD}, 'text': '/stop',
           'sender': {'name': 'users/carlo'}, 'space': {'name': SPACE, 'type': 'DM'}}
    if flag is not None:
        msg['threadReply'] = flag
    event = asyncio.run(chat._build_message_event(msg, {}))
    assert event.source.thread_id == (THREAD if flag is True else None)
    assert chat._reply_in_existing_thread.get(msg['name']) == (THREAD if flag is True else None)


def test_verified_target_remains_authorized_at_transport(tmp_path):
    from robie_job_engine.chat_write_boundary import assert_chat_write_allowed, assert_chat_applicant
    store = JobStore(str(tmp_path / 'jobs.db'))
    owner = running(store, 'Add a note to applicant 26356199', applicant_id='26356199')
    begin_model_generation(owner, store=store)
    assert_chat_write_allowed()
    assert_chat_applicant('26356199')
    with pytest.raises(RuntimeError, match='EZLYNX_APPLICANT_UNTRUSTED'):
        assert_chat_applicant('220250093')


def test_new_request_in_old_question_thread_gets_one_fresh_job(tmp_path):
    from robie_job_engine.chat_guard import open_chat_job
    from robie_job_engine.chat_job_controls import mark_job_waiting_for_user
    store = JobStore(str(tmp_path / 'jobs.db'))
    owner, _ = question(store)
    mark_job_waiting_for_user(store, owner, 'Which discussion?')
    kwargs = dict(conversation_id=SPACE, inbound_thread_id=THREAD, requested_by='Carlo')
    first = open_chat_job(str(store.path), 'new-request', 'New request: check a different client', **kwargs)
    duplicate = open_chat_job(str(store.path), 'new-request', 'New request: check a different client', **kwargs)
    assert first == duplicate and first != owner
    assert store.get_checkpoint(owner, 'chat_live_question')['open']
    assert len(store.list_jobs_by_status(set(JobStatus))) == 2


def test_owned_stop_cancels_exact_stamped_background_task(tmp_path):
    from robie_job_engine.chat_turn_control import remember_session_owner, terminate_gateway_agent
    store = JobStore(str(tmp_path / 'jobs.db'))
    owner = running(store)
    generation = begin_model_generation(owner, store=store)
    chat = _chat(str(store.path))
    key = f'agent:main:google_chat:dm:{SPACE}'
    agent = Mock()
    task = Mock()
    task.done.return_value = False
    chat.gateway_runner._running_agents[key] = agent
    chat.gateway_runner._session_history = {key: ['old']}
    async def clear(*args, **kwargs):
        chat.gateway_runner._running_agents.pop(key)
    chat.gateway_runner._interrupt_and_clear_session = clear
    event = _Event(None, '/stop')
    remember_session_owner(chat, event, owner, generation, task)
    asyncio.run(terminate_gateway_agent(chat, event, owner, reason='/stop', store=store))
    agent.interrupt.assert_called_once()
    task.cancel.assert_called_once()
    assert chat.gateway_runner._session_history[key] == []


def test_session_stamp_never_claims_another_thread_agent(tmp_path):
    from robie_job_engine.chat_turn_control import remember_session_owner
    chat = _chat(str(tmp_path / 'jobs.db'))
    shared = f'agent:main:google_chat:dm:{SPACE}'
    threaded = shared + '/threads/job'
    chat.gateway_runner._running_agents = {shared: object(), threaded: object()}
    event = _Event(THREAD, 'hello')
    remember_session_owner(chat, event, 'owner', 'generation')
    assert chat._robie_session_owners == {}  # ambiguous fallback refuses
    chat._event_session_key = lambda event: threaded
    remember_session_owner(chat, event, 'owner', 'generation')
    assert list(chat._robie_session_owners) == [threaded]


@pytest.mark.parametrize('instruction,single,expected_posts', [
    ('Add a note to applicant 26356199 with text "Forwarded the paperwork to New Business." Ask me which discussion to use', False, 0),
    ('Add a note to applicant 26356199 with text "Forwarded the paperwork to New Business."', False, 0),
    ('Add a note to applicant 26356199 with text “Use New Business.”', False, 0),
    ('Use New Business. Add a note to applicant 26356199. Ask me which discussion to use', False, 0),
    ('Use New Business. Add a note to applicant 26356199, but I am unsure', False, 0),
    ('Use New Business. Add a note to applicant 26356199 with text "Forwarded the paperwork."', False, 1),
    ('Add a note to applicant 26356199 in New Business with text "Forwarded the paperwork."', False, 1),
    ('Add a note to applicant 26356199 with text "Forwarded the paperwork."', True, 1),
])
def test_actual_note_handler_never_selects_from_payload(tmp_path, monkeypatch, instruction, single, expected_posts):
    from functools import partial
    from test_tonight_fix_bundle import _load_hermes_tool, _restore_modules
    from test_discussion_note_readback import LiveShapeClient
    from robie_job_engine import ezlynx_api_only_writes as writes
    from robie_job_engine.write_verification_loop import PLAN_CHECKPOINT
    from robie_job_engine import ezlynx_write_scope
    monkeypatch.setattr(ezlynx_write_scope, 'ALLOWED_EZLYNX_WRITE_APPLICANT_IDS', frozenset({'26356199'}))

    class Client(LiveShapeClient):
        def __init__(self):
            super().__init__(post_body={'noteId': '701'})
            self.title = self.after_title = 'New Business'
        def get_discussions(self, applicant_id):
            rows = super().get_discussions(applicant_id)
            return rows if single else rows + [{'discussionId': 'another', 'title': 'Follow Up'}]
        def get_discussion(self, discussion_id):
            return {**super().get_discussion(discussion_id), 'applicantId': '26356199'}
        def append_note(self, discussion_id, text, note_type='Note'):
            self.body = text
            return super().append_note(discussion_id, text, note_type)
        def list_notes(self, discussion_id):
            return [{'noteId': '701', 'noteText': self.body}] if self.posted else []

    store = JobStore(str(tmp_path / 'jobs.db'))
    owner = running(store, instruction, applicant_id='26356199')
    generation = begin_model_generation(owner, store=store)
    # A valid tool plan must never turn its model-generated hint into user authority.
    store.checkpoint(owner, PLAN_CHECKPOINT, {'locked': True})
    if expected_posts:
        from robie_job_engine.chat_write_go import bind_chat_write_go

        assert bind_chat_write_go(store, owner, 'go', message_id='m-go')
    client = Client()
    actual_api = partial(writes.add_note_to_discussion, discussion_client=client, ledger_path=tmp_path / 'ledger.json')
    note, previous, created = _load_hermes_tool('quoted_payload_note_tool', 'ezlynx_note_tool.py')
    try:
        with patch.object(writes, 'add_note_to_discussion', side_effect=actual_api):
            response = note.ezlynx_discussion_note_handler(
                {'applicant_id': '26356199', 'note_text': 'Forwarded the paperwork to New Business.', 'title_hint': 'New Business'},
                job_id=owner, db_path=str(store.path), outcome_poster=lambda *_args: None)
        assert 'result' in response, response
        assert client.posts == expected_posts, response
        result = response['result']
        if expected_posts:
            assert result['status'] == 'filed'
            assert result['read_back'] is True and result['note_id'] == '701'
        else:
            assert result['status'] == 'needs_discussion'
            assert not result.get('read_back') and not result.get('note_id')
            from robie_job_engine.turn_finalization import confirmed_write_this_turn
            assert not confirmed_write_this_turn(store, owner)
    finally:
        _restore_modules(previous, created)
