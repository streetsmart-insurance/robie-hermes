"""Production filing for the allowlisted carriers. BOP stays blocked."""

from __future__ import annotations

import json
import os
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from robie_job_engine import document_retrieval_health as health
from robie_job_engine import document_retrieval_service as service
from robie_job_engine import ezlynx_discussions as disc
from robie_job_engine.discussion_note_ledger import (
    DiscussionNoteLedgerError,
    record_known_posted_notes,
)
from robie_job_engine.document_retrieval_filing import (
    GEICO_NOC_RULE,
    KILL_SWITCH_ENV,
    NATGEN_NOC_RULE,
    PROD_CARRIER_RULES,
    PROGRESSIVE_BOP_RULE,
    PROGRESSIVE_MEMO_RULE,
    FilingHeld,
    active_carrier_rules,
    file_carrier_batch,
    file_progressive_memos,
    filing_note,
    live_filing_decision,
    resolve_pull_output,
)
from robie_job_engine.geico_pending_cancellation_noc import refuse_production_host
from robie_job_engine.intake_core import IntakeHold
from robie_job_engine.progressive_fao_memo import DEFAULT_QA_ROOT
from tests.test_document_retrieval_filing import (
    HOST,
    SHEET_DAY,
    SHEET_ROWS,
    FakeDeps,
    FakeSheet,
    memo_item,
)

PDF_DAY = date(2026, 9, 25)
PROD_HOST = "hermes-poc-01"
PROD_ON = {"ROBIE_ENV": "PRODUCTION", KILL_SWITCH_ENV: "1"}
PROD_OFF = {"ROBIE_ENV": "PRODUCTION"}
ENABLED = {"ROBIE_ENV": "TEST", KILL_SWITCH_ENV: "1"}
ROOT = Path(__file__).resolve().parents[1]


class EchoTextClient:
    """Discussion read includes the exact note text that was posted."""

    def __init__(self):
        self.posted = []
        self.title = PROGRESSIVE_MEMO_RULE.workflow_title

    def get_discussions(self, applicant_id):
        return [{
            "discussionId": "disc-1",
            "title": self.title,
            "applicantId": applicant_id,
        }]

    def get_discussion(self, discussion_id):
        notes = [
            {"noteId": str(100 + index), "body": text}
            for index, text in enumerate(self.posted, start=1)
        ]
        latest = "100" if not self.posted else str(100 + len(self.posted))
        return {
            "discussionId": discussion_id,
            "title": self.title,
            "noteCount": 2 + len(self.posted),
            "mostRecentNoteId": latest,
            "notes": notes,
        }

    def append_note(self, discussion_id, text, note_type="Note"):
        self.posted.append(text)
        return {"noteId": str(100 + len(self.posted))}


class ProdGateTests(unittest.TestCase):
    def test_switch_off_files_nothing(self):
        def boom():
            raise AssertionError("client factory must not run")

        for rule, caller in (
            (None, lambda: file_progressive_memos(
                [memo_item()], environ=PROD_OFF, hostname=PROD_HOST,
                client_factory=boom, sheet_day=SHEET_DAY,
            )),
            (NATGEN_NOC_RULE, lambda: file_carrier_batch(
                [memo_item()], rule=NATGEN_NOC_RULE, environ=PROD_OFF,
                hostname=PROD_HOST, client_factory=boom, sheet_day=SHEET_DAY,
            )),
            (GEICO_NOC_RULE, lambda: file_carrier_batch(
                [memo_item()], rule=GEICO_NOC_RULE, environ=PROD_OFF,
                hostname=PROD_HOST, client_factory=boom, sheet_day=SHEET_DAY,
            )),
            (PROGRESSIVE_BOP_RULE, lambda: file_carrier_batch(
                [memo_item()], rule=PROGRESSIVE_BOP_RULE, environ=PROD_ON,
                hostname=PROD_HOST, client_factory=boom, sheet_day=SHEET_DAY,
            )),
        ):
            result = caller()
            self.assertEqual(result["status"], "disabled", rule)
            self.assertFalse(result["attempted_writes"], rule)
            self.assertEqual(result["results"], [], rule)

    def test_switch_on_files_fao_natgen_and_geico_and_bop_holds(self):
        ledger = Path(self.id().replace(".", "_") + "-ledger.json")
        # Isolated under the test tree, not the Production path.
        ledger = Path("/tmp") / "robie-prod-filing-ledger.json"
        ledger.write_text('{"version": 1, "notes": []}\n', encoding="utf-8")
        self.addCleanup(lambda: ledger.unlink(missing_ok=True))
        natgen_rows = [list(row) for row in SHEET_ROWS]
        natgen_rows.append(["NatGen", "", "", "", "", "", ""])

        def file_one(rule, rows):
            deps = FakeDeps(sheet=FakeSheet(rows=rows))
            with patch(
                "robie_job_engine.document_retrieval_filing.prod_ledger_path",
                return_value=ledger,
            ):
                result = file_carrier_batch(
                    [memo_item(policy_number="993334183", filename="993334183 notice.pdf")],
                    rule=rule,
                    environ=PROD_ON,
                    hostname=PROD_HOST,
                    client_factory=deps.as_deps,
                    sheet_day=SHEET_DAY,
                )
            return deps, result

        fao_deps, fao = file_one(PROGRESSIVE_MEMO_RULE, SHEET_ROWS)
        self.assertEqual(fao["status"], "filed", fao)
        self.assertEqual(len(fao_deps.notes), 1)
        self.assertIn("993334183", fao_deps.notes[0]["text"])
        self.assertTrue(fao_deps.notes[0]["text"].endswith("ROBIE was here"))

        natgen_deps, natgen = file_one(NATGEN_NOC_RULE, natgen_rows)
        self.assertEqual(natgen["status"], "filed_no_workflow", natgen)
        self.assertEqual(natgen_deps.notes, [])
        self.assertEqual(len(natgen_deps.tasks), 1)
        self.assertEqual(len(natgen_deps.uploads), 1)

        geico_deps, geico = file_one(GEICO_NOC_RULE, SHEET_ROWS)
        self.assertEqual(geico["status"], "filed_no_workflow", geico)
        self.assertEqual(geico_deps.notes, [])
        self.assertEqual(len(geico_deps.tasks), 1)

        def boom():
            raise AssertionError("BOP must not build a client")

        bop = file_carrier_batch(
            [memo_item()],
            rule=PROGRESSIVE_BOP_RULE,
            environ=PROD_ON,
            hostname=PROD_HOST,
            client_factory=boom,
            sheet_day=SHEET_DAY,
        )
        self.assertEqual(bop["status"], "disabled")
        self.assertIn("BOP", bop["reason"])
        self.assertNotIn("bop", PROD_CARRIER_RULES)
        self.assertEqual(
            set(PROD_CARRIER_RULES),
            {"fao", "natgen", "geico", "travelers", "farmersofsalem", "guard", "progressive", "uticafirst"},
        )
        self.assertIn("bop", active_carrier_rules(ENABLED, HOST))

    def test_test_filing_is_unchanged(self):
        self.assertTrue(live_filing_decision(ENABLED, HOST).allowed)
        self.assertTrue(live_filing_decision(ENABLED, HOST, "bop").allowed)
        self.assertTrue(live_filing_decision(ENABLED, HOST, "fao").allowed)
        self.assertFalse(live_filing_decision({"ROBIE_ENV": "TEST"}, HOST).allowed)
        self.assertFalse(
            live_filing_decision({"ROBIE_ENV": "TEST", KILL_SWITCH_ENV: "1"}, PROD_HOST).allowed
        )
        deps = FakeDeps()
        result = file_progressive_memos(
            [memo_item()],
            environ=ENABLED,
            hostname=HOST,
            client_factory=deps.as_deps,
            sheet_day=SHEET_DAY,
        )
        self.assertEqual(result["status"], "filed")
        self.assertEqual(len(deps.notes), 1)

    def test_missing_or_unwritable_prod_ledger_holds(self):
        missing = Path("/tmp/robie-missing-discussion-note-ledger.json")
        missing.unlink(missing_ok=True)
        deps = FakeDeps()
        with patch(
            "robie_job_engine.document_retrieval_filing.prod_ledger_path",
            return_value=missing,
        ):
            result = file_progressive_memos(
                [memo_item()],
                environ=PROD_ON,
                hostname=PROD_HOST,
                client_factory=deps.as_deps,
                sheet_day=SHEET_DAY,
            )
        self.assertEqual(result["status"], "held")
        self.assertIn("missing", result["reason"].lower())
        self.assertEqual(deps.uploads, [])
        self.assertFalse(result["attempted_writes"])

        present = Path("/tmp/robie-locked-discussion-note-ledger.json")
        present.write_text("{}\n", encoding="utf-8")
        self.addCleanup(lambda: present.unlink(missing_ok=True))
        with patch(
            "robie_job_engine.document_retrieval_filing.prod_ledger_path",
            return_value=present,
        ), patch(
            "robie_job_engine.document_retrieval_filing.os.access",
            return_value=False,
        ):
            locked = file_progressive_memos(
                [memo_item()],
                environ=PROD_ON,
                hostname=PROD_HOST,
                client_factory=deps.as_deps,
                sheet_day=SHEET_DAY,
            )
        self.assertEqual(locked["status"], "held")
        self.assertIn("not writable", locked["reason"])
        self.assertEqual(deps.uploads, [])

    def test_prod_output_root_leaves_the_test_tree(self):
        rewritten = resolve_pull_output(
            str(DEFAULT_QA_ROOT), DEFAULT_QA_ROOT, environ=PROD_ON, hostname=PROD_HOST,
        )
        self.assertTrue(str(rewritten).startswith("/opt/streetsmart-hermes/"))
        self.assertNotIn("streetsmart-hermes-test", str(rewritten))
        same = resolve_pull_output(
            str(DEFAULT_QA_ROOT), DEFAULT_QA_ROOT, environ=ENABLED, hostname=HOST,
        )
        self.assertEqual(same, DEFAULT_QA_ROOT)
        with self.assertRaises(FilingHeld):
            resolve_pull_output(
                "/opt/streetsmart-hermes-test/custom",
                DEFAULT_QA_ROOT,
                environ=PROD_ON,
                hostname=PROD_HOST,
            )

    def test_geico_host_refusal_opens_only_with_the_filing_gate(self):
        with patch.dict(os.environ, PROD_ON, clear=False), \
             patch("robie_job_engine.geico_pending_cancellation_noc.socket.gethostname", return_value=PROD_HOST), \
             patch("robie_job_engine.geico_pending_cancellation_noc.socket.getfqdn", return_value=PROD_HOST + ".internal"):
            refuse_production_host()
        with patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION", KILL_SWITCH_ENV: "0"}, clear=False), \
             patch("robie_job_engine.geico_pending_cancellation_noc.socket.gethostname", return_value=PROD_HOST), \
             patch("robie_job_engine.geico_pending_cancellation_noc.socket.getfqdn", return_value=PROD_HOST + ".internal"):
            with self.assertRaises(IntakeHold) as caught:
                refuse_production_host()
        self.assertIn(PROD_HOST, str(caught.exception))

    def test_record_known_is_refused_on_production(self):
        path = Path("/tmp/robie-prod-record-known.json")
        path.unlink(missing_ok=True)
        with patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION"}, clear=False):
            with self.assertRaises(DiscussionNoteLedgerError) as caught:
                record_known_posted_notes(path)
        self.assertIn("Production", str(caught.exception))
        self.assertFalse(path.exists())


class TwoMemoNoteTests(unittest.TestCase):
    def test_two_fao_memos_for_one_client_confirm_and_a_repost_is_blocked(self):
        first = memo_item(
            policy_number="993334183",
            insured_name="Yolanda Concepcion",
            filename="993334183 Progressive Memo Signature.pdf",
            processed_on="2026-09-25",
        )
        second = memo_item(
            policy_number="993334183",
            insured_name="Yolanda Concepcion",
            filename="993334183 Progressive Memo Discount.pdf",
            processed_on="2026-09-25",
        )
        first_text = filing_note(
            PROGRESSIVE_MEMO_RULE, PDF_DAY,
            policy_number=first["policy_number"], filename=first["filename"],
        )
        second_text = filing_note(
            PROGRESSIVE_MEMO_RULE, PDF_DAY,
            policy_number=second["policy_number"], filename=second["filename"],
        )
        self.assertNotEqual(first_text, second_text)
        self.assertIn("Signature", first_text)
        self.assertIn("Discount", second_text)
        self.assertTrue(disc._posted_text_matches(
            {"notes": [{"noteId": "101", "body": first_text}]}, first_text,
        ))
        self.assertFalse(disc._posted_text_matches(
            {"notes": [{"noteId": "101", "body": first_text}]}, second_text,
        ))

        client = EchoTextClient()
        ledger = Path("/tmp/robie-two-memo-ledger.json")
        ledger.unlink(missing_ok=True)
        self.addCleanup(lambda: ledger.unlink(missing_ok=True))
        ids = iter(["501", "502"])

        class Noting(FakeDeps):
            def upload(self, applicant_id, document_name, file_bytes, filename=None):
                self.uploads.append({"applicant_id": applicant_id, "filename": filename})
                return {"document_id": next(ids), "read_back": True}

            def add_note(self, applicant_id, note_text, discussion_title=None, document_id=None):
                self.notes.append({
                    "applicant_id": applicant_id,
                    "text": note_text,
                    "title": discussion_title,
                    "document_id": document_id,
                })
                return disc.file_note_to_existing_discussion(
                    client,
                    applicant_id,
                    note_text,
                    title_hint=discussion_title,
                    document_id=document_id,
                    ledger_path=ledger,
                )

        deps = Noting()
        result = file_progressive_memos(
            [first, second],
            environ=ENABLED,
            hostname=HOST,
            client_factory=deps.as_deps,
            sheet_day=SHEET_DAY,
        )
        self.assertEqual(result["status"], "filed", result)
        self.assertEqual([row["status"] for row in result["results"]], ["filed", "filed"])
        self.assertEqual(client.posted, [first_text, second_text])
        self.assertEqual([row["verified_by"] for row in [
            disc.file_note_to_existing_discussion(
                client, "220250093", first_text,
                title_hint=PROGRESSIVE_MEMO_RULE.workflow_title,
                document_id="501", ledger_path=ledger,
            )
        ]], ["ledger"])
        again = disc.file_note_to_existing_discussion(
            client, "220250093", first_text,
            title_hint=PROGRESSIVE_MEMO_RULE.workflow_title,
            document_id="501", ledger_path=ledger,
        )
        self.assertEqual(again["status"], "filed")
        self.assertTrue(again.get("idempotent"))
        self.assertEqual(len(client.posted), 2)
        self.assertIn("not sent again", again["reason"])


class RetrievalServiceTests(unittest.TestCase):
    def test_commands_cover_all_carriers_on_prod_paths(self):
        commands = service.carrier_argv(date(2026, 9, 25))
        self.assertEqual(
            set(commands),
            {"fao", "natgen", "geico", "travelers", "farmersofsalem", "guard", "progressive", "uticafirst"},
        )
        self.assertNotIn("bop", commands)
        for argv in commands.values():
            folder = argv[-1]
            self.assertTrue(folder.startswith("/opt/streetsmart-hermes/"))
            self.assertNotIn("streetsmart-hermes-test", folder)

    def test_new_carrier_runners_follow_the_pull_then_file_pattern(self):
        runners = service.default_runners(date(2026, 9, 25))
        self.assertEqual(set(runners), set(service.CARRIERS))
        for name in ("travelers", "farmersofsalem", "guard", "progressive", "uticafirst"):
            self.assertIn(name, service.OUTPUT_ROOTS)
            self.assertIn(name, service.PROD_CARRIER_RULES)

    def test_switch_off_holds_without_calling_runners(self):
        called = []
        record = service.run_retrieval(
            business_day=date(2026, 9, 25),
            directory="/tmp/robie-doc-retrieval-state",
            runners={"fao": lambda: called.append("fao") or {}},
            environ=PROD_OFF,
            hostname=PROD_HOST,
        )
        self.assertEqual(called, [])
        self.assertEqual(record["exit_code"], 1)
        self.assertGreater(record["held"], 0)
        self.assertEqual(record["filed"], 0)
        saved = json.loads(
            Path("/tmp/robie-doc-retrieval-state/document-retrieval-last-run.json").read_text()
        )
        self.assertEqual(saved["exit_code"], 1)

    def test_units_use_the_message_runtime_file_and_ship_the_switch_off(self):
        service_text = (ROOT / "deploy/systemd/robie-document-retrieval.service").read_text()
        health_text = (ROOT / "deploy/systemd/robie-document-retrieval-health.service").read_text()
        timer = (ROOT / "deploy/systemd/robie-document-retrieval.timer").read_text()
        health_timer = (ROOT / "deploy/systemd/robie-document-retrieval-health.timer").read_text()
        drop_in = (
            ROOT / "deploy/systemd/robie-document-retrieval.service.d/10-kill-switch.conf"
        ).read_text()
        runtime = "EnvironmentFile=/etc/streetsmart-hermes/robie-message-runtime.env"
        self.assertIn(runtime, service_text)
        self.assertIn(runtime, health_text)
        self.assertIn("Environment=ROBIE_DOCUMENT_RETRIEVAL_FILE_EZLYNX=0", service_text)
        self.assertIn("Environment=ROBIE_DOCUMENT_RETRIEVAL_FILE_EZLYNX=0", drop_in)
        self.assertNotIn("ROBIE_DOCUMENT_RETRIEVAL_FILE_EZLYNX=1", service_text)
        self.assertNotIn("ROBIE_DOCUMENT_RETRIEVAL_FILE_EZLYNX=1", drop_in)
        self.assertIn("User=streetsmart-hermes", service_text)
        self.assertIn("NOT enabled", timer)
        self.assertIn("Mon..Fri", timer)
        self.assertIn("*:0/15", health_timer)
        self.assertNotIn("bop", service_text.casefold())
        installer = (ROOT / "scripts/deploy-production-release.sh").read_text()
        self.assertIn("only test account", installer)
        self.assertIn("ROBIE_EZLYNX_WRITE_SCOPE=all", installer)
        self.assertIn("ROBIE_PLAYGROUND=1", installer)
        self.assertNotIn("agency-wide", installer)


class HealthTests(unittest.TestCase):
    def _write(self, directory: Path, **overrides):
        payload = {
            "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "exit_code": 0,
            "filed": 2,
            "held": 0,
            "duplicate": 0,
            "signals": [],
        }
        payload.update(overrides)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / health.LAST_RUN_NAME).write_text(json.dumps(payload), encoding="utf-8")

    def test_green_and_the_failure_signals(self):
        root = Path("/tmp/robie-doc-health")
        self._write(root)
        report = health.probe(directory=root)
        self.assertTrue(report["green"])
        self._write(root, exit_code=1)
        self.assertFalse(health.probe(directory=root)["green"])
        self._write(root, exit_code=0, held=1)
        self.assertFalse(health.probe(directory=root)["green"])
        self._write(root, signals=["EZLYNX_WRITE_SCOPE_REFUSED"])
        self.assertFalse(health.probe(directory=root)["green"])
        self._write(root, signals=["document_filed_note_held"])
        self.assertFalse(health.probe(directory=root)["green"])
        self.assertFalse(health.probe(directory=root / "missing")["green"])

    def test_stale_weekday_and_weekend_grace(self):
        root = Path("/tmp/robie-doc-health-stale")
        old = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)  # Wednesday
        self._write(root, run_at=old.isoformat())
        thursday = datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc)
        self.assertFalse(health.probe(directory=root, now=thursday)["green"])
        friday_run = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
        self._write(root, run_at=friday_run.isoformat())
        saturday = datetime(2026, 10, 3, 18, 0, tzinfo=timezone.utc)
        self.assertTrue(health.probe(directory=root, now=saturday)["green"])

    def test_alert_is_deduped_and_recovers_once(self):
        root = Path("/tmp/robie-doc-health-alert")
        notes = []
        self._write(root, exit_code=1)
        report = health.probe(directory=root)
        first = health.maybe_alert(report, directory=root, poster=notes.append)
        self.assertEqual(first["state"], "red-alerted")
        self.assertEqual(len(notes), 1)
        quiet = health.maybe_alert(report, directory=root, poster=notes.append)
        self.assertEqual(quiet["state"], "red-quiet")
        self.assertEqual(len(notes), 1)
        self._write(root, exit_code=0, held=0, signals=[])
        healthy = health.probe(directory=root)
        recovery = health.maybe_alert(healthy, directory=root, poster=notes.append)
        self.assertEqual(recovery["state"], "green")
        self.assertEqual(len(notes), 2)
        self.assertIn("healthy again", notes[1])


if __name__ == "__main__":
    unittest.main()
