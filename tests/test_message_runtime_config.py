import importlib.util
from pathlib import Path
import tempfile
import unittest
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
