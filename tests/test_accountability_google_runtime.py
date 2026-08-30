from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = REPO_ROOT / "deploy" / "requirements-test-gateway-playwright.txt"
INSTALLER = REPO_ROOT / "scripts" / "deploy-test-release.sh"


class TestAccountabilityGoogleRuntime(unittest.TestCase):
    def test_test_runtime_pins_google_clients(self):
        lines = {
            line.strip()
            for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertIn("google-api-python-client==2.170.0", lines)
        self.assertIn("google-auth==2.40.3", lines)
        self.assertIn("google-cloud-secret-manager==2.24.0", lines)
        self.assertIn("playwright==1.52.0", lines)

    def test_installer_import_probes_google_clients_before_pointer_flip(self):
        script = INSTALLER.read_text(encoding="utf-8")
        probe = script.index('PYTHONPATH="${runtime_root}" "${gateway_python}" - <<\'PY\'')
        pointer_flip = script.index('bash "${release_root}/scripts/install-official-release.sh" install')
        self.assertLess(probe, pointer_flip)
        checked_region = script[probe:pointer_flip]
        self.assertIn("from google.auth import credentials as google_credentials", checked_region)
        self.assertIn("from google.cloud import secretmanager", checked_region)
        self.assertIn("from googleapiclient.discovery import build", checked_region)


if __name__ == "__main__":
    unittest.main()
