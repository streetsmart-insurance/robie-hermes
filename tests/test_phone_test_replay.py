import unittest,tempfile
from pathlib import Path
from robie_job_engine.phone_test_replay import replay
from robie_job_engine.phone_controls import Refused
class Replay(unittest.TestCase):
 def test_end_to_end_label_controls_completion_replay(self):
  with tempfile.TemporaryDirectory(dir=Path.home(),prefix='phone-replay-proof-') as p:
   Path(p).chmod(0o700);r=replay(p,'hermes-test-01');self.assertEqual(4,r['replay_dispatches']);self.assertEqual(0,r['live_calls']);self.assertFalse(any(x['repeat_dispatch'] for x in r['results']))
 def test_wrong_host(self):
  with self.assertRaises(Refused):replay('/tmp','hermes-poc-01')
if __name__=='__main__':unittest.main()
