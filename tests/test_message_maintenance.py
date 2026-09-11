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
