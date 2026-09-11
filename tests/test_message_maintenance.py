import unittest
from pathlib import Path
from durable_temp import durable_temporary_directory
from robie_job_engine.chat_guard import require_message_execution_available
from robie_job_engine.runs import IsolatedRunStore, RunIsolationError

class MessageMaintenanceTests(unittest.TestCase):
    def test_chat_receipt_survives_but_execution_is_fenced_until_configuration_finishes(self):
        with durable_temporary_directory() as tmp:
            db=str(Path(tmp)/'jobs.db'); runs=IsolatedRunStore(db)
            active=runs.start(owner='message-runtime-configuration',job_id='configuration',lease_seconds=1800)
            receipt=runs.record_intake(owner='google-chat-intake',job_id='new-job',payload={'message_id':'new'})
            self.assertEqual(receipt['status'],'INTAKE')
            with self.assertRaises(RunIsolationError):
                require_message_execution_available(db)
            runs.terminate(active['id'],'COMPLETE')
            require_message_execution_available(db)
    def test_adapter_checks_fence_before_calling_hermes(self):
        import ast
        import asyncio
        from unittest.mock import AsyncMock, patch
        source=ast.parse((Path(__file__).resolve().parents[1]/'integrations/google_chat/adapter.py').read_text())
        method=next(n for n in ast.walk(source) if isinstance(n,ast.AsyncFunctionDef) and n.name=='_run_generic_chat_job')
        method.args.args[1].annotation=None; method.args.args[2].annotation=None; method.returns=None
        ns={'asyncio':asyncio,'ROBIE_JOB_DB':'unused'}
        exec(compile(ast.Module(body=[method],type_ignores=[]),'adapter','exec'),ns)
        adapter=type('Adapter',(),{'handle_message':AsyncMock(),'_maintain_generic_chat_job_heartbeat':AsyncMock()})()
        with patch('robie_job_engine.chat_guard.require_message_execution_available',side_effect=RunIsolationError('maintenance')):
            with self.assertRaises(RunIsolationError):
                asyncio.run(ns['_run_generic_chat_job'](adapter,'job',object()))
        adapter.handle_message.assert_not_called()
    def test_durable_adapter_loop_survives_eight_maintenance_polls_then_executes_once(self):
        import ast
        import asyncio
        import logging
        import os
        from datetime import datetime, timedelta, timezone
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock, patch
        from robie_job_engine.chat_queue import DurableChatEventQueue
        from robie_job_engine.models import JobStatus
        from robie_job_engine.runs import MessageMaintenanceDeferred
        source=ast.parse((Path(__file__).resolve().parents[1]/'integrations/google_chat/adapter.py').read_text())
        method=next(n for n in ast.walk(source) if isinstance(n,ast.AsyncFunctionDef) and n.name=='_drain_chat_queue')
        with durable_temporary_directory() as tmp:
            queue=DurableChatEventQueue(Path(tmp)/'jobs.db')
            queue.enqueue(event_id='event',conversation_id='space',message_id='message',payload={'job_id':'job','conversation_id':'space'})
            adapter=SimpleNamespace(_shutting_down=False,_chat_queue_worker_id='worker',_durable_chat_queue=lambda:queue,_chat_queue_wakeup=None)
            async def heartbeat(*args):
                await asyncio.Event().wait()
            async def stop(task):
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            adapter._maintain_chat_queue_lease=heartbeat
            adapter._stop_chat_queue_heartbeat=stop
            adapter.send=AsyncMock(return_value=SimpleNamespace(success=True))
            adapter._fail_queued_job=Mock(side_effect=AssertionError('must not fail queued job'))
            original_defer=queue.defer_until_available
            def deferred(event,owner,**kwargs):
                kwargs['available_at']='2000-01-01T00:00:00+00:00'
                return original_defer(event,owner,**kwargs)
            queue.defer_until_available=deferred
            original_complete=queue.complete
            def completed(*args):
                result=original_complete(*args); adapter._shutting_down=True; return result
            queue.complete=completed
            execute=Mock(return_value=True)
            store=SimpleNamespace(wake_due=lambda:None,get_job=lambda j:{'status':'COMPLETE','action_type':'browser.read'})
            ns={'asyncio':asyncio,'os':os,'datetime':datetime,'timedelta':timedelta,'timezone':timezone,'ROBIE_JOB_DB':str(Path(tmp)/'jobs.db'),'JobStore':lambda p:store,'JobStatus':JobStatus,'maybe_run_bounded_job':execute,'guard_chat_response':lambda *a:'verified result','MessageMaintenanceDeferred':MessageMaintenanceDeferred,'logger':logging.getLogger('test'),'redact_text':str}
            exec(compile(ast.Module(body=[method],type_ignores=[]),'adapter','exec'),ns)
            with patch('robie_job_engine.chat_guard.require_message_execution_available',side_effect=[MessageMaintenanceDeferred('maintenance') for _ in range(8)]+[None]):
                asyncio.run(asyncio.wait_for(ns['_drain_chat_queue'](adapter),timeout=5))
            execute.assert_called_once()
            self.assertEqual(queue.get('event')['state'],'COMPLETE')
            self.assertEqual(queue.get('event')['attempt_count'],1)
