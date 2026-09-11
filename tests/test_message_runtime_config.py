import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock
spec=importlib.util.spec_from_file_location('message_config',Path(__file__).resolve().parents[1]/'scripts/configure-message-runtime.py')
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
REFS={f'ROBIE_EZLYNX_{key}_SECRET':f'projects/example/secrets/{key.lower()}/versions/1' for key in ('USERNAME','PASSWORD')}
class RuntimeConfigTests(unittest.TestCase):
    def test_test_cannot_select_production_secret_or_paths(self):
        result=m.settings('TEST',REFS)
        self.assertIn('ROBIE_EZLYNX_API_UAT_SECRET',result)
        self.assertNotIn('ROBIE_EZLYNX_API_PROD_SECRET',result)
        self.assertEqual(result['ROBIE_JOB_DB'],'/opt/streetsmart-hermes-test/robie-job-engine/data/jobs.db')
    def test_production_uses_only_production_api(self):
        result=m.settings('PRODUCTION',REFS)
        self.assertIn('ROBIE_EZLYNX_API_PROD_SECRET',result)
        self.assertNotIn('ROBIE_EZLYNX_API_UAT_SECRET',result)
    def test_unpinned_login_is_rejected(self):
        with self.assertRaises(ValueError):
            m.settings('PRODUCTION',{k:v.replace('/1','/latest') for k,v in REFS.items()})
    def test_failed_activation_restores_old_configuration_and_removes_new_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); old=root/'old'; new=root/'new'; old.write_text('old config')
            def failed():
                self.assertEqual(old.read_text(),'new config')
                raise RuntimeError('startup failed')
            with self.assertRaises(RuntimeError):
                m.apply_files({old:'new config',new:'new file'},root/'backup',failed)
            self.assertEqual(old.read_text(),'old config'); self.assertFalse(new.exists())
            self.assertEqual((root/'backup/0').read_text(),'old config')

class EnvironmentActivationTests(unittest.TestCase):
    def test_waits_for_startup_and_two_matches_from_same_pid(self):
        with mock.patch.object(m, 'run', side_effect=['0', '10', '11', '11']) as run, \
             mock.patch.object(Path, 'read_text', return_value='ROBIE_ENV=TEST\0'), \
             mock.patch.object(m.time, 'sleep'):
            m.verify_running_environment('gateway', {'ROBIE_ENV': 'TEST'})
        self.assertEqual(run.call_count, 4)

    def test_missing_process_is_retried(self):
        with mock.patch.object(m, 'run', return_value='10'), \
             mock.patch.object(Path, 'read_text', side_effect=[FileNotFoundError(), 'K=V\0', 'K=V\0']), \
             mock.patch.object(m.time, 'sleep'):
            m.verify_running_environment('gateway', {'K': 'V'})

    def test_timeout_names_keys_but_never_values(self):
        with mock.patch.object(m, 'run', return_value='10'), \
             mock.patch.object(Path, 'read_text', return_value='KEY=private-old-value\0'), \
             mock.patch.object(m.time, 'monotonic', side_effect=[0, 0, 16]), \
             mock.patch.object(m.time, 'sleep'):
            with self.assertRaises(RuntimeError) as error:
                m.verify_running_environment('gateway', {'KEY': 'private-new-value'})
        self.assertIn('KEY', str(error.exception))
        self.assertNotIn('private-', str(error.exception))
