"""Unique create/new listbox options (38c0fa79). No live Ascend."""

from __future__ import annotations

import unittest
from pathlib import Path

from durable_temp import durable_temporary_directory

from robie_job_engine.ascend_create_combobox import (
    COMBOBOX_FIELDS,
    COMBOBOX_SCENARIO_ID,
    INVENTED_COMBOBOX_DEFAULTS,
    LIVE_CARRIER_LISTBOX_TAILS,
    LIVE_COVERAGE_LISTBOX_TAILS,
    LIVE_ROLE_LISTBOX_OPTIONS,
    LIVE_STATE_LISTBOX_OPTIONS,
    TEST_QUOTE_FIELDS,
    TEST_QUOTE_TEXT,
    classify_listbox_options,
    default_test_combobox_payload,
    invented_combobox_defaults,
    listbox_audit_should_abort,
    matching_option_count,
    option_locator,
    parse_quote_fields,
    quote_fields_from_payload,
    refuse_non_unique_listbox,
    run_unique_listbox_option_scenario,
    unique_option_hitl,
)
from robie_job_engine.ascend_locator_audit_runner import (
    _allow_first_identical_coverage_option,
    _field_value,
    _scoped_option_names,
    _select_unique_option,
    _upload_test_quote,
    _verified_role_value,
    _wait_open_listbox_options,
    audit_live_comboboxes,
)
from robie_job_engine.ascend_locator_audit import (
    JOB_TYPE,
    AscendLocatorAuditWorker,
    default_audit_payload,
)
from robie_job_engine.ascend_sender_roles import (
    CARLO_FERRARA,
    CARLO_OPTION,
    JAKE_OPTION,
    ascend_new_program_contract_lines,
)
from robie_job_engine.store import JobStore


class UniqueListboxOptionTests(unittest.TestCase):
    def test_non_exact_prefix_collision_is_blocked_and_logs_field(self):
        leak = refuse_non_unique_listbox(
            field="Carrier",
            intended="Progressive",
            options=("Progressive", "Progressive Specialty"),
            exact=False,
        )
        self.assertIsNotNone(leak)
        self.assertIn("Carrier", leak or "")
        self.assertIn("PLAYWRIGHT_BLOCKED", leak or "")
        self.assertEqual(
            matching_option_count(
                ("Progressive", "Progressive Specialty"),
                "Progressive",
                exact=True,
            ),
            1,
        )
        self.assertIsNone(
            refuse_non_unique_listbox(
                field="Carrier",
                intended="Progressive",
                options=("Progressive", "Progressive Specialty"),
                exact=True,
            )
        )

    def test_duplicate_exact_names_fail_and_missing_is_dry_hitl(self):
        leak = refuse_non_unique_listbox(
            field="State",
            intended="Florida",
            options=("Florida", "Florida"),
            exact=True,
        )
        self.assertIsNotNone(leak)
        self.assertIn("State", leak or "")
        missing = classify_listbox_options(
            field="Coverage type",
            intended="",
            options=("Commercial Auto",),
        )
        self.assertTrue(missing["hitl_required"])
        self.assertEqual(missing["blocked_field"], "Coverage type")
        self.assertIn("PLAYWRIGHT_BLOCKED", missing["hitl_text"])
        self.assertNotIn("Listen up", missing["hitl_text"])
        hitl = unique_option_hitl(field="Producer", intended="", match_count=0)
        self.assertIn("Producer", hitl)
        self.assertEqual(
            option_locator(CARLO_OPTION, exact=True),
            f'get_by_role("option", name="{CARLO_OPTION}", exact=True)',
        )
        self.assertNotEqual(
            option_locator(CARLO_FERRARA, exact=True),
            option_locator(CARLO_OPTION, exact=True),
        )

    def test_named_scenario_covers_required_fields(self):
        labels = {item["label"] for item in COMBOBOX_FIELDS}
        for required in (
            "Producer",
            "Account Manager",
            "Carrier",
            "Coverage type",
            "State",
        ):
            self.assertIn(required, labels)
        report = run_unique_listbox_option_scenario()
        self.assertEqual(report["id"], COMBOBOX_SCENARIO_ID)
        self.assertTrue(report["ok"], report.get("evidence"))
        self.assertEqual(report["observed"]["job_id"], "38c0fa79")


    def test_name_only_carlo_fails_two_email_fixture(self):
        name_only = classify_listbox_options(
            field="Producer",
            intended=CARLO_FERRARA,
            options=LIVE_ROLE_LISTBOX_OPTIONS,
            exact=True,
        )
        self.assertEqual(name_only["status"], "FAIL")
        self.assertGreaterEqual(name_only["match_count"], 2)
        self.assertEqual(name_only["blocked_field"], "Producer")
        self.assertIn("PLAYWRIGHT_BLOCKED", name_only["error"] or "")
        self.assertIsNotNone(
            refuse_non_unique_listbox(
                field="Producer",
                intended=CARLO_FERRARA,
                options=LIVE_ROLE_LISTBOX_OPTIONS,
                exact=True,
            )
        )


    def test_email_qualified_carlo_passes_for_streetsmart_requested_by(self):
        payload = default_test_combobox_payload(
            requested_by="carlo@streetsmart.insurance"
        )
        self.assertEqual(payload["producer"], CARLO_OPTION)
        self.assertEqual(payload["account_manager"], CARLO_OPTION)
        self.assertEqual(invented_combobox_defaults(payload), [])
        for banned in INVENTED_COMBOBOX_DEFAULTS:
            self.assertNotIn(banned, payload.values())
        report = classify_listbox_options(
            field="Producer",
            intended=payload["producer"],
            options=LIVE_ROLE_LISTBOX_OPTIONS,
            exact=True,
        )
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["match_count"], 1)
        self.assertEqual(report["intended"], CARLO_OPTION)
        self.assertEqual(
            report["locator"],
            f'get_by_role("option", name="{CARLO_OPTION}", exact=True)',
        )
        self.assertIsNone(
            refuse_non_unique_listbox(
                field="Producer",
                intended=CARLO_OPTION,
                options=LIVE_ROLE_LISTBOX_OPTIONS,
                exact=True,
            )
        )
        missing = classify_listbox_options(
            field="Producer",
            intended=CARLO_OPTION,
            options=(
                "Carlo Ferrara carlo@ssinj.com",
                "Robie AI robie@streetsmart.insurance",
                JAKE_OPTION,
            ),
            exact=True,
        )
        self.assertIn(missing["status"], {"HITL", "FAIL"})
        self.assertNotEqual(missing.get("intended"), "Carlo Ferrara carlo@ssinj.com")


class _FakeControl:
    def __init__(self, page: "_FakePage", label: str, count: int = 1) -> None:
        self._page = page
        self._label = label
        self._count = count

    def count(self) -> int:
        return self._count

    def click(self) -> None:
        self._page.open_label = self._label

    def fill(self, value: str) -> None:
        self._page.open_label = self._label
        self._page.search_by_label[self._label] = value

    def press(self, key: str) -> None:
        self._page.pressed_by_label[self._label] = key
        if key == "Enter":
            self._page.open_label = ""

    def evaluate(self, _expression: str) -> str:
        return "SELECT" if self._label == "State" else "INPUT"

    def locator(self, selector: str) -> _FakeOptions:
        if selector == "option" and self._label == "State":
            return _FakeOptions(self._page.options_by_label.get(self._label, []))
        return _FakeOptions([])


class _FakeOptions:
    def __init__(self, names: list[str]) -> None:
        self._names = names

    def count(self) -> int:
        return len(self._names)

    def all_inner_texts(self) -> list[str]:
        return list(self._names)

    def filter(self, visible: bool = True):
        del visible
        return self

    def get_by_role(self, role: str, name: str | None = None, exact: bool = False):
        del exact
        if role != "option":
            return _FakeOptions([])
        if name is None:
            return _FakeOptions(self._names)
        matched = [item for item in self._names if item == name]
        return _FakeOptions(matched)


class _FakeListbox:
    def __init__(self, names: list[str], *, present: bool) -> None:
        self._names = names
        self._present = present

    def count(self) -> int:
        return 1 if self._present else 0

    def filter(self, visible: bool = True):
        del visible
        return self

    def get_by_role(self, role: str, name: str | None = None, exact: bool = False):
        del name, exact
        if role != "option":
            return _FakeOptions([])
        return _FakeOptions(self._names)


class _FakeFileInput:
    def __init__(self) -> None:
        self.files: str | None = None

    def count(self) -> int:
        return 1

    def set_input_files(self, path: str) -> None:
        self.files = path


class _FakeKeyboard:
    def __init__(self, page: "_FakePage") -> None:
        self._page = page

    def press(self, key: str) -> None:
        if key == "Escape":
            self._page.open_label = ""


class _FakePage:
    def __init__(
        self,
        options_by_label: dict[str, list[str]],
        *,
        leaked: list[str] | None = None,
    ) -> None:
        self.options_by_label = options_by_label
        self.leaked = list(leaked or [])
        self.open_label = ""
        self.search_by_label: dict[str, str] = {}
        self.pressed_by_label: dict[str, str] = {}
        self.keyboard = _FakeKeyboard(self)
        self.file_input = _FakeFileInput()

    def locator(self, selector: str) -> _FakeFileInput | _FakeOptions:
        if selector in {
            "input[aria-label='file_upload'][type='file']",
            "input#file_upload[type='file']",
            "input[type='file']",
        }:
            return self.file_input
        return _FakeOptions([])

    def get_by_label(self, label: str) -> _FakeControl:
        if label in self.options_by_label:
            return _FakeControl(self, label, count=1)
        return _FakeControl(self, label, count=0)

    def get_by_role(self, role: str, name: str | None = None, exact: bool = False):
        if role == "listbox":
            present = bool(self.open_label)
            names = self.options_by_label.get(self.open_label, []) if present else []
            search = self.search_by_label.get(self.open_label, "").casefold()
            if search:
                names = [item for item in names if search in item.casefold()]
            return _FakeListbox(names, present=present)
        if role != "option":
            return _FakeOptions([])
        scoped = self.options_by_label.get(self.open_label, [])
        # Page-wide query includes leftover State names — the leak under test.
        names = list(self.leaked) + scoped if self.open_label else []
        if name is None:
            return _FakeOptions(names)
        matched = [
            item
            for item in names
            if item == name or (not exact and name.casefold() in item.casefold())
        ]
        return _FakeOptions(matched)


class LiveComboboxAuditTests(unittest.TestCase):
    def test_only_identical_exact_coverage_duplicates_use_first_active(self):
        duplicate = classify_listbox_options(
            field="Coverage type",
            intended="Commercial Auto",
            options=["Commercial Auto", "Commercial Auto"],
            exact=True,
        )
        allowed = _allow_first_identical_coverage_option(
            duplicate, field="Coverage type"
        )
        self.assertEqual(allowed["status"], "PASS")
        self.assertEqual(
            allowed["selection_strategy"], "first_active_identical_exact"
        )
        still_blocked = _allow_first_identical_coverage_option(
            duplicate, field="Carrier"
        )
        self.assertEqual(still_blocked["status"], "FAIL")

    def test_coverage_selection_presses_enter_on_first_identical_exact_result(self):
        page = _FakePage(
            {"Coverage type": ["Commercial Auto", "Commercial Auto"]}
        )
        target = page.get_by_label("Coverage type")
        report = _select_unique_option(
            page,
            target,
            "Commercial Auto",
            field="Coverage type",
        )
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(page.pressed_by_label["Coverage type"], "Enter")

    def test_unique_searched_carrier_selects_active_result_with_enter(self):
        carrier = "Drive New Jersey Insurance Company"
        page = _FakePage(
            {
                "Carrier": [
                    f"{carrier}\nOffice: P.O. Box 89490, Cleveland OH"
                ]
            }
        )
        target = page.get_by_label("Carrier")
        report = _select_unique_option(page, target, carrier, field="Carrier")
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(
            report["selection_strategy"], "active_unique_search_result"
        )
        self.assertEqual(page.pressed_by_label["Carrier"], "Enter")

    def test_role_selection_uses_visible_email_option_without_searching(self):
        page = _FakePage({"Producer": list(LIVE_ROLE_LISTBOX_OPTIONS)})
        target = page.get_by_label("Producer")
        report = _select_unique_option(
            page,
            target,
            CARLO_OPTION,
            field="Producer",
        )
        self.assertEqual(report["status"], "PASS")
        self.assertNotIn("Producer", page.search_by_label)

    def test_live_audit_allows_first_of_identical_exact_coverage_only(self):
        page = _FakePage(
            {
                "Producer": list(LIVE_ROLE_LISTBOX_OPTIONS),
                "Account Manager": list(LIVE_ROLE_LISTBOX_OPTIONS),
                "Carrier": list(LIVE_CARRIER_LISTBOX_TAILS),
                "Coverage type": ["Commercial Package", "Commercial Package"],
                "State": ["Georgia"],
            }
        )
        observed = audit_live_comboboxes(
            page,
            {
                "requested_by": "carlo@streetsmart.insurance",
                "quote_text": TEST_QUOTE_TEXT,
            },
        )
        self.assertTrue(observed["ok"], observed)
        coverage = next(
            item for item in observed["fields"] if item["field"] == "Coverage type"
        )
        self.assertEqual(
            coverage["selection_strategy"], "first_active_identical_exact"
        )

    def test_wait_for_searched_options_does_not_return_initial_list(self):
        class DelayedSearchPage:
            def __init__(self) -> None:
                self.reads = 0

            def get_by_role(self, role: str):
                self.reads += 1
                names = (
                    ["CNA", "Philadelphia Indemnity Insurance Company"]
                    if self.reads < 3
                    else [
                        "Drive New Jersey Insurance Company\n"
                        "Office: P.O. Box 89490, Cleveland OH"
                    ]
                )
                return _FakeListbox(names, present=role == "listbox")

        page = DelayedSearchPage()
        names = _wait_open_listbox_options(
            page,
            intended="Drive New Jersey Insurance Company",
            timeout_ms=1_000,
        )
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].startswith("Drive New Jersey Insurance Company"))

    def test_field_value_reads_react_select_single_value_when_input_is_empty(self):
        class ReactSelectInput:
            def input_value(self) -> str:
                return ""

            def evaluate(self, expression: str) -> str:
                self.assert_expression = expression
                return "Carlo Ferrara"

        target = ReactSelectInput()
        self.assertEqual(_field_value(target), "Carlo Ferrara")
        self.assertIn("select__single-value", target.assert_expression)

    def test_name_only_role_is_trusted_only_after_unique_email_option_selection(self):
        self.assertEqual(
            _verified_role_value(
                "Carlo Ferrara",
                resolved_name="Carlo Ferrara",
                exact_option=CARLO_OPTION,
                uniquely_selected=True,
            ),
            CARLO_OPTION,
        )
        self.assertEqual(
            _verified_role_value(
                "Carlo Ferrara",
                resolved_name="Carlo Ferrara",
                exact_option=CARLO_OPTION,
                uniquely_selected=False,
            ),
            "Carlo Ferrara",
        )

    def test_live_audit_fails_duplicate_option_and_logs_field(self):
        page = _FakePage(
            {
                "Producer": list(LIVE_ROLE_LISTBOX_OPTIONS),
                "Account Manager": list(LIVE_ROLE_LISTBOX_OPTIONS),
                "Carrier": list(LIVE_CARRIER_LISTBOX_TAILS),
                "Coverage type": list(LIVE_COVERAGE_LISTBOX_TAILS),
                "State": ["Georgia", "Georgia"],
            }
        )
        observed = audit_live_comboboxes(
            page,
            {
                "requested_by": "carlo@streetsmart.insurance",
                "quote_text": TEST_QUOTE_TEXT,
            },
        )
        self.assertFalse(observed["ok"])
        self.assertIn("State", observed["blocked_fields"])
        self.assertNotIn("Carrier", observed["blocked_fields"])
        state = next(item for item in observed["fields"] if item["field"] == "State")
        self.assertEqual(state["match_count"], 2)
        self.assertIn("State", state["error"])
        self.assertTrue(listbox_audit_should_abort(observed))

    def test_live_audit_opens_every_create_form_combobox(self):
        page = _FakePage(
            {
                "Producer": list(LIVE_ROLE_LISTBOX_OPTIONS),
                "Account Manager": list(LIVE_ROLE_LISTBOX_OPTIONS),
                "Writing company": list(LIVE_CARRIER_LISTBOX_TAILS),
                "Coverage type": list(LIVE_COVERAGE_LISTBOX_TAILS),
                "State": ["Georgia", "New York"],
            }
        )
        observed = audit_live_comboboxes(
            page,
            {
                "requested_by": "carlo@streetsmart.insurance",
                "quote_text": TEST_QUOTE_TEXT,
            },
        )
        self.assertTrue(observed["ok"], observed)
        self.assertEqual(observed["blocked_fields"], [])
        self.assertFalse(observed["abort_walk"])
        producer = next(item for item in observed["fields"] if item["field"] == "Producer")
        self.assertEqual(producer["intended"], CARLO_OPTION)
        self.assertEqual(producer["match_count"], 1)
        self.assertEqual(
            producer["locator"],
            f'get_by_role("option", name="{CARLO_OPTION}", exact=True)',
        )
        coverage = next(item for item in observed["fields"] if item["field"] == "Coverage type")
        self.assertEqual(coverage["intended"], "Commercial Package")
        self.assertEqual(coverage["status"], "PASS")
        labels = {item["field"] for item in observed["fields"]}
        for required in (
            "Producer",
            "Account Manager",
            "Carrier",
            "Coverage type",
            "State",
        ):
            self.assertIn(required, labels)

    def test_empty_intended_is_hitl_and_does_not_abort_walk(self):
        page = _FakePage(
            {
                "Producer": list(LIVE_ROLE_LISTBOX_OPTIONS),
                "Account Manager": list(LIVE_ROLE_LISTBOX_OPTIONS),
                "Carrier": list(LIVE_CARRIER_LISTBOX_TAILS),
                "Coverage type": list(LIVE_COVERAGE_LISTBOX_TAILS),
                "State": ["Georgia"],
            }
        )
        observed = audit_live_comboboxes(
            page,
            {"requested_by": "carlo@streetsmart.insurance"},
        )
        self.assertFalse(observed["ok"])
        self.assertFalse(observed["abort_walk"])
        self.assertFalse(listbox_audit_should_abort(observed))
        for name in ("Carrier", "Coverage type", "State"):
            field = next(item for item in observed["fields"] if item["field"] == name)
            self.assertEqual(field["status"], "HITL", name)
            self.assertEqual(field["intended"], "")
        wholesaler = next(item for item in observed["fields"] if item["field"] == "Wholesaler")
        self.assertEqual(wholesaler["status"], "SKIP")

    def test_leftover_state_options_are_not_counted_as_carrier(self):
        page = _FakePage(
            {
                "Producer": list(LIVE_ROLE_LISTBOX_OPTIONS),
                "Account Manager": list(LIVE_ROLE_LISTBOX_OPTIONS),
                "Carrier": list(LIVE_CARRIER_LISTBOX_TAILS),
                "Coverage type": list(LIVE_COVERAGE_LISTBOX_TAILS),
                "State": list(LIVE_STATE_LISTBOX_OPTIONS),
            },
            leaked=list(LIVE_STATE_LISTBOX_OPTIONS),
        )
        page.open_label = "Carrier"
        page_wide = page.get_by_role("option").all_inner_texts()
        self.assertIn("Florida", page_wide)
        self.assertIn("New Jersey", page_wide)
        scoped = _scoped_option_names(page)
        self.assertNotIn("Florida", scoped)
        self.assertNotIn("New Jersey", scoped)
        self.assertEqual(scoped, list(LIVE_CARRIER_LISTBOX_TAILS))
        page.open_label = ""
        observed = audit_live_comboboxes(
            page,
            {
                "requested_by": "carlo@streetsmart.insurance",
                "quote_text": TEST_QUOTE_TEXT,
            },
        )
        self.assertTrue(observed["ok"], observed)
        carrier = next(item for item in observed["fields"] if item["field"] == "Carrier")
        self.assertEqual(carrier["status"], "PASS")
        self.assertEqual(carrier["intended"], TEST_QUOTE_FIELDS["carrier"])
        self.assertNotIn("Florida", carrier["options"])
        self.assertNotIn("New Jersey", carrier["options"])
        coverage = next(item for item in observed["fields"] if item["field"] == "Coverage type")
        self.assertNotIn("Florida", coverage["options"])
        self.assertEqual(coverage["intended"], "Commercial Package")

    def test_quote_parser_refuses_pawiva_and_does_not_guess(self):
        self.assertEqual(parse_quote_fields(TEST_QUOTE_TEXT), TEST_QUOTE_FIELDS)
        self.assertEqual(
            quote_fields_from_payload({"quote_text": TEST_QUOTE_TEXT}),
            TEST_QUOTE_FIELDS,
        )
        self.assertEqual(
            quote_fields_from_payload({"requested_by": "carlo@streetsmart.insurance"}),
            {"carrier": "", "coverage_type": "", "state": ""},
        )
        with self.assertRaises(ValueError):
            parse_quote_fields("Carrier: PAWIVA\nCoverage type: Commercial Package")

    def test_quote_parser_reads_progressive_commercial_auto_pdf_labels(self):
        text = """
        Commercial Auto Insurance Quote
        I am pleased to provide you with a quote from Drive New
        Jersey Insurance Company, a company that offers competitive rates.
        Form QUOTE NJ (01/25)
        """
        self.assertEqual(
            parse_quote_fields(text),
            {
                "carrier": "Drive New Jersey Insurance Company",
                "coverage_type": "Commercial Auto",
                "state": "New Jersey",
            },
        )

    def test_quote_sourced_comboboxes_are_searched_before_exact_match(self):
        carrier = "Drive New Jersey Insurance Company"
        page = _FakePage(
            {
                "Producer": list(LIVE_ROLE_LISTBOX_OPTIONS),
                "Account Manager": list(LIVE_ROLE_LISTBOX_OPTIONS),
                "Carrier": [
                    "Philadelphia Indemnity Insurance Company",
                    f"{carrier}\nOffice: 1 Test Way, Trenton NJ",
                ],
                "Coverage type": ["Commercial Package", "Commercial Auto"],
                "State": ["Georgia", "New Jersey"],
            }
        )
        observed = audit_live_comboboxes(
            page,
            {
                "requested_by": "carlo@streetsmart.insurance",
                "quote_text": (
                    "Commercial Auto Insurance Quote\n"
                    "quote from Drive New Jersey Insurance Company\n"
                    "Form QUOTE NJ (01/25)"
                ),
            },
        )
        self.assertTrue(observed["ok"], observed)
        for name in ("Carrier", "Coverage type"):
            field = next(item for item in observed["fields"] if item["field"] == name)
            self.assertTrue(field["searched"], field)
            self.assertEqual(field["match_count"], 1)
        state = next(item for item in observed["fields"] if item["field"] == "State")
        self.assertFalse(state["searched"], state)
        self.assertEqual(state["control_type"], "select")
        self.assertEqual(state["match_count"], 1)

    def test_import_uploads_test_quote_file(self):
        page = _FakePage({})
        uploaded = _upload_test_quote(page, "/tmp/robie-test-quote.txt")
        self.assertTrue(uploaded["uploaded"])
        self.assertEqual(page.file_input.files, "/tmp/robie-test-quote.txt")
        with self.assertRaises(RuntimeError):
            _upload_test_quote(page, "/tmp/PAWIVA-quote.pdf")


class FixtureAndDocsTests(unittest.TestCase):
    def test_fixture_walk_logs_unique_listboxes(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            artifacts = str(Path(tmp) / "artifacts")
            store = JobStore(db)
            job = store.create_job(
                JOB_TYPE,
                {
                    **default_audit_payload(
                        live=False,
                        requested_by="carlo@streetsmart.insurance",
                        quote_text=TEST_QUOTE_TEXT,
                    ),
                    "db_path": db,
                    "artifact_root": artifacts,
                    "line_of_business": "commercial auto",
                },
            )
            job["db_path"] = db
            result = AscendLocatorAuditWorker(artifact_root=artifacts).perform(
                job, idempotency_key=job["idempotency_key"]
            )
            self.assertTrue(result.succeeded, result.error)
            walked = {
                item["id"]: item
                for item in (result.detail or {}).get("punch_list", {}).get("steps", [])
            }
            self.assertIn("unique_listbox_options", walked)
            self.assertEqual(walked["unique_listbox_options"]["status"], "PASS")
            fields = walked["unique_listbox_options"]["observed"]["fields"]
            self.assertGreaterEqual(len(fields), 5)
            self.assertEqual(walked["unique_listbox_options"]["observed"]["blocked_fields"], [])
            producer = next(
                item for item in fields if item["field"] == "Producer"
            )
            self.assertEqual(producer["intended"], CARLO_OPTION)
            self.assertEqual(producer["match_count"], 1)
            self.assertEqual(producer["status"], "PASS")
            coverage = next(item for item in fields if item["field"] == "Coverage type")
            self.assertEqual(coverage["intended"], "Commercial Package")
            self.assertEqual(coverage["status"], "PASS")
            self.assertEqual(
                invented_combobox_defaults(
                    default_audit_payload(
                        live=True, requested_by="carlo@streetsmart.insurance"
                    )
                ),
                [],
            )

    def test_docs_lock_test_gate_and_live_jobs(self):
        lines = ascend_new_program_contract_lines(
            "Open Ascend and create a program",
            {"requested_by": "Carlo Ferrara", "action_type": "hermes.google_chat_task"},
        )
        blob = "\n".join(lines)
        self.assertIn("38c0fa79", blob)
        self.assertIn("listbox", blob.casefold())
        self.assertIn("blocked field", blob.casefold())
        self.assertIn(CARLO_OPTION, blob)
        self.assertIn("Name+email", blob)
        state = Path("CURRENT_STATE.md").read_text(encoding="utf-8")
        release = Path("RELEASE_PROCESS.md").read_text(encoding="utf-8")
        skill = Path("skills/ascend-locator-artifact-audit/SKILL.md").read_text(
            encoding="utf-8"
        )
        for text in (state, release):
            flat = " ".join(text.replace("**", "").replace("`", "").split())
            self.assertIn(
                "must get a clean pass on hermes-test-01",
                flat,
            )
            self.assertIn(
                "BEFORE any Production Chat job on a real account",
                flat,
            )
            self.assertIn("Production is not the first test", flat)
            self.assertIn("807f8920", text)
            self.assertIn("38c0fa79", text)
            self.assertIn("The Test gate was skipped. That is a process miss.", flat)
            self.assertIn("A visual walk on Dusty's computer is not the Test gate", flat)
            self.assertIn("PR 35 CI is not the Test gate", flat)
            self.assertIn("Shipping a zip to hermes-poc-01 is not the Test gate", flat)
            self.assertIn(
                "The Test gate is a clean Job Engine job on hermes-test-01",
                flat,
            )
        self.assertIn("38c0fa79", skill)
        self.assertIn(CARLO_OPTION, skill)
        self.assertIn("carlo@ssinj.com", skill)
        self.assertIn("production_ready: false", skill)
        self.assertNotIn("PAWIVA", default_audit_payload()["test_insured"])


if __name__ == "__main__":
    unittest.main()
