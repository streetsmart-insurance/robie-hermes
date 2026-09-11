import unittest
from pathlib import Path
from durable_temp import durable_temporary_directory
from robie_job_engine.runs import IsolatedRunStore, RunIsolationError


class IntakeExecutionIsolationTests(unittest.TestCase):
    def test_intake_during_active_execution_preserves_worker_and_receipt(self):
        with durable_temporary_directory() as tmp:
            runs = IsolatedRunStore(Path(tmp) / 'jobs.db')
            active = runs.start(owner='email-worker', job_id='email-job')
            receipt = runs.record_intake(owner='chat-intake', job_id='chat-job', payload={'message_id': 'm1'})
            self.assertEqual(receipt['status'], 'INTAKE')
            self.assertEqual(runs.bindings(receipt['id'], 'intake')[0]['payload']['message_id'], 'm1')
            self.assertEqual(runs.active_run()['id'], active['id'])
            with self.assertRaises(RunIsolationError):
                runs.start(owner='second-worker', job_id='chat-job')
            runs.terminate(active['id'], 'COMPLETE')
            self.assertEqual(runs.start(owner='chat-worker', job_id='chat-job')['status'], 'ACTIVE')
