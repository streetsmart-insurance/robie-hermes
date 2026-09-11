"""Exercise launcher context without importing Gmail clients or sending mail."""
from durable_temp import durable_temporary_directory
import ast
import os
from pathlib import Path
import subprocess
from unittest import TestCase
from unittest.mock import Mock, patch
from robie_job_engine.deploy_truth import CHAT_RUNTIME_FILES, zip_load_shim_source

ROOT=Path(__file__).resolve().parents[1]
class EmailReleaseRuntimeTests(TestCase):
    def test_child_uses_test_paths_and_durable_job_context(self):
        source=ast.parse((ROOT/'scripts/robie_email_agent.py').read_text())
        function=next(n for n in source.body if isinstance(n,ast.FunctionDef) and n.name=='run_agent_task')
        runner=Mock(return_value=Mock(returncode=0,stdout='done'))
        ns={'os':os,'Path':Path,'__file__':str(ROOT/'scripts/robie_email_agent.py'),'_EXPECTED_ENV':'TEST','OPT_ROOT':Path('/opt/streetsmart-hermes-test'),'HERMES_HOME':Path('/opt/streetsmart-hermes-test/.hermes'),'subprocess':subprocess,'clean_hermes_output':lambda s:s,'logger':Mock()}
        exec(compile(ast.Module(body=[function],type_ignores=[]),'launcher','exec'),ns)
        with patch.dict(os.environ,{'ROBIE_ENV':'TEST'},clear=True),patch.object(subprocess,'run',runner):
            self.assertEqual(ns['run_agent_task']('prompt','job-123','/test/jobs.db'),'done')
        args,kwargs=runner.call_args
        self.assertTrue(args[0][0].startswith('/opt/streetsmart-hermes-test/'))
        self.assertEqual(kwargs['cwd'],'/opt/streetsmart-hermes-test')
        self.assertEqual(kwargs['env']['ROBIE_JOB_ID'],'job-123')
        self.assertEqual(kwargs['env']['ROBIE_JOB_DB'],'/test/jobs.db')
        self.assertEqual(kwargs['env']['ROBIE_ENV'],'TEST')
        self.assertTrue(kwargs['env']['PYTHONPATH'].startswith(str(ROOT)))
        runner.return_value.returncode=1
        with patch.object(subprocess,'run',runner):
            self.assertTrue(ns['run_agent_task']('prompt').startswith('Error executing task:'))
    def test_email_launcher_is_part_of_release_proof(self):
        entry=next(item for item in CHAT_RUNTIME_FILES if item.name=='email-agent')
        self.assertEqual(entry.dest_relpath,'scripts/robie_email_agent.py')
        self.assertIn('scripts/robie_email_agent.py',zip_load_shim_source(entry.zip_relpath))
    def test_new_test_email_shim_never_falls_back_to_production_before_configuration(self):
        import tempfile
        with durable_temporary_directory() as tmp:
            root=Path(tmp)/'streetsmart-hermes-test'
            launcher=root/'.hermes/scripts/robie_email_agent.py'
            launcher.parent.mkdir(parents=True)
            release=root/'releases/current/scripts/robie_email_agent.py'
            release.parent.mkdir(parents=True)
            release.write_text('SELECTED = "test-release"')
            ns={'__file__':str(launcher)}
            with patch.dict(os.environ,{'ROBIE_CANONICAL_JOB_ENGINE_ROOT':'/opt/streetsmart-hermes/releases/current'},clear=True):
                exec(zip_load_shim_source('scripts/robie_email_agent.py'),ns)
            self.assertEqual(ns['SELECTED'],'test-release')
            release.write_text('SELECTED = "rollback-release"')
            ns={'__file__':str(launcher)}
            exec(zip_load_shim_source('scripts/robie_email_agent.py'),ns)
            self.assertEqual(ns['SELECTED'],'rollback-release')
    def test_test_launcher_defaults_are_derived_from_its_own_release(self):
        tree=ast.parse((ROOT/'scripts/robie_email_agent.py').read_text())
        begin=next(i for i,n in enumerate(tree.body) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='_release_root' for t in n.targets))
        end=next(i for i,n in enumerate(tree.body) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='ATTACHMENT_DIR' for t in n.targets))
        code=compile(ast.Module(body=tree.body[begin:end],type_ignores=[]),'paths','exec')
        ns={'Path':Path,'os':os,'__file__':'/opt/streetsmart-hermes-test/releases/sha/repo/scripts/robie_email_agent.py'}
        with patch.dict(os.environ,{},clear=True):
            exec(code,ns)
        self.assertEqual(ns['_EXPECTED_ENV'],'TEST')
        self.assertTrue(ns['TOKEN_PATH'].startswith('/opt/streetsmart-hermes-test/'))
        self.assertTrue(ns['JOB_DB'].startswith('/opt/streetsmart-hermes-test/'))
        with patch.dict(os.environ,{'ROBIE_JOB_DB':'/opt/streetsmart-hermes/robie-job-engine/data/jobs.db'},clear=True),self.assertRaises(RuntimeError):
            exec(code,ns)

class EmailRoutingTests(TestCase):
    def load_executor(self, db_path, manager):
        import re
        from unittest.mock import Mock
        source=ast.parse((ROOT/'scripts/robie_email_agent.py').read_text())
        function=next(n for n in source.body if isinstance(n,ast.FunctionDef) and n.name=='execute_email_work')
        ns={'Path':Path,'re':re,'logger':Mock(),'load_ascend_sessions':lambda:{},
            'AscendWorkflowManager':manager,'run_agent_task':Mock(return_value='generic result')}
        exec(compile(ast.Module(body=[function],type_ignores=[]),'executor','exec'),ns)
        return ns

    def test_quote_word_does_not_hijack_generic_task_into_finance(self):
        import tempfile
        from robie_job_engine.store import JobStore
        with durable_temporary_directory() as tmp:
            db=str(Path(tmp)/'jobs.db'); job=JobStore(db).create_job('hermes.email_task',{})
            manager=Mock(side_effect=AssertionError('Not a finance task'))
            ns=self.load_executor(db,manager)
            result=ns['execute_email_work']('sender','Quote','Upload this quote',[],'thread',job['id'],db,'SOP CONTEXT')
            self.assertEqual(result,'generic result')
            self.assertIn('SOP CONTEXT',ns['run_agent_task'].call_args.args[0])
            manager.assert_not_called()

    def test_finance_exception_does_not_fall_back_and_repeat_mutations(self):
        import tempfile
        from robie_job_engine.store import JobStore
        with durable_temporary_directory() as tmp:
            db=str(Path(tmp)/'jobs.db'); job=JobStore(db).create_job('hermes.email_task',{})
            manager=Mock(); manager.return_value.process_quote_request.side_effect=TimeoutError('unknown result')
            ns=self.load_executor(db,manager)
            result=ns['execute_email_work']('sender','Ascend','Create a finance agreement',[],'thread',job['id'],db)
            self.assertTrue(result.startswith('ROBIE_OUTCOME_UNKNOWN'))
            ns['run_agent_task'].assert_not_called()
            self.assertEqual(JobStore(db).get_checkpoint(job['id'],'email_route')['route'],'finance')

class EmailProcessBoundaryTests(TestCase):
    def test_timeout_stops_process_group_and_reaps_before_returning(self):
        import json, signal, sys
        tree=ast.parse((ROOT/'scripts/robie_email_agent.py').read_text())
        function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='run_email_job')
        child=Mock(pid=123)
        child.communicate.side_effect=[subprocess.TimeoutExpired('email',600), ('','')]
        ns={'os':os,'json':json,'signal':signal,'sys':sys,'Path':Path,'subprocess':subprocess,'__file__':str(ROOT/'scripts/robie_email_agent.py')}
        exec(compile(ast.Module(body=[function],type_ignores=[]),'runner','exec'),ns)
        with patch.object(subprocess,'Popen',return_value=child) as start, patch.object(os,'killpg') as kill:
            response=ns['run_email_job']('task','job','/test/jobs.db',sender='sender',subject='Task',body='work',attachments=[],thread_id='thread')
        self.assertTrue(start.call_args.kwargs['start_new_session'])
        kill.assert_called_once_with(123,signal.SIGKILL)
        self.assertEqual(child.communicate.call_count,2)
        self.assertTrue(response.startswith('ROBIE_OUTCOME_UNKNOWN'))

    def test_real_intake_wraps_execution_in_job_and_returns_engine_receipt(self):
        import base64, tempfile
        from email.mime.text import MIMEText
        from robie_job_engine.email_guard import run_guarded_email_task
        from robie_job_engine import email_guard
        from robie_job_engine.store import JobStore
        from robie_job_engine.message_verification import MessageOutcomeVerifier
        from robie_job_engine.chat_ezlynx_destination_verifier import HermesChatEzlynxDestinationVerifier
        from test_chat_ezlynx_destination_verifier import FakePort, real_policy_row, BOND_POLICY, BOND_APPLICANT
        tree=ast.parse((ROOT/'scripts/robie_email_agent.py').read_text())
        function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='process_inbox')
        body=f'Verify policy {BOND_POLICY} exists on applicant {BOND_APPLICANT}'
        service=Mock(); api=service.users.return_value.messages.return_value
        api.list.return_value.execute.return_value={'messages':[{'id':'m1'}]}
        api.get.return_value.execute.return_value={'id':'m1','threadId':'thread','payload':{'headers':[{'name':'from','value':'sender@example.test'},{'name':'subject','value':'Task'}]}}
        with durable_temporary_directory() as tmp:
            db=str(Path(tmp)/'jobs.db'); calls=[]
            def execute(prompt,job_id,db_path,**kwargs):
                store=JobStore(db_path)
                self.assertEqual(store.get_job(job_id)['status'],'RUNNING')
                self.assertEqual(kwargs['body'],body)
                store.add_playwright_exec(job_id,'playwright_exec','ok',result={'url':f'https://app.ezlynx.com/web/account/{BOND_APPLICANT}'})
                calls.append(job_id)
                return f'Policy {BOND_POLICY} exists.'
            ns={'get_gmail_service':lambda:service,'load_processed_ids':lambda:set(),'save_processed_ids':Mock(),
                'extract_sender_email':lambda v:v,'is_allowed_sender':lambda v:True,'extract_body_text':lambda p:body,
                'download_attachments':lambda *a:[],'logger':Mock(),'JOB_DB':db,'run_guarded_email_task':run_guarded_email_task,
                'run_agent_task':Mock(side_effect=AssertionError('use context')),'run_email_job':execute,
                'EmailTaskPending':email_guard.EmailTaskPending,'MIMEText':MIMEText,'base64':base64}
            exec(compile(ast.Module(body=[function],type_ignores=[]),'intake','exec'),ns)
            verifier=MessageOutcomeVerifier(HermesChatEzlynxDestinationVerifier(FakePort(policies=[real_policy_row()])))
            with patch.object(email_guard,'_default_email_verifiers',return_value={'hermes.email_task':verifier}):
                ns['process_inbox']()
            self.assertEqual(len(calls),1)
            self.assertEqual(JobStore(db).get_job(calls[0])['status'],'COMPLETE')
            api.send.assert_called_once()
