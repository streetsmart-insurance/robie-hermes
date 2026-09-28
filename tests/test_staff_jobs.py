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
    assert "meeting.synthesis.weekly" in BOUNDED_ENGINE_ACTIONS
    assert "staff.fun.monthly" in BOUNDED_ENGINE_ACTIONS


def test_schedule_specs():
    from robie_job_engine.staff_jobs_schedule import DEFAULT_SCHEDULES
    specs = {action: cron for _, action, cron in DEFAULT_SCHEDULES}
    assert specs["meeting.synthesis.weekly"] == "0 8 * * 1"
    assert specs["staff.fun.monthly"] == "0 9 1 * *"


def test_email_body_marks_social_drafts_unposted():
    body = ms.build_email_body("week of Sep 18 – Sep 25, 2026",
                               {"Personal Lines": "Good week."}, "1. draft")
    assert "Nothing was posted" in body
    assert "Personal Lines" in body


def test_second_friday():
    assert sf.second_friday(2026, 10) == "Friday, October 9"
    assert sf.second_friday(2026, 9) == "Friday, September 11"


def test_smart_reward_regex_variants():
    assert ms.SMART_REWARD_RE.search("Angie earned a Smart Reward for the save")
    assert ms.SMART_REWARD_RE.search("smart rewards were announced in the huddle")
    assert ms.SMART_REWARD_RE.search("SMART REWARD")
    assert not ms.SMART_REWARD_RE.search("rewarding work this week")
    assert not ms.SMART_REWARD_RE.search("the reward program launched")


def test_spotlight_prompt_rules():
    prompt = ms.SPOTLIGHT_PROMPT.format(n=2, mentions="x")
    assert "280" in prompt
    assert "Never invent" in prompt
    assert "first name only" in prompt


def test_draft_spotlight_posts_uses_prompt(monkeypatch):
    seen = {}

    def fake_generate(prompt):
        seen["prompt"] = prompt
        return "spotlight draft"

    monkeypatch.setattr(ms.common, "gemini_generate", fake_generate)
    out = ms.draft_spotlight_posts("Angie got a Smart Reward")
    assert out == "spotlight draft"
    assert "Angie got a Smart Reward" in seen["prompt"]
    assert "280" in seen["prompt"]


def _run_synthesis_perform(monkeypatch, notes):
    """Run MeetingSynthesisWorker._perform with all I/O faked.

    notes: list of (name, body). Returns (result, captured) where captured
    holds the gmail kwargs and the appended doc text.
    """
    captured = {}
    monkeypatch.setattr(
        ms, "fetch_notes_last_7_days",
        lambda: [{"id": str(i), "name": name} for i, (name, _) in enumerate(notes)],
    )
    bodies = {str(i): body for i, (_, body) in enumerate(notes)}
    monkeypatch.setattr(ms, "read_note_body", lambda doc_id: bodies[doc_id])
    monkeypatch.setattr(ms, "synthesize_department", lambda dept, text: f"synth:{dept}")
    monkeypatch.setattr(ms, "draft_social_posts", lambda wins, n=3: "DRAFTS")
    monkeypatch.setattr(
        ms, "draft_spotlight_posts", lambda mentions, n=3: f"SPOTLIGHTS<<{mentions}>>"
    )

    def fake_send_gmail(**kwargs):
        captured.update(kwargs)
        return "mid-1"

    monkeypatch.setattr(ms.common, "send_gmail", fake_send_gmail)
    monkeypatch.setattr(ms, "find_or_create_social_doc", lambda: "doc-1")
    monkeypatch.setattr(
        ms, "append_to_doc", lambda doc_id, text: captured.update(doc_text=text)
    )
    result = ms.MeetingSynthesisWorker()._perform({})
    return result, captured


def _long(text):
    return (text + " ") * 3


def test_perform_adds_spotlight_section_when_smart_reward_mentioned(monkeypatch):
    notes = [
        ("Daily PL Huddle - 2026/09/24 09:13 EDT - Notes by Gemini",
         _long("Angie earned a Smart Reward for saving the Johnson renewal with a same-day rewrite.")),
        ("CL Team Meeting - 2026/09/24 10:01 EDT - Notes by Gemini",
         _long("Pipeline updates and carrier follow-ups, nothing about recognition.")),
    ]
    result, captured = _run_synthesis_perform(monkeypatch, notes)
    assert result.succeeded
    # Email carries its own spotlight section with the generated drafts.
    assert "Smart Rewards spotlights" in captured["body"]
    assert "SPOTLIGHTS<<" in captured["body"]
    # Only the mentioning note feeds the spotlight; the other note stays out.
    assert "Daily PL Huddle" in captured["body"]
    assert "CL Team Meeting" not in captured["body"]
    # The social doc gets the spotlights under their own header.
    assert "--- Smart Rewards spotlights ---" in captured["doc_text"]
    assert "SPOTLIGHTS<<" in captured["doc_text"]
    assert result.detail["smart_reward_mentions"] == 1


def test_perform_records_no_mentions_when_absent(monkeypatch):
    notes = [
        ("Daily PL Huddle - 2026/09/24 09:13 EDT - Notes by Gemini",
         _long("Pipeline updates and carrier follow-ups, nothing about recognition.")),
    ]
    result, captured = _run_synthesis_perform(monkeypatch, notes)
    assert result.succeeded
    assert "No Smart Rewards mentions this week." in captured["body"]
    assert "No Smart Rewards mentions this week." in captured["doc_text"]
    assert "--- Smart Rewards spotlights ---" in captured["doc_text"]
    assert result.detail["smart_reward_mentions"] == 0


def test_email_body_includes_spotlight_section():
    body = ms.build_email_body("week of Sep 18 – Sep 25, 2026",
                               {"Personal Lines": "Good week."}, "1. draft",
                               "1. Shout-out to Angie!")
    assert "Smart Rewards spotlights" in body
    assert "Shout-out to Angie!" in body
    assert "Nothing was posted" in body  # social drafts still draft-only


def test_email_body_defaults_to_no_mentions():
    body = ms.build_email_body("week of Sep 18 – Sep 25, 2026",
                               {"Personal Lines": "Good week."}, "1. draft")
    assert "No Smart Rewards mentions this week." in body
