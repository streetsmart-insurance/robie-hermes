"""Regression tests for the staff automation jobs (no network, no secrets)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from robie_job_engine import meeting_synthesis as ms
from robie_job_engine import staff_fun as sf
from robie_job_engine.request_routing import BOUNDED_ENGINE_ACTIONS, WORKER_FOR_ACTION


def test_dedupe_collapses_duplicate_saves():
    notes = [
        {"id": "a", "name": "Daily PL Huddle - 2026/09/24 09:13 EDT - Notes by Gemini"},
        {"id": "b", "name": "Daily PL Huddle - 2026/09/24 09:13 EDT - Notes by Gemini"},
        {"id": "c", "name": "Daily PL Huddle - 2026/09/23 09:13 EDT - Notes by Gemini"},
    ]
    out = ms.dedupe_notes(notes)
    assert [n["id"] for n in out] == ["a", "c"]


def test_classify_department_keywords():
    assert ms.classify_department("CL Team Meeting - 2026/09/11 10:01 EDT - Notes by Gemini") == "Commercial Lines"
    assert ms.classify_department("Daily PL Huddle - 2026/09/24 09:13 EDT - Notes by Gemini") == "Personal Lines"
    assert ms.classify_department("Truck Meeting - 2026/09/09 12:06 EDT - Notes by Gemini") == "Trucking & Transportation"
    assert ms.classify_department("Certificates Meeting - 2026/09/23 09:07 EDT - Notes by Gemini") == "Certificates"
    assert ms.classify_department("1 on 1 Erika Palacios - 2026/09/23 15:00 EDT - Notes by Gemini") == ms.OTHER_BUCKET
    assert ms.classify_department("AM Team Sync - 2026/09/23 10:03 EDT - Notes by Gemini") == ms.OTHER_BUCKET


def test_doc_text_extraction():
    doc = {"body": {"content": [
        {"paragraph": {"elements": [{"textRun": {"content": "hello "}}, {"textRun": {"content": "world"}}]}},
        {"paragraph": {"elements": [{"textRun": {"content": "\n"}}]}},
    ]}}
    assert ms.doc_text(doc) == "hello world"


def test_fun_plan_covers_all_months():
    assert sorted(sf.FUN_PLAN.keys()) == list(range(1, 13))
    for month, plan in sf.FUN_PLAN.items():
        assert plan["name"] and plan["instructions"] and plan["scope"]
        assert plan["entry_method"] in {"chat_thread", "google_form"}


def test_october_is_costume_contest():
    assert sf.FUN_PLAN[10]["name"] == "Halloween costume contest"
    assert sf.FUN_PLAN[2]["name"] == "Super Bowl squares"


def test_routing_registered():
    assert WORKER_FOR_ACTION["meeting.synthesis.weekly"] == "meeting-synthesis"
    assert WORKER_FOR_ACTION["staff.fun.monthly"] == "staff-fun"
    assert WORKER_FOR_ACTION["staff.holiday.alert"] == "staff-holiday-alert"
    assert "meeting.synthesis.weekly" in BOUNDED_ENGINE_ACTIONS
    assert "staff.fun.monthly" in BOUNDED_ENGINE_ACTIONS
    assert "staff.holiday.alert" in BOUNDED_ENGINE_ACTIONS


def test_schedule_specs():
    from robie_job_engine.staff_jobs_schedule import DEFAULT_SCHEDULES
    specs = {action: cron for _, action, cron in DEFAULT_SCHEDULES}
    assert specs["meeting.synthesis.weekly"] == "0 8 * * 1"
    assert specs["staff.fun.monthly"] == "0 9 1 * *"
    assert specs["staff.holiday.alert"] == "0 9 * * 1-5"


def test_email_body_marks_social_drafts_unposted():
    body = ms.build_email_body("week of Sep 18 – Sep 25, 2026",
                               {"Personal Lines": "Good week."}, "1. draft")
    assert "Nothing was posted" in body
    assert "Personal Lines" in body


def test_second_friday():
    assert sf.second_friday(2026, 10) == "Friday, October 9"
    assert sf.second_friday(2026, 9) == "Friday, September 11"
