"""Unique create/new listbox options (38c0fa79). No live Ascend."""

from __future__ import annotations

import unittest
from pathlib import Path

from durable_temp import durable_temporary_directory

from robie_job_engine.ascend_create_combobox import (
    COMBOBOX_FIELDS,
    COMBOBOX_SCENARIO_ID,
    classify_listbox_options,
    matching_option_count,
    option_locator,
    refuse_non_unique_listbox,
    run_unique_listbox_option_scenario,
    unique_option_hitl,
)
from robie_job_engine.ascend_locator_audit_runner import audit_live_comboboxes
from robie_job_engine.ascend_locator_audit import (
    JOB_TYPE,
    AscendLocatorAuditWorker,
    default_audit_payload,
)
from robie_job_engine.ascend_sender_roles import ascend_new_program_contract_lines
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
            option_locator("Carlo Ferrara", exact=True),
            'get_by_role("option", name="Carlo Ferrara", exact=True)',
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


class _FakeControl:
    def __init__(self, page: "_FakePage", label: str, count: int = 1) -> None:
        self._page = page
        self._label = label
        self._count = count

    def count(self) -> int:
        return self._count

    def click(self) -> None:
        self._page.open_label = self._label


class _FakeOptions:
    def __init__(self, names: list[str]) -> None:
        self._names = names

    def all_inner_texts(self) -> list[str]:
        return list(self._names)


class _FakeKeyboard:
    def press(self, _key: str) -> None:
        return None


class _FakePage:
    def __init__(self, options_by_label: dict[str, list[str]]) -> None:
        self.options_by_label = options_by_label
        self.open_label = ""
        self.keyboard = _FakeKeyboard()

    def get_by_label(self, label: str) -> _FakeControl:
        if label in self.options_by_label:
            return _FakeControl(self, label, count=1)
        return _FakeControl(self, label, count=0)

    def get_by_role(self, role: str, name: str | None = None, exact: bool = False):
        if role != "option":
            return _FakeOptions([])
        names = self.options_by_label.get(self.open_label, [])
        if name is None:
            return _FakeOptions(names)
        matched = [
            item
            for item in names
            if item == name or (not exact and name.casefold() in item.casefold())
        ]
        return _FakeOptions(matched)


class LiveComboboxAuditTests(unittest.TestCase):
    def test_live_audit_fails_duplicate_option_and_logs_field(self):
        page = _FakePage(
            {
                "Producer": ["Carlo Ferrara", "Jake Ferrara"],
                "Account Manager": ["Carlo Ferrara"],
                "Carrier": ["Progressive", "Progressive Specialty"],
                "Coverage type": ["Commercial Auto"],
                "State": ["Florida", "Florida"],
                "Wholesaler": ["Test Wholesaler"],
            }
        )
        observed = audit_live_comboboxes(
            page,
            {
                "requested_by": "Carlo Ferrara",
                "producer": "Carlo Ferrara",
                "account_manager": "Carlo Ferrara",
                "test_carrier": "Progressive",
                "test_coverage": "Commercial Auto",
                "test_state": "Florida",
                "test_wholesaler": "Test Wholesaler",
            },
        )
        self.assertFalse(observed["ok"])
        self.assertIn("State", observed["blocked_fields"])
        self.assertNotIn("Carrier", observed["blocked_fields"])
        state = next(item for item in observed["fields"] if item["field"] == "State")
        self.assertEqual(state["match_count"], 2)
        self.assertIn("State", state["error"])

    def test_live_audit_opens_every_create_form_combobox(self):
        page = _FakePage(
            {
                "Producer": ["Carlo Ferrara"],
                "Account Manager": ["Carlo Ferrara"],
                "Writing company": ["Test Carrier"],
                "Coverage type": ["Commercial Auto"],
                "State": ["Florida"],
                "Wholesaler": ["Test Wholesaler"],
            }
        )
        observed = audit_live_comboboxes(
            page,
            {
                "requested_by": "Carlo Ferrara",
                "test_carrier": "Test Carrier",
                "test_coverage": "Commercial Auto",
                "test_state": "Florida",
                "test_wholesaler": "Test Wholesaler",
            },
        )
        self.assertTrue(observed["ok"], observed)
        self.assertEqual(observed["blocked_fields"], [])
        labels = {item["field"] for item in observed["fields"]}
        for required in (
            "Producer",
            "Account Manager",
            "Carrier",
            "Coverage type",
            "State",
        ):
            self.assertIn(required, labels)


class FixtureAndDocsTests(unittest.TestCase):
    def test_fixture_walk_logs_unique_listboxes(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            artifacts = str(Path(tmp) / "artifacts")
            store = JobStore(db)
            job = store.create_job(
                JOB_TYPE,
                {
                    **default_audit_payload(live=False, requested_by="Carlo Ferrara"),
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

    def test_docs_lock_test_gate_and_live_jobs(self):
        lines = ascend_new_program_contract_lines(
            "Open Ascend and create a program",
            {"requested_by": "Carlo Ferrara", "action_type": "hermes.google_chat_task"},
        )
        blob = "\n".join(lines)
        self.assertIn("38c0fa79", blob)
        self.assertIn("listbox", blob.casefold())
        self.assertIn("blocked field", blob.casefold())
        state = Path("CURRENT_STATE.md").read_text(encoding="utf-8")
        release = Path("RELEASE_PROCESS.md").read_text(encoding="utf-8")
        skill = Path("skills/ascend-locator-artifact-audit/SKILL.md").read_text(
            encoding="utf-8"
        )
        for text in (state, release):
            flat = " ".join(text.replace("**", "").replace("`", "").split())
            self.assertIn("Production is not the first test", flat)
            self.assertIn("807f8920", text)
            self.assertIn("38c0fa79", text)
            self.assertIn("A visual walk on Dusty's computer is not the Test gate", flat)
            self.assertIn("PR 35 CI is not the Test gate", flat)
            self.assertIn("Shipping a zip to hermes-poc-01 is not the Test gate", flat)
            self.assertIn(
                "The Test gate is a clean Job Engine job on hermes-test-01",
                flat,
            )
        self.assertIn("38c0fa79", skill)
        self.assertIn("production_ready: false", skill)
        self.assertNotIn("PAWIVA", default_audit_payload()["test_insured"])


if __name__ == "__main__":
    unittest.main()
