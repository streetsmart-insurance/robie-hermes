"""Internal authority for one tool-free reply to a trusted no-job Chat turn.

Nothing in inbound text or outbound metadata grants this authority. The adapter
creates it after ingress/actor/routing checks; the gateway handler's return seals
its exact final text. It never creates/finalizes a business Job or verifies writes.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import uuid

@dataclass
class ConversationReply:
    adapter: object
    generations: dict
    lane: tuple[str, str]
    generation: str
    actor: str
    message: str
    final_text: str | None = None
    revoked: bool = False
    consumed: bool = False

    def current(self) -> bool:
        return not self.revoked and self.generations.get(self.lane) == self.generation

    def seal(self, event, response) -> None:
        source = getattr(event, 'source', None)
        if (not self.current() or self.final_text is not None or self.consumed or not isinstance(response, str)
            or str(getattr(source, 'user_id', '') or '') != self.actor
            or str(getattr(source, 'chat_id', '') or '') != self.lane[0]
            or str(getattr(source, 'thread_id', '') or '') != self.lane[1]
            or str(getattr(event, 'message_id', '') or '') != self.message
            or not response.strip()):
            self.revoked = True
            return
        self.final_text = response

    def permits(self, adapter, chat, text, reply_to, metadata) -> bool:
        meta = metadata or {}
        return bool(self.current() and not self.consumed and self.adapter is adapter
            and self.final_text is not None and text == self.final_text
            and chat == self.lane[0] and reply_to == self.message
            and str(meta.get('thread_id') or '') == self.lane[1]
            and all(not meta.get(k) or str(meta[k]) == self.actor for k in ('actor','user_id','sender_id'))
            and not any(meta.get(k) for k in ('thread_name','thread_ts','robie_job_id','job_id','robie_delivery_kind','robie_stop_notice')))

    @property
    def request_id(self) -> str:
        return hashlib.sha256(('conversation-reply\0'+self.actor+'\0'+self.message+'\0'+self.lane[1]).encode()).hexdigest()[:32]


_CURRENT = ContextVar('robie_conversation_reply', default=None)

def current_reply() -> ConversationReply | None:
    return _CURRENT.get()


def advance(adapter, event) -> str:
    source = getattr(event, 'source', None)
    lane = (str(getattr(source, 'chat_id', '') or ''), str(getattr(source, 'thread_id', '') or ''))
    if not hasattr(adapter, '_conversation_reply_generations'):
        adapter._conversation_reply_generations = {}
    generation = uuid.uuid4().hex
    adapter._conversation_reply_generations[lane] = generation
    event._robie_conversation_generation = generation
    return generation


@contextmanager
def bind(adapter, event):
    source = event.source
    lane = (str(source.chat_id), str(source.thread_id or ''))
    scope = ConversationReply(adapter, adapter._conversation_reply_generations, lane,
        str(event._robie_conversation_generation), str(source.user_id), str(event.message_id))
    token = _CURRENT.set(scope)
    try:
        yield scope
    finally:
        # Child tasks retain their own context; this dispatch task does not.
        _CURRENT.reset(token)


def refuse_conversation_tools() -> None:
    scope = current_reply()
    if scope is not None:
        scope.revoked = True
        raise RuntimeError('Non-operational Chat turns cannot dispatch tools')
