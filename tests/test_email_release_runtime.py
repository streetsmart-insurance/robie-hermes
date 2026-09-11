"""Exercise launcher context without importing Gmail clients or sending mail."""
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
        with tempfile.TemporaryDirectory() as tmp:
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
