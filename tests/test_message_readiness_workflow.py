from pathlib import Path
import unittest


class MessageReadinessWorkflowTests(unittest.TestCase):
    def test_readiness_checks_installed_probe_digest_before_execution(self):
        workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/verify-message-runtime.yml').read_text()
        self.assertIn("github.ref == 'refs/heads/main'", workflow)
        self.assertIn('PROBE="${ROOT}/releases/current/scripts/milestone_preflight.py"', workflow)
        self.assertIn('PROBE_DIGEST=$(sha256sum scripts/milestone_preflight.py', workflow)
        self.assertIn("sha256sum -c -; sudo python3 '${PROBE}'", workflow)
        self.assertIn('--ssh-flag=-oConnectTimeout=20', workflow)
        self.assertIn('timeout-minutes: 4', workflow)
        self.assertNotIn('gcloud compute scp', workflow)
        self.assertNotIn('rm -f', workflow)
