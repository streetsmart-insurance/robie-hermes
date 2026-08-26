from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "deploy/hermes/tools/playwright_tool.py"
SKILL = ROOT / "deploy/hermes/skills/robie-playwright-browser/SKILL.md"
CONFIG = ROOT / "deploy/hermes/config/playwright.yaml"
SOUL = ROOT / "deploy/hermes/SOUL.playwright.md"


class PlaywrightIntegrationTests(unittest.TestCase):
    def test_playwright_tool_is_registered_and_fail_closed(self):
        source = TOOL.read_text()
        self.assertIn('name="playwright_exec"', source)
        self.assertIn('toolset="playwright"', source)
        self.assertIn("connect_over_cdp", source)
        self.assertIn("PLAYWRIGHT_BLOCKED", source)
        self.assertIn("if proc.returncode != 0", source)
        self.assertIn("start_new_session=True", source)
        self.assertIn("os.killpg", source)
        self.assertIn("persistent browser was preserved", source)

    def test_playwright_policy_forbids_silent_engine_fallback(self):
        source = SKILL.read_text()
        for forbidden in ("Selenium", "Puppeteer", "raw CDP", "coordinate-only"):
            self.assertIn(forbidden, source)
        self.assertIn("independently reread the destination", source)
        self.assertIn("stored Drive link", source)

    def test_runtime_fragments_enable_tool_and_preserve_completion_authority(self):
        config = CONFIG.read_text()
        soul = SOUL.read_text()
        self.assertEqual(2, config.count("- playwright"))
        self.assertIn("ROBIE_PLAYWRIGHT_CDP_URL", config)
        self.assertIn("fail_closed: true", config)
        self.assertIn("recordings are supporting", soul)
        self.assertIn("do not authorize Job Engine `COMPLETE`", soul)


if __name__ == "__main__":
    unittest.main()
