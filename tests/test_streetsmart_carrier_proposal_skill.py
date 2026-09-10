from __future__ import annotations

import json
import unittest
from pathlib import Path


REPOSITORY_SKILL = Path("skills/streetsmart-carrier-proposal")
DEPLOY_SKILL = Path("deploy/hermes/skills/streetsmart-carrier-proposal")
PACKAGE_FILES = (
    "SKILL.md",
    "agents/openai.yaml",
    "scripts/generate_proposal.js",
    "scripts/package.json",
    "scripts/render_proposal.sh",
    "scripts/assets/cns_logo.png",
    "scripts/assets/jake_photo.png",
    "scripts/assets/logo.png",
    "scripts/assets/stripe.png",
)


class StreetSmartCarrierProposalSkillTests(unittest.TestCase):
    def test_repository_and_deploy_packages_are_exact_mirrors(self):
        for relative in PACKAGE_FILES:
            self.assertEqual(
                (REPOSITORY_SKILL / relative).read_bytes(),
                (DEPLOY_SKILL / relative).read_bytes(),
                relative,
            )

    def test_skill_uses_existing_carrier_proposal_job_type(self):
        skill = (REPOSITORY_SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("job_type: carrier.proposal", skill)
        self.assertIn("production_ready: true", skill)
        self.assertIn("Never add a default broker", skill)
        self.assertIn("deliver both the .docx and the .pdf", skill.casefold())

    def test_renderer_is_deterministic_and_forbids_browser_pdf_fallback(self):
        wrapper = (REPOSITORY_SKILL / "scripts/render_proposal.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('generate_proposal.js', wrapper)
        self.assertIn('LibreOffice is required', wrapper)
        self.assertNotIn('chromium', wrapper.casefold())
        self.assertNotIn('playwright', wrapper.casefold())
        self.assertNotIn('puppeteer', wrapper.casefold())

    def test_node_dependency_is_pinned_and_brand_assets_are_pngs(self):
        package = json.loads(
            (REPOSITORY_SKILL / "scripts/package.json").read_text(encoding="utf-8")
        )
        self.assertEqual(package["dependencies"]["docx"], "9.7.1")
        for name in ("cns_logo.png", "jake_photo.png", "logo.png", "stripe.png"):
            data = (REPOSITORY_SKILL / "scripts/assets" / name).read_bytes()
            self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"), name)
            self.assertGreater(len(data), 1000, name)


if __name__ == "__main__":
    unittest.main()
