"""zap-trigger path resolution + assignee normalization (unit only, no live Zap)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from robie_job_engine import zapier_tasks as zt

REPO = Path(__file__).resolve().parents[1]
IN_TREE = REPO / "skills" / "zapier" / "bin" / "zap-trigger"
FAKE_URL = "https://hooks.zapier.com/" + "unit-test-placeholder/"


def _clean_env():
    return {k: v for k, v in os.environ.items() if k != zt.ZAP_TRIGGER_ENV}


class ResolverTests(unittest.TestCase):
    def test_release_copy_is_first_default_candidate(self):
        with mock.patch.dict(os.environ, _clean_env(), clear=True):
            candidates = zt.zap_trigger_candidates()
        self.assertEqual(candidates[0], str(IN_TREE))
        self.assertIn(zt.ZAP_TRIGGER_SCRIPT, candidates)
        for root in ("/opt/streetsmart-hermes-test", "/opt/streetsmart-hermes"):
            self.assertIn(f"{root}/.hermes/skills/zapier/bin/zap-trigger", candidates)
            self.assertIn(f"{root}/workspace/skills/zapier/bin/zap-trigger", candidates)
        self.assertEqual(len(candidates), len(set(candidates)))

    def test_resolves_in_tree_script(self):
        with mock.patch.dict(os.environ, _clean_env(), clear=True):
            self.assertEqual(zt.resolve_zap_trigger(), str(IN_TREE))

    def test_env_override_is_exclusive(self):
        with tempfile.NamedTemporaryFile() as f, \
                mock.patch.dict(os.environ, {zt.ZAP_TRIGGER_ENV: f.name}):
            self.assertEqual(zt.zap_trigger_candidates(), [f.name])
            self.assertEqual(zt.resolve_zap_trigger(), f.name)

    def test_missing_override_does_not_fall_through(self):
        with mock.patch.dict(os.environ, {zt.ZAP_TRIGGER_ENV: "/nope/zap-trigger"}):
            with self.assertRaises(RuntimeError) as ctx:
                zt.resolve_zap_trigger()
        self.assertIn("/nope/zap-trigger", str(ctx.exception))

    def test_miss_lists_every_tried_path(self):
        tried = ["/a/zap-trigger", "/b/zap-trigger"]
        with self.assertRaises(RuntimeError) as ctx:
            zt.resolve_zap_trigger(tried)
        msg = str(ctx.exception)
        for path in tried:
            self.assertIn(path, msg)
        self.assertNotIn("hooks.zapier.com", msg)


class AssigneeTests(unittest.TestCase):
    def test_display_names_map_to_login(self):
        for name in ("Nicole Segovia", "nicole segovia", "  Nicole   Segovia ", "nicole", "Nicole"):
            with self.subTest(name=name):
                self.assertEqual(zt.normalize_assignee(name), "SSNicole")

    def test_login_passes_through(self):
        self.assertEqual(zt.normalize_assignee("SSNicole"), "SSNicole")

    def test_unknown_display_name_left_for_zap_trigger_to_refuse(self):
        self.assertEqual(zt.normalize_assignee("Some Person"), "Some Person")

    def test_document_retrieval_payload(self):
        payload = zt.document_retrieval_task_payload(
            applicant_id="220250093",
            carrier="Travelers",
            doc_type="Dec Page",
            insured="Acme LLC",
            policy_number="ABC-123",
            due_date="2026-10-01",
            assignee="Nicole Segovia",
        )
        self.assertEqual(payload, {
            "applicant_id": "220250093",
            "assignee": "SSNicole",
            "source": "document-retrieval",
            "due_date": "2026-10-01",
            "task_title": "Document Retrieval review — Travelers Dec Page — Acme LLC — ABC-123",
        })

    def test_document_retrieval_default_assignee_is_login(self):
        payload = zt.document_retrieval_task_payload(
            applicant_id="1", carrier="C", doc_type="D", insured="I",
            policy_number="P", due_date="2026-10-01",
        )
        self.assertEqual(payload["assignee"], "SSNicole")

    def test_document_retrieval_rejects_blanks_and_bad_date(self):
        with self.assertRaises(ValueError):
            zt.document_retrieval_task_payload(
                applicant_id="1", carrier="", doc_type="D", insured="I",
                policy_number="P", due_date="2026-10-01")
        with self.assertRaises(ValueError):
            zt.document_retrieval_task_payload(
                applicant_id="1", carrier="C", doc_type="D", insured="I",
                policy_number="P", due_date="10/01/2026")


class FireTaskDryRunTests(unittest.TestCase):
    def test_fire_task_normalizes_display_name_and_dry_runs(self):
        payload = {
            "applicant_id": "220250093",
            "task_title": "Document Retrieval review — C D — I — P",
            "assignee": "Nicole Segovia",
            "source": "document-retrieval",
            "due_date": "2026-10-01",
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {**_clean_env(), "ZAPIER_CATCH_HOOK_URL": FAKE_URL, "HERMES_HOME": tmp, "HOME": tmp},
            clear=True,
        ):
            result = zt.fire_task(payload, dry_run=True)
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["dry_run"], True)
        self.assertEqual(payload["assignee"], "SSNicole")


if __name__ == "__main__":
    unittest.main()
