"""Unit tests for the Test-only Gemini UI rescue on Playwright sites."""
from __future__ import annotations

import json
import os
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from robie_job_engine.gemini_ui_rescue import (
    GEMINI_API_KEY_SECRET_ID,
    _PAGE_BUDGETS,
    _SAFE_CONTEXT_JS,
    ApiKeyGeminiClient,
    RescueBudget,
    build_rescue_prompt,
    capture_safe_page_context,
    gemini_ui_rescue_budget,
    load_gemini_api_key,
    locator_is_refused,
    retry_failed_locator_action,
    run_named_control_step,
    safe_page_reference,
    wait_visible_or_rescue,
)
from robie_job_engine.locator_registry import FieldLocator, LocatorRegistry, PageLocators
from robie_job_engine.playwright_write_guard import _wrap_write
from robie_job_engine.intake_core import IntakeHold
from robie_job_engine.staff_jobs_common import DEFAULT_GEMINI_KEY_SECRET


def _hold(label: str) -> IntakeHold:
    return IntakeHold(f"Progressive control {label!r} is missing or ambiguous")


class _Locator:
    def __init__(self, count: int, *, visible: bool = True, control_type: str = ""):
        self._count = count
        self._visible = visible
        self._control_type = control_type
        self.clicked = False

    def count(self) -> int:
        return self._count

    def is_visible(self) -> bool:
        if self._count != 1:
            raise AssertionError("refusing a positional visibility check")
        return self._visible

    def click(self) -> None:
        if self._count != 1:
            raise AssertionError("refusing a positional click")
        self.clicked = True

    def wait_for(self, **kwargs: object) -> None:
        if self._count != 1:
            raise AssertionError("refusing a positional wait")

    def get_attribute(self, name: str) -> str:
        if name == "type":
            return self._control_type
        return ""


class _Page:
    def __init__(self) -> None:
        self.url = (
            "https://user:secretpass@www.foragentsonly.com"
            "/managepolicies/policyactivity?token=sekret&password=nope"
        )
        self.nodes: dict[str, _Locator] = {}
        self.selectors: list[str] = []

    def locator(self, selector: str) -> _Locator:
        self.selectors.append(selector)
        return self.nodes.get(selector, _Locator(0))

    def get_by_role(self, role: str, name: str | None = None, exact: bool = True) -> _Locator:
        key = f"{role}:{name}"
        self.selectors.append(key)
        return self.nodes.get(key, _Locator(0))

    def evaluate(self, script: str) -> dict[str, object]:
        return {
            "title": "Policy Activity",
            "labels": [
                "link Communications",
                "button Password",
                "input one-time code",
                "link Communications",
            ],
        }


class _Client:
    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.prompts: list[str] = []

    def generate_unique_locator(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if isinstance(self.raw, BaseException):
            raise self.raw
        return self.raw


class GeminiUiRescueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(os.environ, {"ROBIE_ENV": "TEST"}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.budget = RescueBudget()
        self.scope = gemini_ui_rescue_budget(self.budget)
        self.scope.__enter__()
        self.addCleanup(lambda: self.scope.__exit__(None, None, None))

    def test_secret_id_matches_the_existing_gemini_api_key(self) -> None:
        self.assertEqual(GEMINI_API_KEY_SECRET_ID, "gemini-api-key")
        self.assertIn("/secrets/gemini-api-key/", DEFAULT_GEMINI_KEY_SECRET)
        seen: list[str] = []

        def reader(name: str) -> str:
            seen.append(name)
            return "unit-test-key"

        with patch("robie_job_engine.staff_jobs_common.read_secret", reader):
            self.assertEqual(load_gemini_api_key(), "unit-test-key")
        self.assertEqual(seen, [DEFAULT_GEMINI_KEY_SECRET])

    def test_missing_secret_is_empty_and_holds_not_configured(self) -> None:
        with patch(
            "robie_job_engine.staff_jobs_common.read_secret",
            side_effect=RuntimeError("secret unavailable"),
        ):
            self.assertEqual(load_gemini_api_key(), "")
        page = _Page()
        retried: list[_Locator] = []

        def primary() -> None:
            raise _hold("Start Date")

        with patch(
            "robie_job_engine.gemini_ui_rescue.load_gemini_api_key",
            return_value="",
        ):
            with self.assertRaisesRegex(IntakeHold, "gemini: not_configured") as caught:
                run_named_control_step(
                    page,
                    "Start Date",
                    primary,
                    lambda locator: retried.append(locator),
                )
        self.assertIn("Start Date", str(caught.exception))
        self.assertEqual(retried, [])

    def test_production_skips_rescue(self) -> None:
        page = _Page()
        client = _Client('{"decision":"unique","locator":"a.rescued"}')

        def primary() -> None:
            raise _hold("Communications")

        with patch.dict(
            os.environ,
            {"ROBIE_ENV": "PRODUCTION", "ROBIE_FAO_GEMINI_UI_RESCUE": "1"},
            clear=False,
        ):
            with patch(
                "robie_job_engine.gemini_ui_rescue.build_default_client",
                return_value=client,
            ):
                with self.assertRaisesRegex(IntakeHold, "Communications") as caught:
                    run_named_control_step(page, "Communications", primary, lambda locator: None)
        self.assertNotIn("gemini:", str(caught.exception))
        self.assertEqual(client.prompts, [])
        self.assertFalse(self.budget.used)

    def test_unique_control_does_not_call_gemini(self) -> None:
        def forbid() -> None:
            raise AssertionError("Gemini was called for a unique control")

        with patch("robie_job_engine.gemini_ui_rescue.build_default_client", forbid):
            result = run_named_control_step(
                _Page(),
                "Communications",
                lambda: "already-unique",
                lambda locator: (_ for _ in ()).throw(AssertionError("retried")),
            )
        self.assertEqual(result, "already-unique")
        self.assertFalse(self.budget.used)

    def test_unique_locator_retries_once_including_timeout(self) -> None:
        page = _Page()
        rescued = _Locator(1)
        page.nodes["a.rescued"] = rescued
        client = _Client(json.dumps({"decision": "unique", "locator": "a.rescued"}))
        calls = {"primary": 0}

        def primary() -> None:
            calls["primary"] += 1
            raise TimeoutError("control wait timed out")

        with patch(
            "robie_job_engine.gemini_ui_rescue.build_default_client",
            return_value=client,
        ):
            run_named_control_step(page, "Communications", primary, lambda locator: locator.click())
        self.assertEqual(calls["primary"], 1)
        self.assertEqual(len(client.prompts), 1)
        self.assertTrue(rescued.clicked)
        self.assertIn("www.foragentsonly.com", client.prompts[0])
        self.assertNotIn("sekret", client.prompts[0])
        self.assertNotIn("secretpass", client.prompts[0])
        self.assertNotIn("password=nope", client.prompts[0])
        self.assertIn("Do not use .first", client.prompts[0])

    def test_unsure_ambiguous_and_positional_hold_without_a_click(self) -> None:
        page = _Page()
        many = _Locator(2)
        page.nodes["a.many"] = many
        hidden = _Locator(1, visible=False)
        page.nodes["a.hidden"] = hidden
        clicked: list[str] = []

        def retry(locator: _Locator) -> None:
            clicked.append("clicked")
            locator.click()

        cases = (
            ('{"decision":"unsure","reason":"two buttons"}', "gemini: unsure"),
            ('{"decision":"unique","locator":"a.many","locators":["a.many","a.other"]}', "gemini: ambiguous"),
            ('{"decision":"unique","locator":"a.many"}', "gemini: ambiguous"),
            ('{"decision":"unique","locator":"a.hidden"}', "gemini: ambiguous"),
            ('{"decision":"unique","locator":"button >> nth=0"}', "gemini: ambiguous"),
            ('{"decision":"unique","locator":"a.rescued.first"}', "gemini: ambiguous"),
        )
        for raw, code in cases:
            with self.subTest(raw=raw):
                self.budget.used = False
                client = _Client(raw)
                with patch(
                    "robie_job_engine.gemini_ui_rescue.build_default_client",
                    return_value=client,
                ):
                    with self.assertRaisesRegex(IntakeHold, code):
                        run_named_control_step(
                            page,
                            "Communications",
                            lambda: (_ for _ in ()).throw(_hold("Communications")),
                            retry,
                        )
                self.assertEqual(clicked, [])
                self.assertFalse(many.clicked)
                self.assertNotIn("button >> nth=0", page.selectors)
                self.assertNotIn("a.rescued.first", page.selectors)

    def test_request_failure_does_not_echo_the_key_or_crash(self) -> None:
        client = _Client(RuntimeError("https://example.test?key=unit-test-key"))
        with patch(
            "robie_job_engine.gemini_ui_rescue.build_default_client",
            return_value=client,
        ):
            with self.assertRaisesRegex(IntakeHold, "gemini: request_failed") as caught:
                run_named_control_step(
                    _Page(),
                    "Memo",
                    lambda: (_ for _ in ()).throw(
                        IntakeHold("Memo open control is missing or ambiguous")
                    ),
                    lambda locator: None,
                )
        self.assertNotIn("unit-test-key", str(caught.exception))

    def test_api_client_puts_the_key_in_the_query_and_not_the_error(self) -> None:
        captured: list[urllib.request.Request] = []

        def opener(request: urllib.request.Request, timeout: float | None = None):
            captured.append(request)
            raise urllib.error.URLError("down")

        client = ApiKeyGeminiClient("unit-test-key", opener=opener)
        with self.assertRaises(RuntimeError) as caught:
            client.generate_unique_locator("name one control")
        self.assertNotIn("unit-test-key", str(caught.exception))
        self.assertEqual(len(captured), 1)
        self.assertIn("key=unit-test-key", captured[0].full_url)
        self.assertNotIn(b"unit-test-key", captured[0].data or b"")
        self.assertIn(b"name one control", captured[0].data or b"")

    def test_safe_context_drops_secrets_and_query(self) -> None:
        host, url = safe_page_reference(_Page().url)
        self.assertEqual(host, "www.foragentsonly.com")
        self.assertEqual(url, "https://www.foragentsonly.com/managepolicies/policyactivity")
        context = capture_safe_page_context(_Page())
        self.assertEqual(context["labels"], ["link Communications"])
        self.assertNotIn("password", " ".join(context["labels"]).casefold())
        prompt = build_rescue_prompt(
            label="Communications",
            context=context,
            reason="Progressive control 'Communications' is missing or ambiguous",
        )
        self.assertNotIn("sekret", prompt)
        self.assertNotIn("Password", prompt)
        self.assertIn("link Communications", prompt)
        self.assertNotIn("body.innerText", _SAFE_CONTEXT_JS)
        self.assertNotIn("document.body", _SAFE_CONTEXT_JS)

    def test_positional_markers_are_refused(self) -> None:
        for selector in (
            "button.first",
            "a >> nth=0",
            "div:nth-child(1)",
            ".last",
            "option:first-child",
        ):
            with self.subTest(selector=selector):
                self.assertTrue(locator_is_refused(selector))

    def test_rescue_module_does_not_file_documents_or_notes(self) -> None:
        text = Path("robie_job_engine/gemini_ui_rescue.py").read_text(encoding="utf-8")
        for banned in (
            "upload_applicant_document",
            "add_note_to_discussion",
            "DiscussionApi",
            "DocumentApi",
            "ROBIE_DOCUMENT_RETRIEVAL_FILE_EZLYNX",
            "create_task_once",
        ):
            self.assertNotIn(banned, text)


class SharedPlaywrightRescueTests(unittest.TestCase):
    """Registry, write guard, and page budget. No FAO ContextVar scope."""

    def setUp(self) -> None:
        self.env = patch.dict(os.environ, {"ROBIE_ENV": "TEST"}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        _PAGE_BUDGETS.clear()
        self.addCleanup(_PAGE_BUDGETS.clear)

    def test_call_sites_use_the_shared_helper(self) -> None:
        root = Path("robie_job_engine")
        expectations = {
            "locator_registry.py": "retry_failed_locator_action",
            "playwright_write_guard.py": "retry_failed_locator_action",
            "ascend_locator_audit_runner.py": "wait_visible_or_rescue",
            "ezlynx_policy_setup.py": "wait_visible_or_rescue_async",
            "submission_audit_runner.py": "retry_failed_locator_action",
            "je_kill_live.py": "retry_failed_locator_action",
            "progressive_fao_memo.py": "run_named_control_step",
        }
        for name, needle in expectations.items():
            with self.subTest(name=name):
                self.assertIn(needle, (root / name).read_text(encoding="utf-8"))
        guard = (root / "playwright_write_guard.py").read_text(encoding="utf-8")
        deploy = Path("deploy/hermes/tools/playwright_write_guard.py").read_text(
            encoding="utf-8"
        )
        self.assertEqual(guard, deploy)
        wrapped = guard.split("def _wrap_write", 1)[1].split(
            "def _wrap_destructive_press_only", 1
        )[0]
        self.assertLess(
            wrapped.index("playwright_note_doc_block_reason"),
            wrapped.index("_test_ui_rescue_control"),
        )
        self.assertLess(
            wrapped.index("destructive_action_block_reason"),
            wrapped.index("_test_ui_rescue_control"),
        )
        self.assertIn("add_discussion_note", (root / "ezlynx_policy_setup.py").read_text(encoding="utf-8"))
        self.assertIn(
            "refuse_playwright_note_or_doc",
            (root / "ezlynx_policy_setup.py").read_text(encoding="utf-8"),
        )

    def _registry(self) -> LocatorRegistry:
        registry = LocatorRegistry(registry_dir=Path("/tmp/robie-no-locator-json"))
        page = PageLocators(portal="ezlynx", page_name="document_library")
        page.add_field(
            FieldLocator(
                name="apply_button",
                primary_strategy="css",
                primary_selector="#apply",
            )
        )
        registry.register_page(page)
        return registry

    def test_registry_unique_control_does_not_call_gemini(self) -> None:
        page = _Page()
        page.nodes["#apply"] = _Locator(1)

        def forbid() -> None:
            raise AssertionError("Gemini was called for a unique registry locator")

        with patch("robie_job_engine.gemini_ui_rescue.build_default_client", forbid):
            found = self._registry().resolve_element(
                page, "ezlynx", "document_library", "apply_button"
            )
        self.assertIs(found, page.nodes["#apply"])

    def test_registry_miss_retries_one_unique_locator(self) -> None:
        page = _Page()
        page.nodes["#apply"] = _Locator(0)
        rescued = _Locator(1)
        page.nodes["#rescued"] = rescued
        client = _Client(json.dumps({"decision": "unique", "locator": "#rescued"}))
        with patch(
            "robie_job_engine.gemini_ui_rescue.build_default_client",
            return_value=client,
        ):
            found = self._registry().resolve_element(
                page, "ezlynx", "document_library", "apply_button"
            )
        self.assertIs(found, rescued)
        self.assertEqual(len(client.prompts), 1)

    def test_registry_production_keeps_the_blocked_message(self) -> None:
        page = _Page()
        page.nodes["#apply"] = _Locator(0)
        client = _Client(json.dumps({"decision": "unique", "locator": "#rescued"}))
        with patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION"}, clear=False):
            with patch(
                "robie_job_engine.gemini_ui_rescue.build_default_client",
                return_value=client,
            ):
                with self.assertRaises(RuntimeError) as caught:
                    self._registry().resolve_element(
                        page, "ezlynx", "document_library", "apply_button"
                    )
        self.assertIn("resolved to 0 elements", str(caught.exception))
        self.assertNotIn("gemini:", str(caught.exception))
        self.assertEqual(client.prompts, [])

    def test_registry_missing_key_is_playwright_blocked_not_configured(self) -> None:
        page = _Page()
        page.nodes["#apply"] = _Locator(0)
        with patch(
            "robie_job_engine.gemini_ui_rescue.build_default_client",
            return_value=None,
        ):
            with self.assertRaises(RuntimeError) as caught:
                self._registry().resolve_element(
                    page, "ezlynx", "document_library", "apply_button"
                )
        message = str(caught.exception)
        self.assertIn("PLAYWRIGHT_BLOCKED", message)
        self.assertIn("gemini: not_configured", message)
        self.assertNotIsInstance(caught.exception, IntakeHold)

    def test_page_budget_asks_once_without_a_scope(self) -> None:
        page = _Page()
        page.nodes["a.rescued"] = _Locator(1)
        client = _Client(json.dumps({"decision": "unique", "locator": "a.rescued"}))
        failure = TimeoutError("locator wait timed out")
        with patch(
            "robie_job_engine.gemini_ui_rescue.build_default_client",
            return_value=client,
        ):
            retry_failed_locator_action(page, "Apply", failure, lambda locator: locator)
            with self.assertRaises(TimeoutError):
                retry_failed_locator_action(
                    page, "Apply", failure, lambda locator: locator
                )
        self.assertEqual(len(client.prompts), 1)

    def test_visible_wait_retries_once_on_test(self) -> None:
        page = _Page()
        rescued = _Locator(1)
        page.nodes["button.ready"] = rescued
        client = _Client(json.dumps({"decision": "unique", "locator": "button.ready"}))
        waits = {"n": 0}

        class _Waiting:
            def wait_for(self, **kwargs: object) -> None:
                waits["n"] += 1
                raise TimeoutError("timed out")

        with patch(
            "robie_job_engine.gemini_ui_rescue.build_default_client",
            return_value=client,
        ):
            found = wait_visible_or_rescue(page, _Waiting(), "New program", 1000)
        self.assertIs(found, rescued)
        self.assertEqual(waits["n"], 1)
        self.assertEqual(len(client.prompts), 1)

    def test_write_guard_on_test_retries_the_rescued_locator(self) -> None:
        page = _Page()
        rescued = _Locator(1)
        page.nodes["#go"] = rescued
        client = _Client(json.dumps({"decision": "unique", "locator": "#go"}))
        seen: list[object] = []

        class _Owner:
            def __init__(self) -> None:
                self.page = page

            def count(self) -> int:
                return 0

        def original(target: object, *args: object, **kwargs: object) -> str:
            seen.append(target)
            return "wrote"

        wrapped = _wrap_write(
            original,
            page_level=False,
            scope={},
            locator_originals={"original": original},
        )
        with patch(
            "robie_job_engine.gemini_ui_rescue.build_default_client",
            return_value=client,
        ):
            self.assertEqual(wrapped(_Owner()), "wrote")
        self.assertEqual(seen, [rescued])
        self.assertEqual(len(client.prompts), 1)

    def test_write_guard_production_keeps_the_vertex_path(self) -> None:
        page = _Page()

        class _Owner:
            def __init__(self) -> None:
                self.page = page

            def count(self) -> int:
                return 0

        def original(target: object, *args: object, **kwargs: object) -> str:
            raise AssertionError("original write ran")

        wrapped = _wrap_write(
            original,
            page_level=False,
            scope={},
            locator_originals={},
        )
        with patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION"}, clear=False):
            with patch(
                "robie_job_engine.gemini_ui_rescue.build_default_client",
                side_effect=AssertionError("Test rescue ran on Production"),
            ):
                with patch(
                    "robie_job_engine.playwright_write_guard._gemini_then_write_or_hitl",
                    return_value="vertex-hitl",
                ):
                    self.assertEqual(wrapped(_Owner()), "vertex-hitl")


if __name__ == "__main__":
    unittest.main()
