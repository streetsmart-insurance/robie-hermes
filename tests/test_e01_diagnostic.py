"""Attach + capture path for scripts/e01_diagnostic.py --mode=full.

No live Chrome. No EZLynx. No bind. Applicant 220250093 only.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import py_compile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "e01_diagnostic.py"
ALLOWED = "220250093"
FOREIGN = "221398001"
POLICY_ID = "83669533"
EDIT_URL = (
    "https://app.ezlynx.com/applicantportal/Policy/Actions/Edit/"
    f"{ALLOWED}/{POLICY_ID}"
)


def load_script():
    spec = importlib.util.spec_from_file_location("e01_diagnostic", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def run(coro):
    return asyncio.run(coro)


class FakeLocator:
    def __init__(self, page: "FakePage", name: str = "") -> None:
        self._page = page
        self._name = name
        self.first = self

    def nth(self, _index: int) -> "FakeLocator":
        return self

    def locator(self, *args, **_kwargs) -> "FakeLocator":
        return FakeLocator(self._page, str(args[0]) if args else self._name)

    async def count(self) -> int:
        return 1

    async def fill(self, value, **_kwargs) -> None:
        self._page.fills.append((self._name, value))

    async def select_option(self, *args, **kwargs) -> None:
        self._page.selects.append((self._name, args, kwargs))

    async def click(self, **_kwargs) -> None:
        self._page.clicks.append(self._name)
        self._page.url = f"https://app.ezlynx.com/applicantportal/FormEntry/{ALLOWED}"

    async def inner_text(self) -> str:
        return "Direct Bill"

    async def get_attribute(self, _name: str) -> str:
        return "DB"

    async def input_value(self) -> str:
        return "DB"

    async def all_inner_texts(self) -> list[str]:
        return ["Direct Bill"]

    def all(self) -> list["FakeLocator"]:
        return [self]


class FakePage:
    def __init__(self, url: str = EDIT_URL) -> None:
        self.url = url
        self.gotos: list[str] = []
        self.fills: list[tuple] = []
        self.selects: list[tuple] = []
        self.clicks: list[str] = []
        self.waits: list[int] = []
        self.context = SimpleNamespace(pages=[self])
        self.frames: list = []

    async def goto(self, url: str, **_kwargs) -> None:
        self.gotos.append(url)
        self.url = url

    async def wait_for_timeout(self, timeout: int) -> None:
        self.waits.append(timeout)

    async def title(self) -> str:
        return "Edit Policy"

    async def screenshot(self, path=None, **_kwargs) -> bytes:
        if path:
            Path(path).write_bytes(b"png")
        return b"png"

    async def content(self) -> str:
        return "<html><body>e01</body></html>"

    def locator(self, *args, **_kwargs) -> FakeLocator:
        return FakeLocator(self, str(args[0]) if args else "")

    def get_by_role(self, role: str, **kwargs) -> FakeLocator:
        return FakeLocator(self, f"{role}:{kwargs.get('name', '')}")

    async def evaluate(self, _js) -> dict:
        return {}


class FakeBrowser:
    def __init__(self, pages: list[FakePage]) -> None:
        self.contexts = [SimpleNamespace(pages=pages)]


class FakeChromium:
    def __init__(self, browser: FakeBrowser) -> None:
        self._browser = browser
        self.connect_calls: list[str] = []
        self.launch_calls: list[dict] = []

    async def connect_over_cdp(self, url: str, **_kwargs):
        self.connect_calls.append(url)
        return self._browser

    async def launch(self, **kwargs):
        self.launch_calls.append(kwargs)
        raise AssertionError("e01_diagnostic must not chromium.launch")


class FakePlaywright:
    def __init__(self, pages: list[FakePage] | None = None) -> None:
        self.page = (pages or [FakePage()])[0]
        self.browser = FakeBrowser(pages or [self.page])
        self.chromium = FakeChromium(self.browser)
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class FakeSetup:
    def __init__(self, page, job_id=None, hitl_deps=None) -> None:
        self.page = page
        self.job_id = job_id
        self.applicant_id = None
        self.mint_calls: list[tuple[str, str]] = []

    async def _mint_formentry(self, policy_id: str, applicant_id: str = "") -> dict:
        self.mint_calls.append((policy_id, applicant_id))
        await self.page.goto(
            "https://app.ezlynx.com/applicantportal/Policy/Actions/Edit/"
            f"{applicant_id}/{policy_id}"
        )
        await self.page.locator("#BillingType").select_option(value="DB")
        await self.page.locator("#Department").select_option(value="Personal")
        await self.page.get_by_role("button", name="Save & Continue Edit").click()
        for _ in range(6):
            await self.page.wait_for_timeout(1000)
        return {
            "formentry_found": True,
            "policy_id": policy_id,
            "via": "save_and_continue_edit",
        }


class E01DiagnosticSourceTests(unittest.TestCase):
    def test_script_compiles(self):
        py_compile.compile(str(SCRIPT), doraise=True)

    def test_full_mode_is_not_a_placeholder(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("NOTE: Browser setup is environment-specific", source)
        self.assertNotIn("Placeholder for the actual flow", source)
        self.assertNotIn("robie-main2", source)
        self.assertNotIn("chromium.launch(", source)
        self.assertNotIn("launch_persistent_context", source)
        self.assertIn("connect_over_cdp", source)
        self.assertIn("/opt/streetsmart-hermes/releases/current", source)
        self.assertIn("EzlynxPolicySetupPage", source)
        self.assertNotIn("EzlynxPolicySetup()", source)
        self.assertIn("_mint_formentry", source)
        self.assertNotIn("setup_policy_by_lob", source)

    def test_no_applicant_cli_override(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("--applicant-id", source)
        self.assertNotIn("--applicant_id", source)


class E01AllowlistTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_script()

    def test_locked_constants(self):
        self.assertEqual(self.mod.ALLOWED_APPLICANT_ID, ALLOWED)
        self.assertEqual(self.mod.ALLOWED_POLICY_ID, POLICY_ID)
        self.assertEqual(self.mod.E01["applicant_id"], ALLOWED)
        self.assertEqual(self.mod.PRODUCTION_ENGINE_ROOT, "/opt/streetsmart-hermes/releases/current")
        self.assertIn(self.mod.PRODUCTION_ENGINE_ROOT, self.mod.sys.path)

    def test_require_e01_applicant_accepts_locked_id(self):
        self.assertEqual(self.mod.require_e01_applicant(ALLOWED), ALLOWED)
        self.assertEqual(self.mod.require_e01_applicant("  220250093  "), ALLOWED)

    def test_require_e01_applicant_refuses_every_other_id(self):
        for value in (FOREIGN, "220250094", "", None, "PAWIVA"):
            with self.subTest(value=value):
                with self.assertRaises(RuntimeError) as ctx:
                    self.mod.require_e01_applicant(value)
                self.assertIn("REFUSED", str(ctx.exception))
                self.assertIn(ALLOWED, str(ctx.exception))

    def test_require_e01_policy_id_refuses_other_policies(self):
        self.assertEqual(self.mod.require_e01_policy_id(POLICY_ID), POLICY_ID)
        with self.assertRaises(RuntimeError) as ctx:
            self.mod.require_e01_policy_id("83651751")
        self.assertIn("REFUSED", str(ctx.exception))

    def test_e01_edit_url_is_locked(self):
        self.assertEqual(self.mod.e01_edit_url(), EDIT_URL)
        with self.assertRaises(RuntimeError):
            self.mod.e01_edit_url(applicant_id=FOREIGN)
        with self.assertRaises(RuntimeError):
            self.mod.e01_edit_url(policy_id="99999999")

    def test_require_e01_url_refuses_foreign_edit_page(self):
        foreign_url = (
            "https://app.ezlynx.com/applicantportal/Policy/Actions/Edit/"
            f"{FOREIGN}/83669533"
        )
        with self.assertRaises(RuntimeError) as ctx:
            self.mod.require_e01_url(foreign_url)
        self.assertIn("REFUSED", str(ctx.exception))


class E01AttachTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_script()

    def test_attach_uses_connect_over_cdp_never_launch(self):
        pw = FakePlaywright()

        async def _run():
            session = await self.mod.attach_existing_cdp(
                playwright=pw, cdp_url=self.mod.DEFAULT_CDP_URL
            )
            return session

        session = run(_run())
        self.assertEqual(pw.chromium.connect_calls, ["http://127.0.0.1:9222"])
        self.assertEqual(pw.chromium.launch_calls, [])
        self.assertIs(session["page"], pw.page)
        self.assertFalse(session["own_playwright"])

    def test_attach_refuses_empty_contexts(self):
        pw = FakePlaywright()
        pw.browser.contexts = []

        async def _run():
            await self.mod.attach_existing_cdp(playwright=pw)

        with self.assertRaises(RuntimeError) as ctx:
            run(_run())
        self.assertIn("will not launch Chrome", str(ctx.exception))
        self.assertEqual(pw.chromium.launch_calls, [])

    def test_attach_refuses_no_pages(self):
        pw = FakePlaywright()
        pw.browser.contexts = [SimpleNamespace(pages=[])]

        async def _run():
            await self.mod.attach_existing_cdp(playwright=pw)

        with self.assertRaises(RuntimeError) as ctx:
            run(_run())
        self.assertIn("will not launch Chrome", str(ctx.exception))

    def test_select_prefers_existing_e01_edit_page(self):
        other = FakePage("https://app.ezlynx.com/web/account/220250093/policies")
        edit = FakePage(EDIT_URL)
        browser = FakeBrowser([other, edit])
        chosen = self.mod.select_existing_page(browser)
        self.assertIs(chosen, edit)

    def test_select_refuses_foreign_applicant_tabs(self):
        foreign = FakePage(
            f"https://app.ezlynx.com/applicantportal/Policy/Actions/Edit/{FOREIGN}/1"
        )
        browser = FakeBrowser([foreign])
        with self.assertRaises(RuntimeError) as ctx:
            self.mod.select_existing_page(browser)
        self.assertIn("other applicants", str(ctx.exception))


class E01CapturePathTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_script()

    def test_bound_page_captures_navigate_fill_click_and_five_second_waits(self):
        page = FakePage("https://app.ezlynx.com/web/")
        with tempfile.TemporaryDirectory() as tmp:
            ev = self.mod.EvidenceCollector(tmp)
            bound = self.mod.EvidenceBoundPage(page, ev, wait_interval_ms=5000)

            async def _drive():
                await bound.goto(EDIT_URL)
                await bound.locator("#BillingType").select_option(value="DB")
                await bound.locator("#Department").select_option(value="Personal")
                await bound.get_by_role("button", name="Save & Continue Edit").click()
                for _ in range(6):
                    await bound.wait_for_timeout(1000)

            run(_drive())
            actions = [step["action"] for step in ev.steps]
            self.assertEqual(actions[0], "navigate")
            self.assertGreaterEqual(actions.count("fill"), 2)
            self.assertIn("click", actions)
            self.assertIn("wait", actions)
            self.assertEqual(actions.count("wait"), 1)
            self.assertTrue((Path(tmp) / "evidence.json").exists() or ev.steps)
            ev.save()
            payload = json.loads((Path(tmp) / "evidence.json").read_text())
            self.assertEqual(payload["applicant_id"], ALLOWED)
            self.assertEqual(payload["policy_id"], POLICY_ID)

    def test_goto_refuses_foreign_applicant(self):
        page = FakePage()
        with tempfile.TemporaryDirectory() as tmp:
            ev = self.mod.EvidenceCollector(tmp)
            bound = self.mod.EvidenceBoundPage(page, ev)

            async def _drive():
                await bound.goto(
                    "https://app.ezlynx.com/applicantportal/Policy/Actions/Edit/"
                    f"{FOREIGN}/1"
                )

            with self.assertRaises(RuntimeError) as ctx:
                run(_drive())
            self.assertIn("REFUSED", str(ctx.exception))
            self.assertEqual(page.gotos, [])

    def test_full_diagnostic_attaches_and_drives_mint_path(self):
        pw = FakePlaywright()
        setups: list[FakeSetup] = []

        class TrackingSetup(FakeSetup):
            def __init__(self, page, job_id=None, hitl_deps=None):
                super().__init__(page, job_id=job_id, hitl_deps=hitl_deps)
                setups.append(self)

        with tempfile.TemporaryDirectory() as tmp:

            async def _run():
                return await self.mod.run_full_diagnostic(
                    playwright=pw,
                    output_dir=tmp,
                    setup_cls=TrackingSetup,
                )

            nav = run(_run())
            payload = json.loads((Path(tmp) / "evidence.json").read_text())

        self.assertEqual(pw.chromium.connect_calls, ["http://127.0.0.1:9222"])
        self.assertEqual(pw.chromium.launch_calls, [])
        self.assertTrue(setups)
        self.assertEqual(setups[0].mint_calls, [(POLICY_ID, ALLOWED)])
        self.assertEqual(setups[0].applicant_id, ALLOWED)
        self.assertTrue(nav["formentry_found"])
        actions = [step["action"] for step in payload["steps"]]
        self.assertIn("navigate", actions)
        self.assertIn("fill", actions)
        self.assertIn("click", actions)
        self.assertIn("wait", actions)
        self.assertIn("done", actions)
        self.assertEqual(payload["applicant_id"], ALLOWED)
        self.assertEqual(payload["mode"], "full")
        self.assertFalse(pw.stopped)


if __name__ == "__main__":
    unittest.main()
