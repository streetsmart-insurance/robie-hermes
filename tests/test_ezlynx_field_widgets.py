"""Shared live-option dropdown helper. Mock locators only. No live EZLynx."""

from __future__ import annotations

import asyncio
import json
import unittest

from robie_job_engine.ezlynx_field_widgets import (
    BILLING_TYPE_WIDGET,
    DEPARTMENT_WIDGET,
    LOB_WIDGET_ROOT,
    FieldWidget,
    fill_identified_widget,
    fill_live_dropdown,
    identified_widget,
)
from robie_job_engine.gemini_field_helper import ask_gemini_live_option, exact_live_option


class FakeGemini:
    def __init__(self, payload: str, content: str | None = None):
        self.payload = payload
        self.content = content
        self.prompts: list[str] = []
        self.content_prompts: list[str] = []

    def generate_unique_field(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.payload

    def generate_content(self, prompt: str) -> str:
        self.content_prompts.append(prompt)
        if self.content is not None:
            return self.content
        return self.payload


class _Node:
    def __init__(self, text: str = "", children: list["_Node"] | None = None, element_id: str = ""):
        self._text = text
        self._children = children or []
        self._id = element_id
        self.clicked = False
        self.selected_label: str | None = None

    @property
    def first(self) -> "_Node":
        return self

    async def count(self) -> int:
        if self._children:
            return len(self._children)
        return 1 if self._text or self._id or self.clicked is not None else 0

    async def click(self) -> None:
        self.clicked = True

    async def inner_text(self) -> str:
        return self._text

    async def all_inner_texts(self) -> list[str]:
        if self._children:
            return [child._text for child in self._children]
        return [self._text] if self._text else []

    def all(self) -> list["_Node"]:
        return list(self._children) if self._children else [self]

    def nth(self, index: int) -> "_Node":
        if self._children:
            return self._children[index]
        return self

    def locator(self, selector: str) -> "_Node":
        if "option" in selector:
            return _Node(children=list(self._children))
        return self

    async def get_attribute(self, name: str) -> str:
        return self._id if name == "id" else ""

    async def select_option(self, label: str | None = None, value: str | None = None) -> None:
        self.selected_label = label or value
        self._text = self.selected_label or ""

    async def input_value(self) -> str:
        return self.selected_label or ""


class EditPolicyPage:
    """Edit Policy page: #Department select2 plus a LOB native select trap."""

    def __init__(
        self,
        department_options: list[str],
        billing_options: list[str] | None = None,
        lob_options: list[str] | None = None,
        department_present: bool = True,
        lob_orig_date: str | None = None,
    ) -> None:
        self.department_options = list(department_options)
        self.billing_options = list(billing_options or ["Agency", "Direct"])
        self.lob_options = list(
            lob_options or ["Homeowners", "Auto (Personal)", "Auto (Commercial)"]
        )
        self.department_present = department_present
        self.dept_selected = ""
        self.billing_selected = ""
        self.lob_touched = False
        self.opened_department = False
        self.scanned_all_selects = False
        self.lob_orig_present = lob_orig_date is not None
        self.lob_orig_date = lob_orig_date or ""

    async def wait_for_timeout(self, _ms: int) -> None:
        return None

    def _department_rows(self) -> _Node:
        nodes = []
        for opt in self.department_options:
            node = _Node(text=opt)

            async def _click(_node=node, label=opt) -> None:
                _node.clicked = True
                self.dept_selected = label

            node.click = _click  # type: ignore[method-assign]
            nodes.append(node)
        return _Node(children=nodes)

    def locator(self, selector: str) -> _Node:
        if selector == "select":
            self.scanned_all_selects = True
            return _Node(
                children=[_Node(text=opt) for opt in self.lob_options],
                element_id="mergeSplitLOB",
            )
        if LOB_WIDGET_ROOT.lstrip("#") in selector and "Department" not in selector:
            self.lob_touched = True
            return _Node(
                children=[_Node(text=opt) for opt in self.lob_options],
                element_id="mergeSplitLOB",
            )
        if selector == "#Department":
            root = _Node(element_id="Department")
            if not self.department_present:

                async def _missing() -> int:
                    return 0

                root.count = _missing  # type: ignore[method-assign]
            return root
        if selector == DEPARTMENT_WIDGET.trigger or (
            selector.startswith("#Department")
            and ("select2-choice" in selector or "ui-select-match" in selector)
            and "choices-row" not in selector
        ):
            trigger = _Node(text=self.dept_selected or "---Select---")

            async def _open() -> None:
                self.opened_department = True

            async def _text() -> str:
                return self.dept_selected or "---Select---"

            trigger.click = _open  # type: ignore[method-assign]
            trigger.inner_text = _text  # type: ignore[method-assign]
            return trigger
        if "ui-select-choices-row" in selector and "Department" in selector:
            return self._department_rows()
        if selector == "#BillingType option" or selector.endswith("#BillingType option"):
            return _Node(children=[_Node(text=opt) for opt in self.billing_options])
        if "option:checked" in selector and "BillingType" in selector:
            return _Node(text=self.billing_selected)
        if selector == "#LOBOriginationDate":
            if not self.lob_orig_present:
                missing = _Node()

                async def _zero() -> int:
                    return 0

                missing.count = _zero  # type: ignore[method-assign]
                return missing
            field = _Node(element_id="LOBOriginationDate")

            async def _value() -> str:
                return self.lob_orig_date

            async def _fill(value: str) -> None:
                self.lob_orig_date = value
                field.selected_label = value

            field.input_value = _value  # type: ignore[method-assign]
            field.fill = _fill  # type: ignore[method-assign]
            return field
        if selector == "#BillingType" or selector == BILLING_TYPE_WIDGET.root:
            select = _Node(
                children=[_Node(text=opt) for opt in self.billing_options],
                element_id="BillingType",
            )

            async def _select(label: str | None = None, value: str | None = None) -> None:
                chosen = label or value or ""
                self.billing_selected = chosen
                select.selected_label = chosen

            async def _value() -> str:
                return self.billing_selected

            def _child(sub: str) -> _Node:
                if "checked" in sub:
                    return _Node(text=self.billing_selected)
                return _Node(children=[_Node(text=opt) for opt in self.billing_options])

            select.select_option = _select  # type: ignore[method-assign]
            select.input_value = _value  # type: ignore[method-assign]
            select.locator = _child  # type: ignore[method-assign]
            return select
        return _Node()


class LiveOptionHelperTests(unittest.TestCase):
    def test_exact_match_does_not_need_gemini(self) -> None:
        decision = ask_gemini_live_option(
            widget_name="Department",
            wanted="Personal",
            live_options=["---Select---", "Personal"],
            client=FakeGemini('{"decision":"unsure","reason":"should not be asked"}'),
        )
        self.assertEqual(decision.action, "APPLY")
        self.assertEqual(decision.option, "Personal")
        self.assertFalse(decision.gemini_asked)

    def test_gemini_names_one_live_option(self) -> None:
        live = ["---Select---", "Personal Lines (P/L)", "Certificates Team (COI)"]
        decision = ask_gemini_live_option(
            widget_name="Department",
            wanted="Personal",
            live_options=live,
            client=FakeGemini(
                json.dumps({"decision": "unique", "option": "Personal Lines (P/L)"})
            ),
        )
        self.assertEqual(decision.action, "APPLY")
        self.assertEqual(decision.option, "Personal Lines (P/L)")
        self.assertTrue(decision.gemini_asked)

    def test_gemini_names_billing_live_option(self) -> None:
        decision = ask_gemini_live_option(
            widget_name="Billing Type",
            wanted="Direct Bill",
            live_options=["---Select---", "Agency", "Direct"],
            client=FakeGemini(json.dumps({"decision": "unique", "option": "Direct"})),
        )
        self.assertEqual(decision.action, "APPLY")
        self.assertEqual(decision.option, "Direct")
        self.assertTrue(decision.gemini_asked)

    def test_production_exact_miss_asks_gemini_and_applies_named_live_option(self) -> None:
        client = FakeGemini(json.dumps({"decision": "unique", "option": "Direct"}))
        decision = ask_gemini_live_option(
            widget_name="Billing Type",
            wanted="Direct Bill",
            live_options=["---Select---", "Agency", "Direct"],
            client=client,
        )
        self.assertEqual(decision.action, "APPLY")
        self.assertEqual(decision.option, "Direct")
        self.assertTrue(decision.gemini_asked)
        self.assertEqual(len(client.prompts), 1)
        self.assertIn("Direct Bill", client.prompts[0])
        self.assertIn("- Direct", client.prompts[0])
        self.assertIn("Do not answer unsure just because", client.prompts[0])

    def test_unsure_json_then_plain_live_option_on_retry_is_applied(self) -> None:
        client = FakeGemini(
            json.dumps(
                {
                    "decision": "unsure",
                    "reason": "Wanted value 'Direct Bill' has no exact match in the live options.",
                }
            ),
            content="Direct",
        )
        decision = ask_gemini_live_option(
            widget_name="Billing Type",
            wanted="Direct Bill",
            live_options=["---Select---", "Agency", "Direct"],
            client=client,
        )
        self.assertEqual(decision.action, "APPLY")
        self.assertEqual(decision.option, "Direct")
        self.assertTrue(client.content_prompts)

    def test_gemini_unsure_is_hitl_no_option(self) -> None:
        decision = ask_gemini_live_option(
            widget_name="Department",
            wanted="Personal",
            live_options=["---Select---", "Personal Lines (P/L)"],
            client=FakeGemini('{"decision":"unsure","reason":"truncated"}'),
        )
        self.assertEqual(decision.action, "HITL")
        self.assertIsNone(decision.option)
        self.assertIn("Carlo", decision.reason)

    def test_gemini_invented_option_is_hitl(self) -> None:
        decision = ask_gemini_live_option(
            widget_name="Department",
            wanted="Personal",
            live_options=["---Select---", "Personal Lines (P/L)"],
            client=FakeGemini(json.dumps({"decision": "unique", "option": "Auto (Personal)"})),
        )
        self.assertEqual(decision.action, "HITL")
        self.assertIsNone(decision.option)

    def test_substring_is_not_an_exact_live_match(self) -> None:
        self.assertIsNone(exact_live_option("Personal", ["Auto (Personal)"]))


class WidgetFillTests(unittest.TestCase):
    def test_department_gemini_apply_selects_live_option_not_lob(self) -> None:
        async def _run() -> None:
            page = EditPolicyPage(
                department_options=[
                    "---Select---",
                    "Personal Lines (P/L)",
                    "Certificates Team (COI)",
                    "Commercial Lines (CL)",
                    "Trucking (TL)",
                ]
            )
            client = FakeGemini(
                json.dumps({"decision": "unique", "option": "Personal Lines (P/L)"})
            )
            result = await fill_live_dropdown(
                page, DEPARTMENT_WIDGET, "Personal", gemini_client=client
            )
            self.assertFalse(result.hitl)
            self.assertTrue(result.gemini_applied)
            self.assertEqual(result.selected, "Personal Lines (P/L)")
            self.assertEqual(page.dept_selected, "Personal Lines (P/L)")
            self.assertFalse(page.lob_touched)
            self.assertFalse(page.scanned_all_selects)
            self.assertTrue(page.opened_department)

        asyncio.run(_run())

    def test_billing_exact_miss_applies_gemini_live_option_and_retries(self) -> None:
        async def _run() -> None:
            page = EditPolicyPage(
                department_options=["---Select---"],
                billing_options=["---Select---", "Agency", "Direct"],
            )
            result = await fill_identified_widget(
                page,
                BILLING_TYPE_WIDGET,
                "Direct Bill",
                gemini_client=FakeGemini(
                    json.dumps({"decision": "unique", "option": "Direct"})
                ),
            )
            self.assertFalse(result.hitl, result.error)
            self.assertTrue(result.gemini_applied)
            self.assertEqual(result.selected, "Direct")
            self.assertEqual(page.billing_selected, "Direct")
            self.assertGreaterEqual(result.attempts, 1)

        asyncio.run(_run())

    def test_billing_type_uses_same_helper(self) -> None:
        async def _run() -> None:
            page = EditPolicyPage(
                department_options=["---Select---", "Personal Lines (P/L)"],
                billing_options=["Agency", "Direct"],
            )
            client = FakeGemini(json.dumps({"decision": "unique", "option": "Direct"}))
            result = await fill_identified_widget(
                page, BILLING_TYPE_WIDGET, "Direct Bill", gemini_client=client
            )
            self.assertFalse(result.hitl)
            self.assertEqual(result.selected, "Direct")
            self.assertEqual(page.billing_selected, "Direct")

        asyncio.run(_run())

    def test_gemini_unsure_does_not_select(self) -> None:
        async def _run() -> None:
            page = EditPolicyPage(
                department_options=["---Select---", "Personal Lines (P/L)"]
            )
            result = await fill_identified_widget(
                page,
                DEPARTMENT_WIDGET,
                "Personal",
                gemini_client=FakeGemini('{"decision":"unsure","reason":"cannot tell"}'),
            )
            self.assertTrue(result.hitl)
            self.assertFalse(result.gemini_applied)
            self.assertEqual(page.dept_selected, "")
            self.assertIn("Personal Lines (P/L)", result.error or "")

        asyncio.run(_run())

    def test_missing_department_does_not_fall_back_to_lob_select(self) -> None:
        async def _run() -> None:
            page = EditPolicyPage(department_options=[], department_present=False)
            with self.assertRaisesRegex(RuntimeError, "#Department"):
                await fill_identified_widget(
                    page,
                    DEPARTMENT_WIDGET,
                    "Personal",
                    gemini_client=FakeGemini(
                        json.dumps({"decision": "unique", "option": "Auto (Personal)"})
                    ),
                )
            self.assertFalse(page.scanned_all_selects)
            self.assertFalse(page.lob_touched)

        asyncio.run(_run())

    def test_combobox_kind_uses_same_helper(self) -> None:
        async def _run() -> None:
            widget = FieldWidget(
                name="Department",
                root="#Department",
                kind="combobox",
                trigger="#Department a.ui-select-match",
                option_rows="#Department .ui-select-choices-row",
            )
            page = EditPolicyPage(
                department_options=["---Select---", "Personal Lines (P/L)"]
            )
            result = await fill_identified_widget(
                page,
                widget,
                "Personal",
                gemini_client=FakeGemini(
                    json.dumps({"decision": "unique", "option": "Personal Lines (P/L)"})
                ),
            )
            self.assertEqual(result.selected, "Personal Lines (P/L)")

        asyncio.run(_run())

    def test_any_identified_dropdown_uses_the_same_helper(self) -> None:
        async def _run() -> None:
            widget = identified_widget(name="Writing Company", root="#WritingCompany")
            page = EditPolicyPage(
                department_options=["---Select---"],
                billing_options=["Agency"],
            )
            page.writing_options = ["Agency Mutual", "Progressive Garden State"]
            page.writing_selected = ""
            real = page.locator

            def locator(selector: str):
                if selector.startswith("#WritingCompany"):
                    select = _Node(
                        children=[_Node(text=opt) for opt in page.writing_options],
                        element_id="WritingCompany",
                    )

                    async def _select(label: str | None = None, value: str | None = None) -> None:
                        page.writing_selected = label or value or ""
                        select.selected_label = page.writing_selected

                    def _child(sub: str) -> _Node:
                        if "checked" in sub:
                            return _Node(text=page.writing_selected)
                        return _Node(children=[_Node(text=opt) for opt in page.writing_options])

                    select.select_option = _select
                    select.locator = _child
                    if "option" in selector:
                        return _child(selector)
                    return select
                return real(selector)

            page.locator = locator
            result = await fill_live_dropdown(
                page,
                widget,
                "Progressive Insurance",
                gemini_client=FakeGemini(
                    json.dumps({"decision": "unique", "option": "Progressive Garden State"})
                ),
            )
            self.assertEqual(result.selected, "Progressive Garden State")
            self.assertEqual(page.writing_selected, "Progressive Garden State")
            self.assertEqual(widget.root, "#WritingCompany")

        asyncio.run(_run())

    def test_helper_source_has_no_alias_map(self) -> None:
        from pathlib import Path

        text = Path("robie_job_engine/ezlynx_field_widgets.py").read_text()
        self.assertNotIn("Personal Lines (P/L)", text)
        self.assertNotIn("Commercial Lines (CL)", text)
        self.assertNotIn("alias", text.casefold().split("no alias")[0][-20:] + "")
        self.assertNotIn('ALIASES', text)
        helper = Path("robie_job_engine/gemini_field_helper.py").read_text()
        self.assertNotIn("Personal Lines (P/L)", helper)


if __name__ == "__main__":
    unittest.main()
