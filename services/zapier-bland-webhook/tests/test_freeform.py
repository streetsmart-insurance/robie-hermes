"""Tests for freeform context building (mocked EZLynx) — note_body first.

Instruction priority for the freeform "Robie Call" flow:
1. note_body from the webhook (PRIMARY) — the note text POSTed by the
   Zapier "New Note" trigger (the EZLynx API never returns note bodies).
2. Discussion title via the v8 API (FALLBACK) — only when no note_body.
3. Empty/generic -> proceed on applicant context only.

discussion_id is still recorded for the writeback.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dispatcher import build_freeform_context, is_generic_title
from prompts import build_freeform_task, EVA_IDENTITY


class FakeEZ:
    """Minimal EZLynxClient double; records title-API usage."""

    def __init__(self, applicant=None, policies=None, discussions=None):
        self._applicant = applicant or {}
        self._policies = policies or []
        # discussions: list of v8-shaped dicts (discussionId, title, lastModified)
        self._discussions = discussions or []
        self.by_id_calls = []       # applicant_id/discussion_id lookups
        self.most_recent_calls = 0  # count of most_recent_discussion calls

    def get_applicant(self, applicant_id):
        if not self._applicant:
            return {"status": "error", "error": "not found"}
        return {"status": "success", "applicant": self._applicant}

    def get_applicant_policies(self, applicant_id):
        return {"status": "success", "policies": self._policies}

    def get_discussions(self, applicant_id):
        return self._discussions

    def get_discussion_by_id(self, applicant_id, discussion_id):
        self.by_id_calls.append(discussion_id)
        for d in self._discussions:
            if str(d.get("discussionId") or d.get("id") or "") == discussion_id:
                return d
        return None

    def most_recent_discussion(self, applicant_id):
        self.most_recent_calls += 1
        best, best_ts = None, ""
        for d in self._discussions:
            ts = str(d.get("lastModified") or "")
            if ts >= best_ts:
                best, best_ts = d, ts
        return best


APPLICANT = {
    "FirstName": "Jane",
    "LastName": "Doe",
    "CellPhone": "(555) 123-4567",
    "BusinessName": "",
}

POLICIES = [
    {"policy_number": "ABC123", "carrier_name": "Progressive",
     "line_of_business": "Auto", "expiration_date": "2026-11-01"},
]

DISCUSSIONS = [
    {"discussionId": "d1", "title": "Call about renewal",
     "lastModified": "2026-10-02T10:00:00", "applicantId": "123"},
    {"discussionId": "d2", "title": "Untitled",
     "lastModified": "2026-10-01T10:00:00", "applicantId": "123"},
]

NOTE_BODY = ("Hi Eva, please call Jane about her auto policy renewal. "
             "The Progressive policy ABC123 expires Nov 1 and she has "
             "not responded to our two emails.")


# ----------------------------------------------------------------------
# PRIMARY: note_body from the webhook
# ----------------------------------------------------------------------
def test_note_body_used_as_instruction():
    ez = FakeEZ(applicant=APPLICANT, policies=POLICIES, discussions=DISCUSSIONS)
    ctx = build_freeform_context(
        ez, "123", "Robie Call", discussion_id="d1", note_body=NOTE_BODY)
    assert ctx["instruction"] == NOTE_BODY
    assert ctx["instruction_source"] == "webhook note_body"
    assert ctx["name"] == "Jane Doe"
    assert ctx["phone"] == "(555) 123-4567"
    assert ctx["trigger_discussion_id"] == "d1"  # writeback still recorded
    assert ctx["errors"] == []


def test_note_body_skips_title_api_calls():
    """No discussion API lookup is needed when note_body is present."""
    ez = FakeEZ(applicant=APPLICANT, policies=POLICIES, discussions=DISCUSSIONS)
    build_freeform_context(
        ez, "123", "Robie Call", discussion_id="d1", note_body=NOTE_BODY)
    assert ez.by_id_calls == []
    assert ez.most_recent_calls == 0


def test_note_body_without_discussion_id():
    ez = FakeEZ(applicant=APPLICANT, policies=POLICIES, discussions=DISCUSSIONS)
    ctx = build_freeform_context(ez, "123", "Robie Call", note_body=NOTE_BODY)
    assert ctx["instruction"] == NOTE_BODY
    assert ctx["instruction_source"] == "webhook note_body"
    assert ctx["trigger_discussion_id"] is None
    assert ez.by_id_calls == []
    assert ez.most_recent_calls == 0


def test_blank_note_body_falls_back_to_title():
    ez = FakeEZ(applicant=APPLICANT, policies=POLICIES, discussions=DISCUSSIONS)
    ctx = build_freeform_context(
        ez, "123", "Robie Call", discussion_id="d1", note_body="   ")
    assert ctx["instruction"] == "Call about renewal"
    assert ctx["instruction_source"] == "discussion d1 title"
    assert ez.by_id_calls == ["d1"]


# ----------------------------------------------------------------------
# FALLBACK: discussion title via v8 (no note_body)
# ----------------------------------------------------------------------
def test_title_fallback_with_discussion_id():
    ez = FakeEZ(applicant=APPLICANT, policies=POLICIES, discussions=DISCUSSIONS)
    ctx = build_freeform_context(ez, "123", "Robie Call", discussion_id="d1")
    assert ctx["instruction"] == "Call about renewal"
    assert ctx["instruction_source"] == "discussion d1 title"
    assert ctx["trigger_discussion_id"] == "d1"
    assert ctx["errors"] == []


def test_title_fallback_to_most_recent_discussion():
    ez = FakeEZ(applicant=APPLICANT, policies=POLICIES, discussions=DISCUSSIONS)
    ctx = build_freeform_context(ez, "123", "Robie Call")  # no discussion_id
    # d1 is the most recently modified and has a real title.
    assert ctx["instruction"] == "Call about renewal"
    assert ctx["instruction_source"] == "most recent discussion title"
    assert ctx["trigger_discussion_id"] == "d1"
    assert ez.most_recent_calls == 1


def test_title_fallback_unknown_discussion_id():
    ez = FakeEZ(applicant=APPLICANT, policies=POLICIES, discussions=DISCUSSIONS)
    ctx = build_freeform_context(ez, "123", "Robie Call", discussion_id="nope")
    assert ctx["instruction"] == "Call about renewal"  # fell back to d1
    assert ctx["instruction_source"] == "most recent discussion title"
    assert ctx["trigger_discussion_id"] == "d1"  # the FOUND discussion
    assert any("not found" in e for e in ctx["errors"])


def test_title_fallback_generic_title_proceeds_with_context_only():
    ez = FakeEZ(applicant=APPLICANT, policies=POLICIES, discussions=DISCUSSIONS)
    ctx = build_freeform_context(ez, "123", "Robie Call", discussion_id="d2")
    assert ctx["instruction"] == ""
    assert ctx["instruction_source"] == ""  # no usable title found
    assert any("empty/generic" in e for e in ctx["errors"])
    # Still proceeds: phone + policies are there.
    assert ctx["phone"] == "(555) 123-4567"
    assert len(ctx["policies"]) == 1
    assert ctx["trigger_discussion_id"] == "d2"


def test_no_discussions_recorded():
    ez = FakeEZ(applicant=APPLICANT, policies=POLICIES, discussions=[])
    ctx = build_freeform_context(ez, "123", "Robie Call")
    assert any("no discussions found" in e for e in ctx["errors"])
    assert ctx["trigger_discussion_id"] is None


def test_applicant_failure_recorded_but_title_still_read():
    ez = FakeEZ(discussions=DISCUSSIONS)
    ctx = build_freeform_context(ez, "123", "Robie Call", discussion_id="d1")
    assert any("applicant lookup failed" in e for e in ctx["errors"])
    assert ctx["name"] == "the client"  # safe fallback
    assert ctx["instruction"] == "Call about renewal"  # title still read


def test_is_generic_title():
    assert is_generic_title("")
    assert is_generic_title("   ")
    assert is_generic_title("Untitled")
    assert is_generic_title("UNTITLED")
    assert is_generic_title("New Discussion")
    assert not is_generic_title("Call about renewal")
    assert not is_generic_title("Follow up on Tuesday's quote")


# ----------------------------------------------------------------------
# Prompt construction: source label follows the real instruction source
# ----------------------------------------------------------------------
def test_freeform_task_labels_note_body_source():
    task = build_freeform_task(
        "Jane Doe", "Jane", NOTE_BODY, POLICIES, "(555) 123-4567",
        instruction_source="webhook note_body",
    )
    assert "Eva" in task
    assert "AI assistant" in task
    assert "Jake" in task and "StreetSmart Insurance" in task
    assert NOTE_BODY in task
    assert "ABC123" in task  # policy context present
    assert "Do NOT ask the client what the call is about" in task
    assert "the EZLynx note the CSR wrote" in task
    # Screener + end rules are part of every task.
    assert "screener" in task.lower()
    assert EVA_IDENTITY.split(".")[0] in task


def test_freeform_task_labels_title_fallback_source():
    task = build_freeform_task(
        "Jane Doe", "Jane", "Call about the renewal", [], "(555) 123-4567",
        instruction_source="discussion d1 title",
    )
    assert "discussion title" in task.lower()
    assert "fallback" in task.lower()


def test_freeform_task_default_source_still_builds():
    """Old callers that don't pass instruction_source keep working."""
    task = build_freeform_task(
        "Jane Doe", "Jane", "call about renewal", POLICIES, "(555) 123-4567",
    )
    assert "call about renewal" in task
    assert "ABC123" in task


# ----------------------------------------------------------------------
# dispatch(): note_body flows end to end into the Bland payload
# ----------------------------------------------------------------------
def test_dispatch_note_body_reaches_bland_task():
    from dispatcher import dispatch
    from bland_client import BlandClient

    ez = FakeEZ(applicant=APPLICANT, policies=POLICIES, discussions=DISCUSSIONS)
    bland = BlandClient()  # dry_run=True -> no network, no real call
    result = dispatch(
        "123", "robie-call", "Robie Call", "",
        discussion_id="d1", note_body=NOTE_BODY + " Call at 908-555-0199.",
        dry_run=True, ez=ez, bland=bland,
    )
    assert result["ok"] is True
    assert result["flow"] == "freeform"
    # Robie Call dials only the typed number (Carlo, Oct 7 2026).
    assert result["call_summary"]["attempts"][0]["payload"]["phone_number"] == "+19085550199"
    assert result["instruction_source"] == "webhook note_body"
    assert result["dry_run"] is True
    task = result["call_summary"]["attempts"][0]["payload"]["task"]
    assert NOTE_BODY in task
    assert "the EZLynx note the CSR wrote" in task
    assert result["trigger_discussion_id"] == "d1"
    # writeback is skipped in dry-run, never attempted against the API.
    assert result["writeback"] == {"skipped": "dry_run"}
    # No discussion API lookup was needed for the instruction.
    assert ez.by_id_calls == []
    assert ez.most_recent_calls == 0


def test_dispatch_title_fallback_reaches_bland_task():
    from dispatcher import dispatch
    from bland_client import BlandClient

    ez = FakeEZ(applicant=APPLICANT, policies=POLICIES, discussions=DISCUSSIONS)
    bland = BlandClient()
    result = dispatch(
        "123", "robie-call", "Robie Call", "",
        discussion_id="d1", note_body="",  # no note text from the Zap
        dry_run=True, ez=ez, bland=bland,
    )
    # No note text means no typed number: a Robie Call never falls back to
    # the number on file, so nothing is dialed (Carlo, Oct 7 2026).
    assert result["ok"] is False
    assert result["needs_typed_number"] is True
    assert result["instruction_source"] == "discussion d1 title"
    assert "call_summary" not in result
    assert ez.by_id_calls == ["d1"]  # title lookup happened this time


# ----------------------------------------------------------------------
# dispatch(): writeback failures are surfaced (not silently ok)
# ----------------------------------------------------------------------
class FakeEZWritebackFail(FakeEZ):
    """EZ double where the Discussion API 403s on note write."""

    def append_note(self, discussion_id, body):
        return {"status": "error", "code": 403,
                "error": "The user does not have access to the Applicant."}

    def upload_document(self, applicant_id, document_name, file_bytes,
                        content_type="audio/mpeg"):
        return {"status": "error", "error": "not attempted in test"}


class FakeBlandNoNetwork:
    """Bland double: fake call summary, no network, no real dial."""

    def call_with_double_dial(self, phone, task, first_sentence,
                              voicemail_message, metadata=None, dry_run=False, halt_check=None):
        return {
            "mode": "LIVE_BLAND_AI",
            "phone": phone,
            "attempts": [{"attempt": 1, "call_id": "fake-call-1",
                          "success": True, "mode": "LIVE_BLAND_AI", "final_status": {"status": "completed", "answered_by": "human", "duration": 60}}],
            "voicemail_hit": False,
            "redialed": False,
            "outcome": "completed",
        }

    def get_call(self, call_id):
        return {}


def test_dispatch_writeback_403_reports_not_ok():
    """Regression: 2026-10-03 live call reported ok=True while the
    EZLynx outcome note 403'd. The writeback failure must flip ok to
    False and surface a writeback_error."""
    from dispatcher import dispatch

    ez = FakeEZWritebackFail(applicant=APPLICANT, policies=POLICIES,
                             discussions=DISCUSSIONS)
    bland = FakeBlandNoNetwork()
    result = dispatch(
        "123", "robie-unresponsive", "Robie unresponsive", "",
        discussion_id="d1", note_body="",
        dry_run=False, ez=ez, bland=bland,
    )
    # The call itself "succeeded" (fake), but the writeback 403'd.
    assert result["ok"] is False, (
        f"writeback 403 must flip ok to False, got: {result}")
    assert "writeback_error" in result, (
        f"writeback_error missing from result: {result}")
    wb = result.get("writeback") or {}
    note_res = wb.get("note") or {}
    assert note_res.get("status") == "error"
    assert note_res.get("code") == 403


def test_dispatch_writeback_success_stays_ok():
    """When the note write succeeds, ok stays True."""
    from dispatcher import dispatch

    class FakeEZWritebackOk(FakeEZ):
        def append_note(self, discussion_id, body):
            return {"status": "success", "data": {"noteId": "n1"}}

        def upload_document(self, applicant_id, document_name, file_bytes,
                            content_type="audio/mpeg"):
            return {"status": "error", "error": "no recording yet"}

    ez = FakeEZWritebackOk(applicant=APPLICANT, policies=POLICIES,
                           discussions=DISCUSSIONS)
    bland = FakeBlandNoNetwork()
    result = dispatch(
        "123", "robie-unresponsive", "Robie unresponsive", "",
        discussion_id="d1", note_body="",
        dry_run=False, ez=ez, bland=bland,
    )
    assert result["ok"] is True
    assert "writeback_error" not in result
