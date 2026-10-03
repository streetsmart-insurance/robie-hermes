"""No network: no-job final answers need trusted turn ownership, not fake Jobs."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from durable_temp import durable_temporary_directory
from test_round10_reply_lifecycle import _adapter_module, _chat, SPACE, THREAD, OTHER
from robie_job_engine.conversation_reply import advance, bind, current_reply
from robie_job_engine.store import JobStore

MARKER = 'QA-e3a3aad9-03'
ACTOR = 'carlo@streetsmart.insurance'


def event(thread=THREAD, text='What is this?', actor=ACTOR):
    return SimpleNamespace(source=SimpleNamespace(chat_id=SPACE,thread_id=thread,user_id=actor,chat_type='dm'),
        message_id=SPACE+'/messages/inbound', text=text,
        raw_message={'name':SPACE+'/messages/inbound','thread':{'name':thread},'sender':{'email':actor,'type':'HUMAN'}})


@pytest.fixture
def chat():
    module = _adapter_module()
    with durable_temporary_directory() as directory:
        db = str(Path(directory)/'jobs.db')
        JobStore(db)
        from robie_job_engine.chat_queue import DurableChatEventQueue
        with patch.object(module,'ROBIE_JOB_DB',db), patch.dict('os.environ',{'ROBIE_HEALTH_CHAT_SPACE':''}):
            chat = _chat(db)
            chat._chat_queue = DurableChatEventQueue(db)
            from test_chat_reply_recovery import Messages
            chat._chat_api.messages = Messages()
            chat._chat_api._spaces._messages = chat._chat_api.messages
            yield chat


def execute(coro):
    return asyncio.run(coro)


def test_reproduces_current_no_job_drop(chat):
    result = execute(chat.send(SPACE,MARKER,reply_to=event().message_id,metadata={'thread_id':THREAD}))
    assert result.success and result.message_id is None
    assert not chat._chat_api.messages.calls


def test_validated_final_reply_exact_frozen_thread_and_stable_id(chat):
    inbound=event();advance(chat,inbound)
    async def run():
        with bind(chat,inbound) as scope:
            scope.seal(inbound,MARKER)
            chat._last_inbound_thread[SPACE]=OTHER
            result=await chat.send(SPACE,MARKER,reply_to=inbound.message_id,metadata={'thread_id':THREAD})
            assert result.success
            assert not (await chat.send(SPACE,MARKER,reply_to=inbound.message_id,metadata={'thread_id':THREAD})).success
            return scope.request_id
    request=execute(run());calls=chat._chat_api.messages.calls
    assert len(calls)==1
    assert calls[0]['body']=={'text':MARKER,'thread':{'name':THREAD}}
    assert calls[0]['messageId']=='client-'+request
    assert calls[0]['messageReplyOption']=='REPLY_MESSAGE_OR_FAIL'
    with JobStore(chat._db).connect() as conn:
        assert conn.execute('SELECT count(*) FROM jobs').fetchone()[0]==0


def test_main_dm_stays_top_level(chat):
    inbound=event(thread=None);advance(chat,inbound)
    async def run():
        with bind(chat,inbound) as scope:
            scope.seal(inbound,MARKER)
            chat._last_inbound_thread[SPACE]=OTHER
            return await chat.send(SPACE,MARKER,reply_to=inbound.message_id)
    assert execute(run()).success
    assert 'thread' not in chat._chat_api.messages.calls[0]['body']


@pytest.mark.parametrize('mutation',[
    {'thread_id':OTHER},{'thread_name':OTHER},{'robie_job_id':'arbitrary-job'},
    {'job_id':'cron'},{'robie_delivery_kind':'notice'},{'robie_stop_notice':True},
])
def test_metadata_cannot_widen_scope(chat,mutation):
    inbound=event();advance(chat,inbound)
    async def run():
        with bind(chat,inbound) as scope:
            scope.seal(inbound,MARKER)
            return await chat.send(SPACE,MARKER,reply_to=inbound.message_id,metadata={'thread_id':THREAD,**mutation})
    assert not execute(run()).success
    assert not chat._chat_api.messages.calls


@pytest.mark.parametrize('change',['actor','message','thread','space'])
def test_seal_rejects_changed_event_identity(chat,change):
    inbound=event();advance(chat,inbound)
    with bind(chat,inbound) as scope:
        if change=='actor':inbound.source.user_id='stranger@example.com'
        elif change=='message':inbound.message_id=SPACE+'/messages/other'
        elif change=='thread':inbound.source.thread_id=OTHER
        else:inbound.source.chat_id='spaces/foreign'
        scope.seal(inbound,MARKER)
        assert scope.revoked


def test_superseded_generation_cannot_send_or_borrow_business_job(chat):
    inbound=event();advance(chat,inbound)
    async def run():
        with bind(chat,inbound) as scope:
            scope.seal(inbound,MARKER)
            advance(chat,event(text='Create a policy'))
            chat._active_chat_job[SPACE]='new-business-job'
            return await chat.send(SPACE,MARKER,reply_to=inbound.message_id,metadata={'thread_id':THREAD})
    assert not execute(run()).success
    assert not chat._chat_api.messages.calls


def test_progress_unsealed_and_arbitrary_final_text_stay_blocked(chat):
    inbound=event();advance(chat,inbound)
    async def run():
        with bind(chat,inbound) as scope:
            assert not (await chat.send(SPACE,'Working...',reply_to=inbound.message_id,metadata={'thread_id':THREAD})).success
            scope.seal(inbound,MARKER)
            assert not (await chat.send(SPACE,'Other text',reply_to=inbound.message_id,metadata={'thread_id':THREAD})).success
    execute(run());assert not chat._chat_api.messages.calls


@pytest.mark.parametrize('text',['Policy successfully created.','The note was filed.','I sent the email.','The payment was processed.'])
def test_approved_freeform_risk_is_not_a_keyword_truth_filter(chat,text):
    inbound=event();advance(chat,inbound)
    with bind(chat,inbound) as scope:
        scope.seal(inbound,text)
        assert not scope.revoked and scope.final_text == text


def test_generation_checked_again_at_actual_post(chat):
    inbound=event();advance(chat,inbound)
    async def run():
        with bind(chat,inbound) as scope:
            scope.seal(inbound,MARKER)
            async def before_call(fn,**kwargs):
                advance(chat,event())
                return fn()
            with patch.object(chat,'_call_with_retry',before_call):
                return await chat.send(SPACE,MARKER,reply_to=inbound.message_id,metadata={'thread_id':THREAD})
    assert not execute(run()).success
    assert not chat._chat_api.messages.calls


def test_tool_call_is_refused_before_execution_and_revokes_reply(chat):
    from robie_job_engine import turn_finalization as guard
    class Agent:
        def _emit_interim_assistant_message(self,message):pass
        def _execute_tool_calls(self,*args,**kwargs):raise AssertionError('tool must never execute')
    with patch.object(guard,'_agent_class',return_value=Agent),patch.object(guard,'_INSTALLED',False):
        guard.install_tool_call_text_guard()
        inbound=event();advance(chat,inbound)
        with bind(chat,inbound) as scope:
            with pytest.raises(RuntimeError,match='cannot dispatch tools'):
                Agent()._execute_tool_calls([])
            scope.seal(inbound,MARKER)
            assert scope.revoked


def test_gateway_return_seals_only_final_text_in_background_context(chat):
    module=_adapter_module();inbound=event();advance(chat,inbound)
    async def handler(event):
        assert current_reply() is not None
        assert not (await chat.send(SPACE,'Thinking...',reply_to=event.message_id,metadata={'thread_id':THREAD})).success
        return MARKER
    async def run():
        with patch.object(module.BasePlatformAdapter,'set_message_handler',lambda self,h:setattr(self,'_message_handler',h),create=True):
            chat.set_message_handler(handler)
        async def background():
            response=await chat._message_handler(inbound)
            return await chat.send(SPACE,response,reply_to=inbound.message_id,metadata={'thread_id':THREAD})
        with bind(chat,inbound):
            task=asyncio.create_task(background())
        assert current_reply() is None
        return await task
    assert execute(run()).success
    assert len(chat._chat_api.messages.calls)==1

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket
    def denied(*args,**kwargs):raise AssertionError('No network is permitted in this suite')
    monkeypatch.setattr(socket,'create_connection',denied)
    monkeypatch.setattr(socket.socket,'connect',denied)


def test_capability_cannot_be_reused_by_another_adapter_or_message(chat):
    inbound=event();advance(chat,inbound)
    with bind(chat,inbound) as scope:
        scope.seal(inbound,MARKER)
        assert not scope.permits(object(),SPACE,MARKER,inbound.message_id,{'thread_id':THREAD})
        assert not scope.permits(chat,SPACE,MARKER,SPACE+'/messages/other',{'thread_id':THREAD})
        assert not scope.permits(chat,'spaces/foreign',MARKER,inbound.message_id,{'thread_id':THREAD})


@pytest.mark.parametrize('cancel',[False,True])
def test_callback_error_and_cancellation_revoke_scope(chat,cancel):
    module=_adapter_module();inbound=event();advance(chat,inbound)
    async def handler(_event):
        if cancel:raise asyncio.CancelledError()
        raise ValueError('synthetic failure')
    async def run():
        with patch.object(module.BasePlatformAdapter,'set_message_handler',lambda self,h:setattr(self,'_message_handler',h),create=True):
            chat.set_message_handler(handler)
        with bind(chat,inbound) as scope:
            with pytest.raises(asyncio.CancelledError if cancel else ValueError):
                await chat._message_handler(inbound)
            assert scope.revoked and scope.final_text is None
            assert not (await chat.send(SPACE,MARKER,reply_to=inbound.message_id,metadata={'thread_id':THREAD})).success
    execute(run());assert not chat._chat_api.messages.calls


def test_concurrent_callbacks_do_not_borrow_actor_or_generation(chat):
    first=event();advance(chat,first)
    async def run():
        release=asyncio.Event()
        async def old():
            with bind(chat,first) as scope:
                await release.wait()
                scope.seal(first,'old response')
                return await chat.send(SPACE,'old response',reply_to=first.message_id,metadata={'thread_id':THREAD})
        task=asyncio.create_task(old())
        await asyncio.sleep(0)
        newer=event(actor='jake@streetsmart.insurance');newer.message_id=SPACE+'/messages/newer'
        advance(chat,newer)
        with bind(chat,newer) as scope:
            scope.seal(newer,MARKER)
            result=await chat.send(SPACE,MARKER,reply_to=newer.message_id,metadata={'thread_id':THREAD})
        release.set()
        assert not (await task).success
        return result
    assert execute(run()).success
    assert len(chat._chat_api.messages.calls)==1
    assert chat._chat_api.messages.calls[0]['body']['text']==MARKER


def test_independent_thread_callback_cannot_redirect_other_lane(chat):
    first=event();advance(chat,first)
    second=event(thread=OTHER);second.message_id=SPACE+'/messages/other';advance(chat,second)
    with bind(chat,first) as scope:
        scope.seal(first,MARKER)
        assert scope.current()
        assert not scope.permits(chat,SPACE,MARKER,second.message_id,{'thread_id':OTHER})


def test_duplicate_completed_callback_cannot_reseal(chat):
    inbound=event();advance(chat,inbound)
    with bind(chat,inbound) as scope:
        scope.seal(inbound,MARKER)
        scope.seal(inbound,'different answer')
        assert scope.revoked


def test_accepted_send_uncertainty_retry_reads_same_server_message(chat):
    from test_chat_reply_recovery import Messages
    messages=Messages();messages.accept_then_timeout=True;messages.collide=True
    chat._chat_api.messages=messages;chat._chat_api._spaces._messages=messages
    inbound=event();advance(chat,inbound)
    async def run():
        with bind(chat,inbound) as scope:
            scope.seal(inbound,MARKER)
            first=await chat.send(SPACE,MARKER,reply_to=inbound.message_id,metadata={'thread_id':THREAD})
            assert first.success and scope.consumed
            assert len(messages.accepted)==1
            second=await chat.send(SPACE,MARKER,reply_to=inbound.message_id,metadata={'thread_id':THREAD})
            assert not second.success
    execute(run())
    assert len(messages.calls)==2 and len(messages.accepted)==1
    assert messages.calls[0]['messageId']==messages.calls[1]['messageId']
    assert next(iter(messages.accepted.values()))['text']==MARKER


def test_replay_after_restart_uses_same_id_but_cannot_change_answer(chat):
    from test_chat_reply_recovery import Messages
    messages=Messages();messages.collide=True
    chat._chat_api.messages=messages;chat._chat_api._spaces._messages=messages
    async def run(target,text):
        inbound=event();advance(target,inbound)
        with bind(target,inbound) as scope:
            scope.seal(inbound,text)
            return await target.send(SPACE,text,reply_to=inbound.message_id,metadata={'thread_id':THREAD})
    assert execute(run(chat,MARKER)).success
    restarted=_chat(chat._db)
    restarted._chat_api.messages=messages;restarted._chat_api._spaces._messages=messages
    assert not hasattr(restarted,'_conversation_reply_generations')
    assert execute(run(restarted,MARKER)).success
    assert not execute(run(restarted,'different answer')).success
    assert len(messages.accepted)==1


def test_tool_guard_survives_gateway_thread_context_copy(chat):
    from contextvars import copy_context
    from robie_job_engine import turn_finalization as guard
    class Agent:
        def _emit_interim_assistant_message(self,message):pass
        def _execute_tool_calls(self,*args,**kwargs):raise AssertionError('tool ran')
    async def run():
        with patch.object(guard,'_agent_class',return_value=Agent),patch.object(guard,'_INSTALLED',False):
            guard.install_tool_call_text_guard()
            inbound=event();advance(chat,inbound)
            with bind(chat,inbound) as scope:
                ctx=copy_context()
                with pytest.raises(RuntimeError,match='cannot dispatch tools'):
                    await asyncio.get_running_loop().run_in_executor(None,ctx.run,Agent()._execute_tool_calls,[])
                assert scope.revoked
    execute(run())


def test_exhausted_uncertain_send_retains_same_id_for_safe_retry(chat):
    from test_chat_reply_recovery import Messages
    messages=Messages();messages.accept_then_timeout=True;messages.collide=True
    chat._chat_api.messages=messages;chat._chat_api._spaces._messages=messages
    inbound=event();advance(chat,inbound)
    async def run():
        with bind(chat,inbound) as scope:
            scope.seal(inbound,MARKER)
            with patch.object(_adapter_module(),'_RETRY_MAX_ATTEMPTS',1):
                assert not (await chat.send(SPACE,MARKER,reply_to=inbound.message_id,metadata={'thread_id':THREAD})).success
            assert not scope.consumed
            with patch.object(messages,'get',wraps=messages.get) as readback:
                assert (await chat.send(SPACE,MARKER,reply_to=inbound.message_id,metadata={'thread_id':THREAD})).success
                readback.assert_called_once_with(name=SPACE+'/messages/client-'+scope.request_id)
    execute(run());assert len(messages.accepted)==1


def test_supersession_during_uncertain_post_refuses_additional_attempt(chat):
    from test_chat_reply_recovery import Messages
    messages=Messages();messages.accept_then_timeout=True
    messages.on_accept=lambda:advance(chat,event(text='Create a policy'))
    chat._chat_api.messages=messages;chat._chat_api._spaces._messages=messages
    inbound=event();advance(chat,inbound)
    async def run():
        with bind(chat,inbound) as scope:
            scope.seal(inbound,MARKER)
            return await chat.send(SPACE,MARKER,reply_to=inbound.message_id,metadata={'thread_id':THREAD})
    assert not execute(run()).success
    # An already accepted HTTP write cannot be revoked; no later POST is made.
    assert len(messages.accepted)==1 and len(messages.calls)==1


def test_cancelled_delivery_revokes_capability(chat):
    inbound=event();advance(chat,inbound)
    async def run():
        with bind(chat,inbound) as scope:
            scope.seal(inbound,MARKER)
            async def cancelled(*args,**kwargs):raise asyncio.CancelledError()
            with patch.object(chat,'_create_message',cancelled):
                with pytest.raises(asyncio.CancelledError):
                    await chat.send(SPACE,MARKER,reply_to=inbound.message_id,metadata={'thread_id':THREAD})
            assert scope.revoked
            assert not (await chat.send(SPACE,MARKER,reply_to=inbound.message_id,metadata={'thread_id':THREAD})).success
    execute(run());assert not chat._chat_api.messages.calls


def test_forged_actor_metadata_refused(chat):
    inbound=event();advance(chat,inbound)
    with bind(chat,inbound) as scope:
        scope.seal(inbound,MARKER)
        assert not scope.permits(chat,SPACE,MARKER,inbound.message_id,{'thread_id':THREAD,'actor':'stranger@example.com'})

@pytest.mark.parametrize('invalid',[
    'operational','attachment','bot','actor','missing_generation','foreign_thread',
    'missing_group_thread','unidentified_message','foreign_message',
])
def test_trusted_no_job_ingress_boundary_is_required(chat,invalid):
    module=_adapter_module();inbound=event();advance(chat,inbound)
    count=0
    if invalid=='operational':inbound.text='Create a policy in EZLynx'
    elif invalid=='attachment':count=1
    elif invalid=='bot':inbound.raw_message['sender']['type']='BOT'
    elif invalid=='actor':inbound.raw_message['sender']['email']='stranger@example.com'
    elif invalid=='missing_generation':del inbound._robie_conversation_generation
    elif invalid=='foreign_thread':inbound.source.thread_id='spaces/foreign/threads/test'
    elif invalid=='missing_group_thread':inbound.source.thread_id=None;inbound.source.chat_type='group'
    elif invalid=='unidentified_message':inbound.message_id='unidentified:123'
    elif invalid=='foreign_message':inbound.message_id='spaces/foreign/messages/inbound'
    async def forbidden(_event):raise AssertionError('untrusted turn dispatched')
    with patch.object(chat,'handle_message',forbidden,create=True),patch.object(chat,'_chat_job_attachment_kwargs',return_value={'expected_attachment_count':count}),patch.dict('os.environ',{'GOOGLE_CHAT_ALLOWED_USERS':ACTOR}):
        execute(chat._handle_conversation_only(inbound))
    assert not chat._chat_api.messages.calls


def test_trusted_no_job_dispatch_requires_installed_tool_guard(chat):
    from robie_job_engine import turn_finalization as guard
    inbound=event();advance(chat,inbound)
    async def forbidden(_event):raise AssertionError('unguarded turn dispatched')
    with patch.object(chat,'handle_message',forbidden,create=True),patch.object(chat,'_chat_job_attachment_kwargs',return_value={'expected_attachment_count':0}),patch.object(guard,'install_tool_call_text_guard'),patch.object(guard,'_agent_class',return_value=SimpleNamespace(_execute_tool_calls=lambda:None)),patch.dict('os.environ',{'GOOGLE_CHAT_ALLOWED_USERS':ACTOR}):
        execute(chat._handle_conversation_only(inbound))


def test_trusted_no_job_dispatch_creates_internal_context(chat):
    from robie_job_engine import turn_finalization as guard
    inbound=event();advance(chat,inbound)
    def execute_tools():pass
    execute_tools._robie_conversation_tool_guard=True
    seen=[]
    async def handled(_event):
        scope=current_reply();seen.append(scope)
        assert scope.actor==ACTOR and scope.message==inbound.message_id and scope.final_text is None
    with patch.object(chat,'handle_message',handled,create=True),patch.object(chat,'_chat_job_attachment_kwargs',return_value={'expected_attachment_count':0}),patch.object(guard,'install_tool_call_text_guard'),patch.object(guard,'_agent_class',return_value=SimpleNamespace(_execute_tool_calls=execute_tools)),patch.dict('os.environ',{'GOOGLE_CHAT_ALLOWED_USERS':ACTOR}):
        execute(chat._handle_conversation_only(inbound))
    assert len(seen)==1 and current_reply() is None


def test_conversation_tool_refusal_does_not_clear_business_tool_guard(chat):
    from robie_job_engine import turn_finalization as guard
    class Agent:
        def _emit_interim_assistant_message(self,message):raise AssertionError('tool narration emitted')
        def _execute_tool_calls(self,*args,**kwargs):raise AssertionError('tool executed')
    with patch.object(guard,'_agent_class',return_value=Agent),patch.object(guard,'_INSTALLED',False),patch.dict('os.environ',{'ROBIE_CURRENT_JOB_ID':'unrelated-business-job'}):
        guard.install_tool_call_text_guard()
        guard.begin_tool_call_message('unrelated-business-job')
        try:
            inbound=event();advance(chat,inbound)
            with bind(chat,inbound):
                with pytest.raises(RuntimeError):Agent()._emit_interim_assistant_message({'tool_calls':[{}],'content':'Working...'})
                with pytest.raises(RuntimeError):Agent()._execute_tool_calls([])
            assert guard.tool_call_message_is_open('unrelated-business-job')
        finally:
            guard.clear_tool_call_text('unrelated-business-job')

@pytest.mark.parametrize('receipt',[None,'spaces/foreign/messages/out'])
def test_unusable_http_receipt_does_not_claim_delivery(chat,receipt):
    inbound=event();advance(chat,inbound)
    async def run():
        with bind(chat,inbound) as scope:
            scope.seal(inbound,MARKER)
            async def invalid_receipt(*args,**kwargs):return _adapter_module().SendResult(success=True,message_id=receipt)
            with patch.object(chat,'_create_message',invalid_receipt):
                result=await chat.send(SPACE,MARKER,reply_to=inbound.message_id,metadata={'thread_id':THREAD})
            assert not scope.consumed
            return result
    assert not execute(run()).success
