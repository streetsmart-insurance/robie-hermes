"""Fixture tests for the carrier dry-run orchestrator. No live portals, no CDP."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from robie_job_engine import carrier_dry_run
from robie_job_engine.carrier_dry_run import (
    CARRIER_ORDER,
    SPECS,
    _normalize_receipt,
    _resolve_carrier_names,
    build_parser,
    render_summary,
    run_dry_run,
)
from robie_job_engine.intake_core import IntakeHold

AS_OF = date(2026, 10, 5)
TEST_ENV = {"ROBIE_ENV": "TEST"}


def _good_receipt(count=2):
    return {
        "status": "PULLED",
        "count": count,
        "downloaded": [{"filename": f"doc{i}.pdf"} for i in range(count)],
        "held": [],
        "skipped_already_delivered": ["skip1"],
    }


def _run_with_patches(tmp_path, run_behaviors, carriers=None):
    """Run the orchestrator with every module's run_pull patched.

    run_behaviors: dict carrier name -> receipt dict | Exception instance.
    """
    patches = []
    for name in CARRIER_ORDER:
        mod_name = "robie_job_engine." + SPECS[name].module_name.lstrip(".")
        behavior = run_behaviors.get(name, _good_receipt())
        if isinstance(behavior, Exception):
            side_effect = behavior
        elif callable(behavior):
            side_effect = behavior
        else:
            def _ret(*a, _b=behavior, **k):
                return _b
            side_effect = _ret
        if SPECS[name].runner == "natgen_adapter":
            p = patch("robie_job_engine.carrier_dry_run._run_natgen_pull", side_effect=side_effect)
        else:
            p = patch(mod_name + ".run_pull", side_effect=side_effect)
        patches.append(p)
    for p in patches:
        p.start()
    try:
        with patch.dict(os.environ, TEST_ENV):
            return run_dry_run(
                carriers,
                as_of=AS_OF,
                output_root=str(tmp_path),
                browser_factory=lambda spec: SimpleNamespace(name=spec.name),
            )
    finally:
        for p in patches:
            p.stop()


class ResolveCarrierNamesTests(unittest.TestCase):
    def test_default_is_all_in_order(self):
        self.assertEqual(_resolve_carrier_names(None), CARRIER_ORDER)

    def test_subset_preserves_canonical_order(self):
        self.assertEqual(
            _resolve_carrier_names("guard,progressive"),
            ("progressive", "guard"),
        )

    def test_duplicates_dropped(self):
        self.assertEqual(_resolve_carrier_names("guard,guard"), ("guard",))

    def test_unknown_carrier_holds(self):
        with self.assertRaises(IntakeHold):
            with patch.dict(os.environ, TEST_ENV):
                _resolve_carrier_names("acme")

    def test_empty_list_holds(self):
        with self.assertRaises(IntakeHold):
            with patch.dict(os.environ, TEST_ENV):
                _resolve_carrier_names("  , ")


class DryRunTests(unittest.TestCase):
    def test_all_ok(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            summary = _run_with_patches(Path(tmp), {})
        self.assertEqual(summary["as_of"], "2026-10-05")
        self.assertEqual(summary["mode"], "dry-run")
        self.assertEqual(summary["totals"], {"ok": 7, "held": 0, "failed": 0})
        for name, r in summary["carriers"].items():
            self.assertEqual(r["status"], "OK", name)
            self.assertEqual(r["downloaded"], 2, name)
            self.assertEqual(r["skipped"], 1, name)
            self.assertIsNone(r["error"], name)

    def test_one_failure_does_not_stop_others(self):
        import tempfile
        behaviors = {
            "progressive": IntakeHold("portal layout changed"),
            "guard": RuntimeError("boom"),
        }
        with tempfile.TemporaryDirectory() as tmp:
            summary = _run_with_patches(Path(tmp), behaviors)
        carriers = summary["carriers"]
        self.assertEqual(carriers["progressive"]["status"], "HELD")
        self.assertIn("portal layout changed", carriers["progressive"]["held"][0]["reason"])
        self.assertEqual(carriers["guard"]["status"], "FAILED")
        self.assertIn("RuntimeError", carriers["guard"]["error"])
        # Everyone else still ran.
        for name in ("geico", "travelers", "natgen", "uticafirst", "farmersofsalem"):
            self.assertEqual(carriers[name]["status"], "OK", name)
        self.assertEqual(summary["totals"], {"ok": 5, "held": 1, "failed": 1})

    def test_pull_held_details_surface_as_held(self):
        import tempfile

        class FakePullHeld(IntakeHold):
            def __init__(self):
                super().__init__("hard hold")
                self.details = {"held": [{"reason": "ledger conflict"}]}

        with tempfile.TemporaryDirectory() as tmp:
            summary = _run_with_patches(Path(tmp), {"geico": FakePullHeld()})
        r = summary["carriers"]["geico"]
        self.assertEqual(r["status"], "HELD")
        self.assertEqual(r["held"][0]["reason"], "ledger conflict")

    def test_subset_runs_only_named_carriers(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            summary = _run_with_patches(Path(tmp), {}, carriers="guard,uticafirst")
        self.assertEqual(set(summary["carriers"]), {"guard", "uticafirst"})
        self.assertEqual(summary["totals"]["ok"], 2)

    def test_requires_test_env(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(IntakeHold):
                    run_dry_run(
                        "guard",
                        as_of=AS_OF,
                        output_root=str(tmp),
                        browser_factory=lambda spec: SimpleNamespace(),
                    )

    def test_refuses_production_host(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, TEST_ENV):
                with patch("socket.gethostname", return_value="hermes-poc-01"), \
                     patch("socket.getfqdn", return_value="hermes-poc-01"):
                    with self.assertRaises(IntakeHold):
                        run_dry_run(
                            "guard",
                            as_of=AS_OF,
                            output_root=str(tmp),
                            browser_factory=lambda spec: SimpleNamespace(),
                        )

    def test_kill_switch_forced_off(self):
        import tempfile
        observed = {}

        def _recording_run(*a, **k):
            observed["kill_switch"] = os.environ.get("ROBIE_DOCUMENT_RETRIEVAL_FILE_EZLYNX")
            return _good_receipt()

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {**TEST_ENV, "ROBIE_DOCUMENT_RETRIEVAL_FILE_EZLYNX": "1"}):
                _run_with_patches(Path(tmp), {"guard": _recording_run}, carriers="guard")
        # Observed from inside the run: the orchestrator forced it off.
        self.assertEqual(observed.get("kill_switch"), "0")


class NormalizeReceiptTests(unittest.TestCase):
    def test_full_receipt(self):
        norm = _normalize_receipt(_good_receipt(count=3))
        self.assertEqual((norm["downloaded"], norm["skipped"], norm["held"]), (3, 1, []))

    def test_missing_keys_default_to_zero(self):
        norm = _normalize_receipt({"status": "PULLED"})
        self.assertEqual((norm["downloaded"], norm["skipped"], norm["held"]), (0, 0, []))

    def test_held_rows_preserved(self):
        norm = _normalize_receipt({"downloaded": [], "held": [{"reason": "x"}]})
        self.assertEqual(len(norm["held"]), 1)


class RenderSummaryTests(unittest.TestCase):
    def test_plain_english_lines(self):
        summary = {
            "as_of": "2026-10-05",
            "mode": "dry-run",
            "carriers": {
                "progressive": {"display": "Progressive (FAO)", "status": "OK", "downloaded": 2, "skipped": 1, "held": [], "error": None},
                "guard": {"display": "Guard", "status": "HELD", "downloaded": 0, "skipped": 0, "held": [{"reason": "login expired"}], "error": None},
                "geico": {"display": "GEICO", "status": "FAILED", "downloaded": 0, "skipped": 0, "held": [], "error": "RuntimeError: boom"},
            },
            "totals": {"ok": 1, "held": 1, "failed": 1},
        }
        text = render_summary(summary)
        self.assertIn("Progressive (FAO): OK", text)
        self.assertIn("2 downloaded, 1 skipped", text)
        self.assertIn("Guard: HELD", text)
        self.assertIn("login expired", text)
        self.assertIn("GEICO: FAILED", text)
        self.assertIn("1 ok, 1 held, 1 failed", text)
        self.assertIn("nothing filed to EZLynx", text)


class CliTests(unittest.TestCase):
    def test_defaults(self):
        args = build_parser().parse_args([])
        self.assertIsNone(args.carriers)
        self.assertIsNotNone(args.as_of)

    def test_carriers_and_as_of(self):
        args = build_parser().parse_args(["--carriers", "guard,geico", "--as-of", "2026-10-01"])
        self.assertEqual(args.carriers, "guard,geico")
        self.assertEqual(args.as_of, "2026-10-01")

    def test_main_bad_date_exits_2(self):
        with patch.object(sys, "argv", ["carrier_dry_run", "--as-of", "not-a-date"]):
            self.assertEqual(carrier_dry_run.main(["--as-of", "not-a-date"]), 2)


if __name__ == "__main__":
    unittest.main()
