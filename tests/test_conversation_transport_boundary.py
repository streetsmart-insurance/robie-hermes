"""Deterministic corrections from independent review; no semantic safety claim."""
import inspect
import pytest
from test_conversation_reply import chat, no_network, event, execute, MARKER, SPACE, THREAD, OTHER
from robie_job_engine.conversation_reply import advance, bind
from test_chat_reply_recovery import Messages

NON_TEXT_PATHS = (
    'send_image','send_image_file','send_document','send_voice','send_video',
    'send_animation','send_card','send_clarify','edit_message','delete_message',
    'send_typing','stop_typing','_send_file','_post_attachment_fallback',
    '_patch_message','_consume_typing_card_with_text','post_outcome_sync',
    '_post_stop_confirmation_now','_retire_typing_card_now','_retire_suppressed_typing_card',
)

@pytest.mark.parametrize('name',NON_TEXT_PATHS)
@pytest.mark.parametrize('state',('unsealed','sealed','revoked','superseded'))
def test_every_media_status_edit_delete_path_is_refused_in_conversation_scope(chat,name,state):
    inbound=event();advance(chat,inbound)
    async def run():
        with bind(chat,inbound) as scope:
            if state!='unsealed':scope.seal(inbound,MARKER)
            if state=='revoked':scope.revoked=True
            if state=='superseded':advance(chat,event())
            method=getattr(chat,name)
            args={k:None for k,p in inspect.signature(method).parameters.items() if p.default is p.empty and p.kind not in (p.VAR_KEYWORD,p.VAR_POSITIONAL)}
            for key in ('chat_id','space'):
                if key in args:args[key]=SPACE
            if 'metadata' in inspect.signature(method).parameters:args['metadata']={'thread_id':OTHER}
            result=method(**args)
            if inspect.isawaitable(result):result=await result
            assert result is None or result is False or not result.success
    execute(run());assert not chat._chat_api.messages.calls


def test_revoked_media_review_reproduction_is_closed(chat):
    inbound=event();advance(chat,inbound)
    async def run():
        with bind(chat,inbound) as scope:
            scope.seal(inbound,'![diagram](https://example.com/image.png)');scope.revoked=True
            return await chat.send_image(SPACE,'https://example.com/image.png',reply_to=inbound.message_id,metadata={'thread_id':OTHER})
    assert not execute(run()).success and not chat._chat_api.messages.calls


def test_request_id_success_replay_must_match_previous_exact_body(chat):
    messages=Messages();chat._chat_api.messages=messages;chat._chat_api._spaces._messages=messages
    async def run(text):
        inbound=event();advance(chat,inbound)
        with bind(chat,inbound) as scope:
            scope.seal(inbound,text)
            result=await chat.send(SPACE,text,reply_to=inbound.message_id,metadata={'thread_id':THREAD})
            assert scope.consumed==result.success
            return result
    assert execute(run('first answer')).success
    assert not execute(run('different answer')).success
    assert len(messages.accepted)==1
    assert next(iter(messages.accepted.values()))['text']=='first answer'

@pytest.mark.parametrize('field',('name','text','thread'))
def test_normal_success_response_validates_identity_text_and_thread(chat,field):
    messages=Messages();original=messages.create
    def wrong(**kwargs):
        request=original(**kwargs);execute_original=request.execute
        def response(http=None):
            body=dict(execute_original(http=http))
            if field=='name':body['name']='spaces/foreign/messages/foreign-id'
            elif field=='text':body['text']='other answer'
            else:body['thread']={'name':OTHER}
            messages.accepted[kwargs['parent']+'/messages/'+kwargs['messageId']]=body
            return body
        request.execute=response;return request
    messages.create=wrong;chat._chat_api.messages=messages;chat._chat_api._spaces._messages=messages
    inbound=event();advance(chat,inbound)
    async def run():
        with bind(chat,inbound) as scope:
            scope.seal(inbound,MARKER)
            result=await chat.send(SPACE,MARKER,reply_to=inbound.message_id,metadata={'thread_id':THREAD})
            assert not scope.consumed
            return result
    assert not execute(run()).success


def test_canonical_server_name_is_distinct_from_client_alias_and_read_back(chat):
    messages=Messages();original=messages.create;reads=[]
    def create(**kwargs):
        result=original(**kwargs);execute_original=result.execute
        def stored(http=None):
            data=execute_original(http=http)
            data['name']=SPACE+'/messages/server123.server456'
            data['clientAssignedMessageId']=kwargs['messageId']
            return data
        result.execute=stored;return result
    original_get=messages.get
    def get(name):reads.append(name);return original_get(name)
    messages.create=create;messages.get=get
    chat._chat_api.messages=messages;chat._chat_api._spaces._messages=messages
    inbound=event();advance(chat,inbound)
    async def run():
        with bind(chat,inbound) as scope:
            scope.seal(inbound,MARKER)
            result=await chat.send(SPACE,MARKER,reply_to=inbound.message_id,metadata={'thread_id':THREAD})
            assert scope.consumed
            assert reads==[SPACE+'/messages/client-'+scope.request_id]
            return result
    assert execute(run()).message_id==SPACE+'/messages/server123.server456'


def test_success_replay_echoes_new_request_but_stored_old_body_is_rejected(chat):
    messages=Messages();original=messages.create
    def create(**kwargs):
        result=original(**kwargs);execute_original=result.execute
        def echo(http=None):
            actual=execute_original(http=http)
            return {'name':actual['name'],'text':kwargs['body']['text'],'thread':kwargs['body']['thread']}
        result.execute=echo;return result
    messages.create=create;chat._chat_api.messages=messages;chat._chat_api._spaces._messages=messages
    async def run(text):
        inbound=event();advance(chat,inbound)
        with bind(chat,inbound) as scope:
            scope.seal(inbound,text)
            return await chat.send(SPACE,text,reply_to=inbound.message_id,metadata={'thread_id':THREAD})
    assert execute(run('first answer')).success
    assert not execute(run('different answer')).success
    assert next(iter(messages.accepted.values()))['text']=='first answer'


def test_failed_readback_does_not_consume_or_claim_accepted_post(chat):
    messages=Messages()
    def missing(name):raise TimeoutError('synthetic readback unavailable')
    messages.get=missing;chat._chat_api.messages=messages;chat._chat_api._spaces._messages=messages
    inbound=event();advance(chat,inbound)
    async def run():
        with bind(chat,inbound) as scope:
            scope.seal(inbound,MARKER)
            from unittest.mock import patch
            with patch.object(__import__('integrations.google_chat.adapter',fromlist=['_RETRY_MAX_ATTEMPTS']),'_RETRY_MAX_ATTEMPTS',1):
                result=await chat.send(SPACE,MARKER,reply_to=inbound.message_id,metadata={'thread_id':THREAD})
            assert not scope.consumed and len(messages.accepted)==1
            return result
    assert not execute(run()).success


def test_success_readback_client_alias_mismatch_is_not_delivery(chat):
    messages=Messages();original_get=messages.get
    def get(name):
        request=original_get(name);execute_original=request.execute
        def wrong(http=None):
            body=dict(execute_original(http=http));body['clientAssignedMessageId']='client-foreign';return body
        request.execute=wrong;return request
    messages.get=get;chat._chat_api.messages=messages;chat._chat_api._spaces._messages=messages
    inbound=event();advance(chat,inbound)
    async def run():
        with bind(chat,inbound) as scope:
            scope.seal(inbound,MARKER)
            return await chat.send(SPACE,MARKER,reply_to=inbound.message_id,metadata={'thread_id':THREAD})
    assert not execute(run()).success


def test_create_and_readback_canonical_identity_must_agree(chat):
    messages=Messages();original_get=messages.get
    def get(name):
        request=original_get(name);execute_original=request.execute
        def wrong(http=None):
            body=dict(execute_original(http=http));body['name']=SPACE+'/messages/other-server-id';return body
        request.execute=wrong;return request
    messages.get=get;chat._chat_api.messages=messages;chat._chat_api._spaces._messages=messages
    inbound=event();advance(chat,inbound)
    async def run():
        with bind(chat,inbound) as scope:
            scope.seal(inbound,MARKER)
            return await chat.send(SPACE,MARKER,reply_to=inbound.message_id,metadata={'thread_id':THREAD})
    assert not execute(run()).success
