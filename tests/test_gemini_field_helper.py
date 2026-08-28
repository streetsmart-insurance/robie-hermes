"""Fail-closed Gemini stuck-field helper. No live Vertex. No bind."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from urllib.error import HTTPError
from io import BytesIO

from robie_job_engine.gemini_field_helper import (
    VertexGeminiFieldClient,
    ask_gemini_unique_field,
    build_gemini_unique_field_prompt,
    describe_blocked_dialog,
    resolve_blocked_unique_write,
)
from robie_job_engine.playwright_write_guard import require_unique_write_target
from robie_job_engine.secrets import FAKE_SECRET_SENTINEL


ROOT = Path(__file__).resolve().parents[1]
SKILL_COPIES = (
    (
        ROOT / "deploy/hermes/skills/ezlynx-commercial-auto-from-quote/SKILL.md",
        ROOT / "skills/ezlynx-commercial-auto-from-quote/SKILL.md",
    ),
    (
        ROOT / "deploy/hermes/skills/ezlynx-gemini-fallback/SKILL.md",
        ROOT / "skills/ezlynx-gemini-fallback/SKILL.md",
    ),
)


class FakeGeminiClient:
    def __init__(self, payload: str | Exception):
        self.payload = payload
        self.prompts: list[str] = []

    def generate_unique_field(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeLocator:
    def __init__(self, matches: int, selector: str):
        self.matches = matches
        self.selector = selector
        self.fills: list[str] = []

    def count(self):
        return self.matches

    def fill(self, value: str):
        require_unique_write_target(self)
        self.fills.append(value)


class GeminiFieldHelperTests(unittest.TestCase):
    def test_overlay_skills_match_repo_ezlynx_skill_folders(self):
        for overlay, repo in SKILL_COPIES:
            self.assertEqual(overlay.read_text(), repo.read_text(), msg=overlay.name)

    def test_commercial_auto_skill_requires_after_shell_edit_and_forbids_bind(self):
        text = ROOT.joinpath(
            "deploy/hermes/skills/ezlynx-commercial-auto-from-quote/SKILL.md"
        ).read_text()
        for required in (
            "Save and Continue Edit",
            "Actions → Edit",
            "vehicles",
            "drivers",
            "garaging",
            "symbols",
            "limits",
            "banks",
            "Robie was here",
            "Untitled",
            "named insured",
            "Do not bind",
            "Do not invent coverage",
            "ezlynx-gemini-fallback",
            "HITL Carlo",
            "PLAYWRIGHT_BLOCKED",
            "Do **not** only stop and report",
            "/web/account/<id>/policies",
            "Do not guess Summary, Details, or Index",
        ):
            self.assertIn(required, text)
        self.assertNotIn("click Bind", text)
        self.assertIn("Do not take payment", text)

    def test_note_rule_is_not_commercial_auto_only(self):
        fallback = ROOT.joinpath(
            "deploy/hermes/skills/ezlynx-gemini-fallback/SKILL.md"
        ).read_text()
        self.assertIn("every EZLynx note write", fallback)
        self.assertIn("Robie was here", fallback)
        self.assertIn("Never write a note on Untitled", fallback)
        self.assertNotIn("EZLynx Gemini fallback", fallback)
        self.assertIn("stuck Playwright write on any site", fallback)

    def test_password_labels_are_stripped_before_gemini(self):
        description = describe_blocked_dialog(
            dialog_title="Sign in",
            visible_labels=["VIN", "Password", "password: hunter2", FAKE_SECRET_SENTINEL],
            block_reason="PLAYWRIGHT_BLOCKED: matched 2 fields",
        )
        self.assertEqual(description["visible_labels"], ["VIN"])
        self.assertNotIn("hunter2", json.dumps(description))
        self.assertNotIn(FAKE_SECRET_SENTINEL, json.dumps(description))

    def test_unique_visible_label_may_apply_and_still_pass_unique_write(self):
        client = FakeGeminiClient(
            json.dumps(
                {
                    "decision": "unique",
                    "field_label": "VIN",
                    "locator": "get_by_label('VIN')",
                }
            )
        )
        decision = resolve_blocked_unique_write(
            block_reason="PLAYWRIGHT_BLOCKED: matched 2 fields",
            dialog_title="Add Vehicle",
            visible_labels=["VIN", "Year"],
            client=client,
        )
        self.assertEqual(decision.action, "APPLY")
        self.assertEqual(decision.field_label, "VIN")
        self.assertEqual(decision.locator, "get_by_label('VIN')")
        self.assertTrue(decision.gemini_asked)
        target = FakeLocator(1, "get_by_label('VIN')")
        target.fill("1HGCM82633A004352")
        self.assertEqual(target.fills, ["1HGCM82633A004352"])

    def test_positional_gemini_locator_is_hitl_and_does_not_write(self):
        for locator in (
            "page.locator('input').first",
            "page.locator('input').nth(0)",
            "page.locator('input').last",
        ):
            with self.subTest(locator=locator):
                decision = ask_gemini_unique_field(
                    dialog_title="Add Vehicle",
                    visible_labels=["VIN"],
                    block_reason="PLAYWRIGHT_BLOCKED: matched 2 fields",
                    client=FakeGeminiClient(
                        json.dumps(
                            {
                                "decision": "unique",
                                "field_label": "VIN",
                                "locator": locator,
                            }
                        )
                    ),
                )
                self.assertEqual(decision.action, "HITL")
                self.assertEqual(decision.hitl_operator, "Carlo")
                self.assertIn("positional", decision.reason.casefold())
                blocked = FakeLocator(2, locator)
                with self.assertRaisesRegex(RuntimeError, "PLAYWRIGHT_BLOCKED"):
                    blocked.fill("guess")
                self.assertEqual(blocked.fills, [])

    def test_unsure_or_unknown_label_or_missing_client_is_hitl(self):
        unsure = ask_gemini_unique_field(
            dialog_title="Add Vehicle",
            visible_labels=["VIN", "Year"],
            client=FakeGeminiClient('{"decision":"unsure","reason":"two VIN boxes"}'),
        )
        self.assertEqual(unsure.action, "HITL")
        self.assertIn("Carlo", unsure.reason)

        invented = ask_gemini_unique_field(
            dialog_title="Add Vehicle",
            visible_labels=["VIN"],
            client=FakeGeminiClient(
                '{"decision":"unique","field_label":"SSN","locator":"get_by_label(\'SSN\')"}'
            ),
        )
        self.assertEqual(invented.action, "HITL")

        missing = ask_gemini_unique_field(
            dialog_title="Add Vehicle",
            visible_labels=["VIN"],
            client=None,
        )
        self.assertEqual(missing.action, "HITL")
        self.assertFalse(missing.gemini_asked)

        failed = ask_gemini_unique_field(
            dialog_title="Add Vehicle",
            visible_labels=["VIN"],
            client=FakeGeminiClient(TimeoutError("vertex down")),
        )
        self.assertEqual(failed.action, "HITL")
        self.assertTrue(failed.gemini_asked)

    def test_vertex_http_error_fails_closed(self):
        def opener(request, timeout=None):
            raise HTTPError(
                url=request.full_url,
                code=403,
                msg="forbidden",
                hdrs=None,
                fp=BytesIO(b""),
            )

        client = VertexGeminiFieldClient(
            project="streetsmart-test",
            token_provider=lambda: "test-token",
            opener=opener,
        )
        with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
            client.generate_unique_field("name one field")

    def test_unconfigured_vertex_client_is_not_used(self):
        client = VertexGeminiFieldClient(project="", location="us-central1", model="gemini-2.5-flash")
        self.assertFalse(client.configured())

    def test_prompt_names_current_page_not_ezlynx_write(self):
        with_host = build_gemini_unique_field_prompt(
            dialog_title="Quote Details",
            visible_labels=["Carrier"],
            block_reason=(
                "PLAYWRIGHT_BLOCKED: write target input[name='quotes.0.carrier_id'] "
                "is hidden / combobox-hidden value; ask Gemini then HITL Carlo; "
                "do not retry-loop"
            ),
            page_url="https://app.ascend.com/quotes/123?token=should-not-appear",
        )
        self.assertNotIn("EZLynx write", with_host)
        self.assertIn("app.ascend.com", with_host)
        self.assertIn("Quote Details", with_host)
        self.assertIn("Carrier", with_host)
        self.assertIn("hidden / combobox-hidden", with_host)
        self.assertNotIn("should-not-appear", with_host)
        self.assertNotIn("token=", with_host)

        untitled = build_gemini_unique_field_prompt(
            dialog_title="Add Vehicle",
            visible_labels=["VIN"],
            block_reason="PLAYWRIGHT_BLOCKED: matched 2 fields",
        )
        self.assertNotIn("EZLynx write", untitled)
        self.assertIn("Add Vehicle", untitled)
        self.assertIn("VIN", untitled)

    def test_ascend_hidden_field_still_apply_or_hitl(self):
        apply_client = FakeGeminiClient(
            json.dumps(
                {
                    "decision": "unique",
                    "field_label": "Carrier",
                    "locator": "get_by_label('Carrier')",
                }
            )
        )
        applied = resolve_blocked_unique_write(
            block_reason=(
                "PLAYWRIGHT_BLOCKED: write target input[name='quotes.0.carrier_id'] "
                "is hidden / combobox-hidden value; ask Gemini then HITL Carlo"
            ),
            dialog_title="Quote Details",
            visible_labels=["Carrier"],
            page_url="https://app.ascend.com/quotes/123",
            client=apply_client,
        )
        self.assertEqual(applied.action, "APPLY")
        self.assertEqual(applied.field_label, "Carrier")
        self.assertEqual(applied.locator, "get_by_label('Carrier')")
        self.assertTrue(applied.gemini_asked)
        self.assertEqual(len(apply_client.prompts), 1)
        self.assertNotIn("EZLynx write", apply_client.prompts[0])
        self.assertIn("app.ascend.com", apply_client.prompts[0])

        hitl = resolve_blocked_unique_write(
            block_reason=(
                "PLAYWRIGHT_BLOCKED: write target is hidden / combobox-hidden value; "
                "ask Gemini then HITL Carlo"
            ),
            dialog_title="Quote Details",
            visible_labels=["Carrier"],
            page_url="https://app.ascend.com/quotes/123",
            client=FakeGeminiClient('{"decision":"unsure","reason":"hidden combobox"}'),
        )
        self.assertEqual(hitl.action, "HITL")
        self.assertEqual(hitl.hitl_operator, "Carlo")
        self.assertIn("Carlo", hitl.reason)
        self.assertTrue(hitl.gemini_asked)


if __name__ == "__main__":
    unittest.main()
