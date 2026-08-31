from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-test.yml"
INSTALLER = ROOT / "scripts" / "deploy-test-release.sh"
ROLLBACK_LIBRARY = ROOT / "scripts" / "lib" / "test-release-rollback.sh"
RUNTIME_REQUIREMENTS = ROOT / "deploy" / "requirements-test-gateway-playwright.txt"


class TestDeployWorkflowContractTests(unittest.TestCase):
    def test_credentials_are_main_only_and_target_is_exact_test_vm(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", text)
        self.assertNotIn("pull_request:", text)
        self.assertNotIn("pull_request_target:", text)
        self.assertIn("github.ref == 'refs/heads/main'", text)
        self.assertIn("inputs.confirmation == 'DEPLOY_TO_HERMES_TEST_01'", text)
        self.assertIn("TEST_VM: hermes-test-01", text)
        self.assertIn("id-token: write", text)
        self.assertNotIn("hermes-poc-01", text)
        self.assertNotIn("systemctl restart hermes-gateway", text)

    def test_installer_fails_closed_and_preserves_rollback(self):
        text = INSTALLER.read_text(encoding="utf-8")
        self.assertIn('EXPECTED_HOST="hermes-test-01"', text)
        self.assertIn('GATEWAY_UNIT="robie-gateway"', text)
        self.assertIn('OPT_ROOT="/opt/streetsmart-hermes-test"', text)
        self.assertIn("mode=ro", text)
        self.assertIn("active Test jobs or leases exist; refuse deploy", text)
        self.assertIn("rollback_test", text)
        self.assertIn("continuing pointer rollback", text)
        self.assertIn("atomic_pointer", text)
        candidate_verify = (
            'bash "${release_root}/scripts/verify-release.sh" "${archive}" "${checksum}"'
        )
        self.assertIn(candidate_verify, text)
        self.assertNotIn('${old_current}/scripts/verify-release.sh', text)
        self.assertLess(text.index('release_root="'), text.index(candidate_verify))
        self.assertLess(text.index(candidate_verify), text.index('source "${release_root}/scripts/lib/test-release-rollback.sh"'))
        self.assertLess(text.index(candidate_verify), text.index('systemctl restart'))
        precision_gate = 'official-install-flip.json'
        self.assertIn(precision_gate, text)
        self.assertIn("whole-second precision", text)
        precision_position = text.index(precision_gate)
        restart_after_gate = text.index(
            'systemctl restart "${GATEWAY_UNIT}"', precision_position
        )
        self.assertLess(precision_position, restart_after_gate)
        self.assertIn('"production_touched": False', text)
        self.assertNotIn("/opt/streetsmart-hermes/", text)
        self.assertNotIn("hermes-poc-01", text)
        self.assertNotIn("rm -rf /opt", text)

    def test_test_proof_uses_test_gateway_and_cannot_complete_jobs(self):
        text = INSTALLER.read_text(encoding="utf-8")
        self.assertIn('--gateway-unit "${GATEWAY_UNIT}"', text)
        self.assertIn('data.get("authorizes_complete") is False', text)
        self.assertIn("TEST VERIFIED", text)

    def test_playwright_runtime_is_release_local_test_only_and_evidenced(self):
        text = INSTALLER.read_text(encoding="utf-8")
        requirements = RUNTIME_REQUIREMENTS.read_text(encoding="utf-8")
        self.assertIn("playwright==1.52.0", requirements)
        self.assertIn('GATEWAY_RUNTIME_DIRNAME=".gateway-runtime"', text)
        self.assertIn("requirements-test-gateway-playwright.txt", text)
        self.assertIn('--target "${runtime_staging}"', text)
        self.assertIn('--only-binary=:all:', text)
        self.assertIn('PYTHONPATH="${runtime_root}"', text)
        self.assertIn("from playwright.sync_api import sync_playwright", text)
        self.assertIn("releases/current/${GATEWAY_RUNTIME_DIRNAME}", text)
        self.assertIn("if ! install_gateway_runtime_config; then", text)
        self.assertIn("rollback_test", text[text.index("if ! install_gateway_runtime_config; then"):])
        self.assertIn('"content_digest": sys.argv[15]', text)
        self.assertIn('"playwright_version": "1.52.0"', text)
        self.assertIn('"browser_binaries_installed": False', text)
        self.assertNotIn("playwright install", text)
        self.assertNotIn("hermes-poc-01", text)

    def test_runtime_dropin_snapshot_restore_is_behavioral(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            destination = root / "systemd" / "runtime.conf"
            snapshot = root / "snapshot.conf"
            destination.parent.mkdir(parents=True)
            destination.write_text("old-runtime\n", encoding="utf-8")
            snapshot.write_text("old-runtime\n", encoding="utf-8")
            destination.write_text("new-runtime\n", encoding="utf-8")

            harness = root / "restore.sh"
            harness.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "source \"$1\"\n"
                "restore_file_snapshot present \"$2\" \"$3\"\n",
                encoding="utf-8",
            )
            harness.chmod(0o755)
            subprocess.run(
                [str(harness), str(ROLLBACK_LIBRARY), str(snapshot), str(destination)],
                check=True,
            )
            self.assertEqual(destination.read_text(encoding="utf-8"), "old-runtime\n")

            destination.write_text("new-runtime\n", encoding="utf-8")
            subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; restore_file_snapshot absent "$2" "$3"',
                    "restore",
                    str(ROLLBACK_LIBRARY),
                    str(snapshot),
                    str(destination),
                ],
                check=True,
            )
            self.assertFalse(destination.exists())

    def test_installs_only_guarded_policy_setup_skill_with_rollback_evidence(self):
        text = INSTALLER.read_text(encoding="utf-8")
        self.assertIn('policy_skill_link="${OPT_ROOT}/.hermes/skills/ezlynx-policy-setup"', text)
        self.assertIn(
            'policy_skill_source="${release_root}/deploy/hermes/skills/ezlynx-policy-setup"',
            text,
        )
        self.assertIn("existing Test Policy Setup skill is not an atomic symlink", text)
        self.assertIn('atomic_pointer "${policy_skill_source}" "${policy_skill_link}"', text)
        self.assertIn('"${old_policy_skill_target}"', text)
        self.assertIn("rollback_test_release", text)
        self.assertIn("Policy Setup Test-only package validation failed", text)
        skill_install = text.index(
            'atomic_pointer "${policy_skill_source}" "${policy_skill_link}"'
        )
        restart_after_skill = text.index('systemctl restart "${GATEWAY_UNIT}"', skill_install)
        self.assertLess(skill_install, restart_after_skill)
        self.assertIn("rollback_test", text[skill_install:restart_after_skill + 200])
        self.assertIn('"homeowners_test_only": True', text)
        self.assertIn('"all_other_profiles": False', text)
        self.assertIn('"applicant_id": "220250093"', text)
        self.assertIn('"policy_number_prefix": "TEST-HO-"', text)
        self.assertIn('"premium": "1.00"', text)
        self.assertIn('"profile_manifest": sys.argv[11]', text)
        self.assertIn('"selector_inventory": sys.argv[11]', text)
        self.assertIn('"production_touched": False', text)
        self.assertNotIn('/opt/streetsmart-hermes/.hermes/skills/ezlynx-policy-setup', text)

    def test_post_flip_failure_restores_all_targets_and_gateway_recovers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old_release = root / "releases" / "old" / "robie-hermes-old"
            new_release = root / "releases" / "new" / "robie-hermes-new"
            old_skill = old_release / "deploy" / "hermes" / "skills" / "ezlynx-policy-setup"
            new_skill = new_release / "deploy" / "hermes" / "skills" / "ezlynx-policy-setup"
            for path in (old_skill, new_skill):
                path.mkdir(parents=True)

            current = root / "current"
            releases_current = root / "releases" / "current"
            skill_link = root / ".hermes" / "skills" / "ezlynx-policy-setup"
            skill_link.parent.mkdir(parents=True)
            current.symlink_to(old_release)
            releases_current.symlink_to(old_release)
            skill_link.symlink_to(old_skill)

            gateway_state = root / "gateway.state"
            gateway_log = root / "gateway.log"
            runtime_dropin = root / "systemd" / "runtime.conf"
            runtime_snapshot = root / "runtime.conf.pre-deploy"
            runtime_dropin.parent.mkdir(parents=True)
            runtime_dropin.write_text("old-runtime\n", encoding="utf-8")
            runtime_snapshot.write_text("old-runtime\n", encoding="utf-8")
            gateway_state.write_text("failed\n", encoding="utf-8")
            systemctl_stub = root / "systemctl-stub"
            systemctl_stub.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "printf '%s\\n' \"$*\" >>\"${GATEWAY_LOG}\"\n"
                "case \"$1\" in\n"
                "  daemon-reload) : ;;\n"
                "  restart) printf 'active\\n' >\"${GATEWAY_STATE}\" ;;\n"
                "  is-active) test \"$(cat \"${GATEWAY_STATE}\")\" = active ;;\n"
                "  *) exit 2 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            systemctl_stub.chmod(0o755)

            harness = root / "exercise-rollback.sh"
            harness.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "source \"$1\"\n"
                "atomic_pointer \"$3\" \"$5\"\n"
                "atomic_pointer \"$3\" \"$6\"\n"
                "atomic_pointer \"$4\" \"$7\"\n"
                "printf 'new-runtime\\n' >\"${10}\"\n"
                "# Fault injection occurs only after both release pointers and the skill flip.\n"
                "if ! false; then\n"
                "  restore_file_snapshot present \"$9\" \"${10}\"\n"
                "  \"${ROBIE_SYSTEMCTL}\" daemon-reload\n"
                "  rollback_test_release \"$2\" \"$2\" \"$8\" \"$5\" \"$6\" \"$7\" robie-gateway\n"
                "fi\n",
                encoding="utf-8",
            )
            harness.chmod(0o755)

            env = {
                **os.environ,
                "ROBIE_SYSTEMCTL": str(systemctl_stub),
                "GATEWAY_STATE": str(gateway_state),
                "GATEWAY_LOG": str(gateway_log),
            }
            subprocess.run(
                [
                    str(harness),
                    str(ROLLBACK_LIBRARY),
                    str(old_release),
                    str(new_release),
                    str(new_skill),
                    str(current),
                    str(releases_current),
                    str(skill_link),
                    str(old_skill),
                    str(runtime_snapshot),
                    str(runtime_dropin),
                ],
                check=True,
                env=env,
            )

            self.assertEqual(current.resolve(strict=True), old_release.resolve(strict=True))
            self.assertEqual(
                releases_current.resolve(strict=True), old_release.resolve(strict=True)
            )
            self.assertEqual(skill_link.resolve(strict=True), old_skill.resolve(strict=True))
            self.assertEqual(runtime_dropin.read_text(encoding="utf-8"), "old-runtime\n")
            self.assertEqual(gateway_state.read_text(encoding="utf-8"), "active\n")
            self.assertEqual(
                gateway_log.read_text(encoding="utf-8").splitlines(),
                [
                    "daemon-reload",
                    "restart robie-gateway",
                    "is-active --quiet robie-gateway",
                ],
            )


if __name__ == "__main__":
    unittest.main()
