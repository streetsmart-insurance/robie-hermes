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
    """Settle Pub/Sub only after the asynchronous handoff has succeeded.

    FlowControl on hermes-gateway defaults to ``max_messages=1``. An
    unsettled delivery holds the streaming-pull lease: the process stays
    up, journalctl goes silent, and a later RETRY never reaches Job
    Engine. Settlement must therefore run before error logging, and no
    exception from the handoff or the logger may escape the done callback.
    """

    def __init__(self, deduplicator: Deduplicator):
        self._dedup = deduplicator
        self._inflight: dict[str, list[Any]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _close_coroutine(coro: Any) -> None:
        if asyncio.iscoroutine(coro):
            coro.close()

    @staticmethod
    def _invoke_on_error(
        on_error: ErrorHandler | None, exc: BaseException
    ) -> None:
        """Report a failure without ever preventing ack/nack."""
        if on_error is None:
            return
        try:
            on_error(exc)
        except BaseException:
            return

    @staticmethod
    def _last_ditch_nack(message: Any) -> None:
        try:
            message.nack()
        except BaseException:
            return

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
        deliveries = [fallback_message]
        ledger_error: BaseException | None = None
        try:
            if message_id:
                with self._lock:
                    deliveries = self._inflight.pop(message_id, [fallback_message])
                    if succeeded:
                        self._dedup.is_duplicate(message_id)
                    else:
                        self._dedup.discard(message_id)
        except BaseException as exc:
            ledger_error = exc
            succeeded = False
            deliveries = [fallback_message]

        method = "ack" if succeeded else "nack"
        nacked = False
        for delivery in deliveries:
            try:
                getattr(delivery, method)()
                if method == "nack":
                    nacked = True
            except BaseException as exc:
                self._invoke_on_error(on_error, exc)
        if not succeeded and not nacked:
            self._last_ditch_nack(fallback_message)
        if ledger_error is not None:
            self._invoke_on_error(on_error, ledger_error)

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
            try:
                message.ack()
            except BaseException as exc:
                self._invoke_on_error(on_error, exc)
            return
        if claim == "inflight":
            self._close_coroutine(coro)
            return

        try:
            future = submit(coro)
        except BaseException as exc:
            self._close_coroutine(coro)
            self._settle(
                message_id=message_id,
                fallback_message=message,
                succeeded=False,
                on_error=None,
            )
            self._invoke_on_error(on_error, exc)
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
            # Production 2026-08-27: resume_human_input raised
            # RuntimeError inside this callback (pubsub_ack.py done →
            # chat_queue.resume_human_input). Logging that failure
            # before nack, or letting on_error raise, held the
            # max_messages=1 lease. hermes-gateway stayed active and
            # journalctl went silent; a later RETRY never landed.
            succeeded = False
            error: BaseException | None = None
            try:
                try:
                    completed.result()
                    succeeded = True
                except BaseException as exc:
                    error = exc
                self._settle(
                    message_id=message_id,
                    fallback_message=message,
                    succeeded=succeeded,
                    on_error=on_error,
                )
            except BaseException as settle_exc:
                self._last_ditch_nack(message)
                self._invoke_on_error(on_error, settle_exc)
            if error is not None:
                self._invoke_on_error(on_error, error)

        try:
            future.add_done_callback(done)
        except BaseException as exc:
            self._settle(
                message_id=message_id,
                fallback_message=message,
                succeeded=False,
                on_error=None,
            )
            self._invoke_on_error(on_error, exc)
