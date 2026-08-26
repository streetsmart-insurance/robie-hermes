from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable, Protocol


class Deduplicator(Protocol):
    def contains(self, message_id: str) -> bool: ...
    def is_duplicate(self, message_id: str) -> bool: ...
    def discard(self, message_id: str) -> None: ...


ErrorHandler = Callable[[BaseException], None]


class PubSubAckCoordinator:
    """Settle Pub/Sub only after the asynchronous handoff has succeeded."""

    def __init__(self, deduplicator: Deduplicator):
        self._dedup = deduplicator
        self._inflight: dict[str, list[Any]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _close_coroutine(coro: Any) -> None:
        if asyncio.iscoroutine(coro):
            coro.close()

    def _claim(self, message_id: str, message: Any) -> str:
        if not message_id:
            return "new"
        with self._lock:
            if self._dedup.contains(message_id):
                return "completed"
            deliveries = self._inflight.get(message_id)
            if deliveries is not None:
                deliveries.append(message)
                return "inflight"
            self._inflight[message_id] = [message]
            return "new"

    def _settle(
        self,
        *,
        message_id: str,
        fallback_message: Any,
        succeeded: bool,
        on_error: ErrorHandler | None,
    ) -> None:
        if message_id:
            with self._lock:
                deliveries = self._inflight.pop(message_id, [fallback_message])
                if succeeded:
                    self._dedup.is_duplicate(message_id)
                else:
                    self._dedup.discard(message_id)
        else:
            deliveries = [fallback_message]

        method = "ack" if succeeded else "nack"
        for delivery in deliveries:
            try:
                getattr(delivery, method)()
            except BaseException as exc:
                if on_error is not None:
                    on_error(exc)

    def schedule(
        self,
        *,
        coro: Any,
        message: Any,
        message_id: str,
        submit: Callable[[Any], Any],
        on_error: ErrorHandler | None = None,
    ) -> None:
        claim = self._claim(message_id, message)
        if claim == "completed":
            self._close_coroutine(coro)
            message.ack()
            return
        if claim == "inflight":
            self._close_coroutine(coro)
            return

        try:
            future = submit(coro)
        except BaseException as exc:
            self._close_coroutine(coro)
            if on_error is not None:
                on_error(exc)
            self._settle(
                message_id=message_id,
                fallback_message=message,
                succeeded=False,
                on_error=on_error,
            )
            return

        if future is None:
            self._settle(
                message_id=message_id,
                fallback_message=message,
                succeeded=False,
                on_error=on_error,
            )
            return

        def done(completed: Any) -> None:
            succeeded = False
            try:
                completed.result()
                succeeded = True
            except BaseException as exc:
                if on_error is not None:
                    on_error(exc)
            self._settle(
                message_id=message_id,
                fallback_message=message,
                succeeded=succeeded,
                on_error=on_error,
            )

        future.add_done_callback(done)

