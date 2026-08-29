from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "deploy/hermes/tools/playwright_tool.py"
GEMINI_TOOL = ROOT / "deploy/hermes/tools/gemini_field_tool.py"
SKILL = ROOT / "deploy/hermes/skills/robie-playwright-browser/SKILL.md"
COMMERCIAL_AUTO = ROOT / "deploy/hermes/skills/ezlynx-commercial-auto-from-quote/SKILL.md"
GEMINI_FALLBACK = ROOT / "deploy/hermes/skills/ezlynx-gemini-fallback/SKILL.md"
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
        self.assertIn("install_playwright_write_guards", source)
        self.assertIn("destination_verified", source)
        self.assertIn("authorizes_complete", source)
        self.assertIn("identifies exactly one field", source)
        self.assertIn("gemini_unique_field", source)
        self.assertIn("ask_gemini_unique_field", source)
        self.assertIn("ROBIE_JOB_ENGINE_ROOT", source)
        self.assertIn("ROBIE_CANONICAL_JOB_ENGINE_ROOT", source)
        self.assertIn("resolve_write_guard_path", source)
        self.assertIn("deploy_truth", source)
        self.assertIn("HITL Carlo", source)
        self.assertIn("do not retry-loop", source)
        self.assertIn("PLAYWRIGHT_FAIL_CLOSED", source)
        self.assertIn("EmptyFileError", source)
        self.assertIn("playwright-artifacts", source)
        self.assertIn("do not retry the same download/screenshot/PDF parse", source)
        self.assertIn(".hermes/hermes-agent/tools/playwright_tool.py", source)
        self.assertIn("runner_failure_error", source)
        self.assertIn("select_playwright_page", source)
        self.assertIn("publish_live_playwright_hint", source)
        self.assertNotIn("page = pages[0] if pages else context.new_page()", source)
        self.assertIn("refusing pages[0] / first-ezlynx-wins", source)
        self.assertIn("flush_tabs_at_job_start", source)
        self.assertIn("refuse_wrong_host_at_job_start", source)

    def test_playwright_policy_forbids_silent_engine_fallback(self):
        source = SKILL.read_text()
        for forbidden in ("Selenium", "Puppeteer", "raw CDP", "coordinate-only"):
            self.assertIn(forbidden, source)
        self.assertIn("independently reread the destination", source)
        self.assertIn("stored Drive link", source)
        self.assertIn("uniquely identifies exactly one", source)
        self.assertIn("PLAYWRIGHT_BLOCKED", source)
        self.assertIn("/web/account/<id>/policies", source)
        self.assertIn("Do not enumerate Summary, Details, or Index", source)

    def test_runtime_fragments_enable_tool_and_preserve_completion_authority(self):
        config = CONFIG.read_text()
        soul = SOUL.read_text()
        self.assertEqual(2, config.count("- playwright"))
        self.assertIn("ROBIE_PLAYWRIGHT_CDP_URL", config)
        self.assertIn("fail_closed: true", config)
        self.assertIn("recordings are supporting", soul)
        self.assertIn("do not authorize Job Engine `COMPLETE`", soul)
        self.assertIn("uniquely identify", soul)
        self.assertIn("ask Gemini for one unique", soul)
        self.assertIn("HITL Carlo", soul)
        self.assertIn("/web/account/<id>/", soul)
        self.assertIn("Do not enumerate Summary/Details/Index", soul)

    def test_gemini_unique_field_tool_is_fail_closed(self):
        source = GEMINI_TOOL.read_text()
        self.assertIn('name="gemini_unique_field"', source)
        self.assertIn('toolset="playwright"', source)
        self.assertIn("ask_gemini_unique_field", source)
        self.assertIn("HITL Carlo", source)
        self.assertIn("Never guess", source)
        self.assertIn(".first/.nth/.last", source)

    def test_commercial_auto_and_gemini_fallback_skills_are_installed(self):
        commercial = COMMERCIAL_AUTO.read_text()
        fallback = GEMINI_FALLBACK.read_text()
        skill = SKILL.read_text()
        self.assertIn("Save and Continue Edit", commercial)
        self.assertIn("Actions → Edit", commercial)
        self.assertIn("Robie was here", commercial)
        self.assertIn("Do not bind", commercial)
        self.assertIn("named insured", commercial)
        self.assertIn("/web/account/<id>/policies", commercial)
        self.assertIn("Do not guess Summary, Details, or Index", commercial)
        self.assertIn("vehicles", commercial.casefold())
        self.assertIn("PLAYWRIGHT_BLOCKED", fallback)
        self.assertIn("ask Gemini for one unique field", fallback)
        self.assertIn("HITL Carlo", fallback)
        self.assertIn("Never write a note on Untitled", fallback)
        self.assertIn("stuck Playwright write on any site", fallback)
        self.assertNotIn("EZLynx Gemini fallback", fallback)
        self.assertIn("ezlynx-gemini-fallback", skill)


if __name__ == "__main__":
    unittest.main()
