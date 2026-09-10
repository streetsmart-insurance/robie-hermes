from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path


REPOSITORY_SKILL = Path("skills/streetsmart-carrier-proposal")
DEPLOY_SKILL = Path("deploy/hermes/skills/streetsmart-carrier-proposal")
EXPECTED_SHA256 = {
    "SKILL.md": "a5dc46f20307f5b0662aa72a9b822268168ae153bfa7bcac13055e24e0a01a90",
    "agents/openai.yaml": "6d42f082a45510ec814d4be6af54f51a31dee4a71b105d912cc33c5a1d2686ba",
    "scripts/assets/cns_logo.png": "9364c6fa0e07eedd7c7a0d806fe505917261a0db7898bbe6a4ee16a88f006aaf",
    "scripts/assets/jake_photo.png": "4865b57cc4086e2faae911be97ec9b83f14d8c64df312dcac915c733d0d378eb",
    "scripts/assets/logo-expanded-v2.png": "cd7ce1db8c19c35a9ab94c10a7fab17e9e5389d0f9bc1363e74e1603174e08c7",
    "scripts/assets/logo.png": "95cfa905696d74517a3cb9958a29e74af5d2ac064c4b4525b52f695139d4c720",
    "scripts/assets/quote-video-qr.png": "f6a8df451506563f900a117054cf895413ad812c3a2fa31500668999fe7eb250",
    "scripts/assets/stripe.png": "f5953551ad5b600cfd28ca78b2589a4bc71fd8bcc7fe3174030a6b597752a694",
    "scripts/generate_proposal.js": "d58f59e8886f95a8c22485171e7dc154f2f142f3e57029737b217ee3f38975e3",
    "scripts/package.json": "6968b829f56c19a9292132b552eb01cf35d5872a4702f2b786e99b1e7745ea13",
    "scripts/render_proposal.sh": "1060772dae138b2f24429c283111312510a890ca949592c354a58ce25d377848",
}


class StreetSmartCarrierProposalSkillTests(unittest.TestCase):
    def test_both_packages_match_the_approved_manifest_exactly(self):
        for root in (REPOSITORY_SKILL, DEPLOY_SKILL):
            actual = {
                path.relative_to(root).as_posix()
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(actual, set(EXPECTED_SHA256))
            for relative, expected in EXPECTED_SHA256.items():
                data = (root / relative).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), expected, relative)

    def test_repository_and_deploy_packages_are_exact_mirrors(self):
        for relative in EXPECTED_SHA256:
            self.assertEqual(
                (REPOSITORY_SKILL / relative).read_bytes(),
                (DEPLOY_SKILL / relative).read_bytes(),
                relative,
            )

    def test_default_is_450_labeled_only_as_taxes_and_fees(self):
        skill = (REPOSITORY_SKILL / "SKILL.md").read_text(encoding="utf-8")
        generator = (REPOSITORY_SKILL / "scripts/generate_proposal.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("return 450", generator)
        self.assertIn('["Taxes and fees", "Included in initial payment"]', generator)
        self.assertIn('"taxes_and_fees": "$450.00"', skill)
        for forbidden in ("Agency fee", "Service fee", "Broker fee"):
            self.assertNotIn(forbidden, generator)

    def test_renderer_contract_and_pinned_dependency(self):
        wrapper = (REPOSITORY_SKILL / "scripts/render_proposal.sh").read_text(
            encoding="utf-8"
        )
        package = json.loads(
            (REPOSITORY_SKILL / "scripts/package.json").read_text(encoding="utf-8")
        )
        self.assertEqual(package["dependencies"]["docx"], "9.7.1")
        self.assertIn("LibreOffice is required", wrapper)
        self.assertIn("612 x 792 pts", wrapper)
        for forbidden in ("chromium", "playwright", "puppeteer", "wkhtmltopdf"):
            self.assertNotIn(forbidden, wrapper.casefold())


if __name__ == "__main__":
    unittest.main()
