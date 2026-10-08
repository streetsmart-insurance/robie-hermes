"""Reliability feature tests: failure alerting, kill switch, double-label
guard, circuit-breaker alert, MP3 note clarity.

All chat posts are mocked — no real HTTP. No real Bland calls.
"""
import sys
import os
import time
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import alerts
from alerts import (
    FailureTracker, DoubleLabelTracker, kill_switch_active,
    reset_kill_cache_for_tests, post_chat_alert,
)
from dispatcher import dispatch
from writeback import format_call_note
from bland_client import BlandClient


APPLICANT = {
    "FirstName": "Jane", "LastName": "Doe",
    "CellPhone": "(555) 123-4567", "BusinessName": "",
}


class FakeEZ:
    def __init__(self, discussions=None):
        self._discussions = discussions if discussions is not None else [
            {"discussionId": "d1", "title": "t", "lastModified": "2026-10-02",
             "applicantId": "123"}]
        self.appended = []

    def get_applicant(self, applicant_id):
        return {"status": "success", "applicant": APPLICANT}

    def get_applicant_policies(self, applicant_id):
        return {"status": "success", "policies": []}

    def get_discussions(self, applicant_id):
        return self._discussions

    def get_discussion_by_id(self, applicant_id, discussion_id):
        return None

    def most_recent_discussion(self, applicant_id):
        return self._discussions[0] if self._discussions else None

    def append_note(self, discussion_id, body):
        self.appended.append((discussion_id, body))
        return {"status": "success", "data": {}}

    def upload_document(self, *a, **k):
        return {"status": "success", "document_id": "doc1"}


class FakeBland:
    def __init__(self, success=True):
        self.success = success
        self.calls = 0

    def call_with_double_dial(self, *a, **k):
        self.calls += 1
        if self.success:
            return {"mode": "LIVE", "attempts": [
                {"success": True, "call_id": "c1", "mode": "LIVE", "final_status": {"status": "completed", "answered_by": "human", "duration": 60}}],
                    "voicemail_hit": False, "redialed": False}
        return {"mode": "LIVE", "attempts": [
            {"success": False, "error": "boom", "mode": "LIVE"}]}

    def get_call(self, call_id):
        return {}


# ----------------------------------------------------------------------
# 1. FailureTracker: 3 in 15 min -> alert; 2 -> silent; reset after alert
# ----------------------------------------------------------------------
def test_failure_tracker_alerts_on_three():
    t = FailureTracker(threshold=3, window_s=900)
    posted = []
    with patch("alerts.post_chat_alert", side_effect=lambda m: posted.append(m) or True):
        assert t.record("1", "c", "e1") is False
        assert t.record("1", "c", "e2") is False
        assert t.record("1", "c", "e3") is True
    assert len(posted) == 1
    assert "3 failures in last 15 min" in posted[0]
    assert "e3" in posted[0]


def test_failure_tracker_silent_on_two():
    t = FailureTracker(threshold=3, window_s=900)
    posted = []
    with patch("alerts.post_chat_alert", side_effect=lambda m: posted.append(m) or True):
        assert t.record("1", "c", "e1") is False
        assert t.record("1", "c", "e2") is False
    assert posted == []


def test_failure_tracker_resets_after_alert():
    t = FailureTracker(threshold=3, window_s=0.05)
    posted = []
    with patch("alerts.post_chat_alert", side_effect=lambda m: posted.append(m) or True):
        t.record("1", "c", "e1")
        t.record("1", "c", "e2")
        assert t.record("1", "c", "e3") is True   # alert #1
        time.sleep(0.08)  # window expires -> counter and alert throttle reset
        assert t.record("1", "c", "e4") is False
        assert t.record("1", "c", "e5") is False
        assert t.record("1", "c", "e6") is True    # alert #2
    assert len(posted) == 2


def test_failure_tracker_window_expiry():
    t = FailureTracker(threshold=3, window_s=0.05)
    posted = []
    with patch("alerts.post_chat_alert", side_effect=lambda m: posted.append(m) or True):
        t.record("1", "c", "e1")
        t.record("1", "c", "e2")
        time.sleep(0.08)  # window expires
        assert t.record("1", "c", "e3") is False  # only 1 in window
    assert posted == []


# ----------------------------------------------------------------------
# 2. Kill switch
# ----------------------------------------------------------------------
def test_kill_switch_env_var_halts():
    reset_kill_cache_for_tests()
    with patch.dict(os.environ, {"ROBIE_HALT": "1"}, clear=False):
        assert kill_switch_active() is True
    reset_kill_cache_for_tests()


def test_kill_switch_off_by_default():
    reset_kill_cache_for_tests()
    env = {k: v for k, v in os.environ.items()
           if k not in ("ROBIE_HALT", "ROBIE_READ_ONLY")}
    with patch.dict(os.environ, env, clear=True):
        with patch("alerts._read_kill_secret", return_value=False):
            assert kill_switch_active() is False
    reset_kill_cache_for_tests()


def test_kill_switch_secret_halts_dispatch():
    reset_kill_cache_for_tests()
    ez, bland = FakeEZ(), FakeBland()
    # Simulate production: DRY_RUN=0, dry_run=None (app.py never passes it).
    with patch.dict(os.environ, {"DRY_RUN": "0"}, clear=False):
        # Config reads env at import; patch the class attribute directly.
        with patch("dispatcher.Config.DRY_RUN", False):
            with patch("alerts._read_kill_secret", return_value=True):
                res = dispatch("123", "robie-audit", dry_run=None, ez=ez, bland=bland)
    assert res["ok"] is False
    assert res["error"] == "kill switch active"
    assert bland.calls == 0  # no call placed
    reset_kill_cache_for_tests()


def test_kill_switch_caches_secret():
    reset_kill_cache_for_tests()
    calls = {"n": 0}

    def fake_read():
        calls["n"] += 1
        return False

    env = {k: v for k, v in os.environ.items()
           if k not in ("ROBIE_HALT", "ROBIE_READ_ONLY")}
    with patch.dict(os.environ, env, clear=True):
        with patch("alerts._read_kill_secret", side_effect=fake_read):
            kill_switch_active()
            kill_switch_active()
            kill_switch_active()
    assert calls["n"] == 1  # cached, not re-read
    reset_kill_cache_for_tests()


# ----------------------------------------------------------------------
# 3. Writeback-failure chat alert (via app._run_dispatch)
# ----------------------------------------------------------------------
def test_writeback_failure_posts_chat_alert():
    import app as appmod

    class FailNoteEZ(FakeEZ):
        def append_note(self, discussion_id, body):
            return {"status": "error", "error": "403 forbidden"}

    ez, bland = FailNoteEZ(), FakeBland(success=True)
    posted = []
    alerts.failures.reset_for_tests()
    alerts.double_labels.reset_for_tests()
    # app.py does `from alerts import post_chat_alert`, so patch the
    # reference in the app module, not the alerts module.
    with patch("app.post_chat_alert", side_effect=lambda m: posted.append(m) or True):
        with patch("app.dispatch", return_value={
                "ok": True,  # call ok...
                "writeback_error": "403 forbidden",  # ...but note failed
                "applicant_id": "123", "campaign_id": "robie-audit",
                "call_summary": {"attempts": [{"call_id": "c1"}]}}):
            appmod._run_dispatch("123", "robie-audit", "", "", "d1", "")
    assert any("writeback failed" in m for m in posted), posted
    assert any("123" in m for m in posted)
    alerts.failures.reset_for_tests()
    alerts.double_labels.reset_for_tests()


def test_successful_dispatch_posts_no_alert():
    import app as appmod

    posted = []
    alerts.failures.reset_for_tests()
    alerts.double_labels.reset_for_tests()
    with patch("app.post_chat_alert", side_effect=lambda m: posted.append(m) or True):
        with patch("app.dispatch", return_value={
                "ok": True, "applicant_id": "123",
                "campaign_id": "robie-audit", "call_summary": {"attempts": []}}):
            appmod._run_dispatch("123", "robie-audit", "", "", "d1", "")
    assert posted == []
    alerts.failures.reset_for_tests()
    alerts.double_labels.reset_for_tests()


# ----------------------------------------------------------------------
# 4. Double-label guard
# ----------------------------------------------------------------------
def test_double_label_different_campaigns_alerts():
    t = DoubleLabelTracker(window_s=300)
    assert t.check("123", "robie-audit") is None
    msg = t.check("123", "robie-cancellation")
    assert msg is not None
    assert "123" in msg and "robie-audit" in msg and "robie-cancellation" in msg


def test_double_label_same_campaign_silent():
    t = DoubleLabelTracker(window_s=300)
    assert t.check("123", "robie-audit") is None
    assert t.check("123", "robie-audit") is None  # same campaign: no alert


def test_double_label_window_expiry():
    t = DoubleLabelTracker(window_s=0.05)
    assert t.check("123", "robie-audit") is None
    time.sleep(0.08)
    assert t.check("123", "robie-cancellation") is None  # outside window


def test_double_label_posts_to_chat_via_run_dispatch():
    import app as appmod

    posted = []
    alerts.failures.reset_for_tests()
    alerts.double_labels.reset_for_tests()
    with patch("app.post_chat_alert", side_effect=lambda m: posted.append(m) or True):
        with patch("app.dispatch", return_value={
                "ok": True, "applicant_id": "123",
                "campaign_id": "robie-audit", "call_summary": {"attempts": []}}):
            appmod._run_dispatch("123", "robie-audit", "", "", "d1", "")
            appmod._run_dispatch("123", "robie-cancellation", "", "", "d2", "")
    assert any("Two different labels" in m for m in posted), posted
    alerts.failures.reset_for_tests()
    alerts.double_labels.reset_for_tests()


# ----------------------------------------------------------------------
# 5. Circuit breaker trip alerts once
# ----------------------------------------------------------------------
def test_circuit_breaker_trip_alerts_once():
    BlandClient._reset_circuit_for_tests()
    posted = []
    with patch("alerts.post_chat_alert", side_effect=lambda m: posted.append(m) or True):
        # Patch the import inside _circuit_record: it does
        # `from alerts import post_chat_alert` at call time, so patch
        # alerts.post_chat_alert itself.
        import alerts as alerts_mod
        orig = alerts_mod.post_chat_alert
        alerts_mod.post_chat_alert = lambda m: posted.append(m) or True
        try:
            for _ in range(10):
                BlandClient._cb_failures = 0  # reset each loop? no - accumulate
            # Simulate 5 consecutive failures (threshold).
            from bland_client import CIRCUIT_THRESHOLD
            for _ in range(CIRCUIT_THRESHOLD):
                b = BlandClient.__new__(BlandClient)
                b._circuit_record(False)
        finally:
            alerts_mod.post_chat_alert = orig
    # Exactly one trip alert for the threshold crossing.
    trips = [m for m in posted if "circuit breaker tripped" in m]
    assert len(trips) == 1, posted
    BlandClient._reset_circuit_for_tests()


# ----------------------------------------------------------------------
# 6. MP3 note clarity
# ----------------------------------------------------------------------
def test_note_says_recording_unavailable_on_failure():
    summary = {"mode": "LIVE", "attempts": [{"call_id": "c1", "success": True, "final_status": {"status": "completed", "answered_by": "human", "duration": 60}}],
               "voicemail_hit": True}
    note = format_call_note("Jane Doe", "robie-audit", "Robie audit", summary,
                            recording_status="failed")
    assert "not available" in note
    assert "upload failed" in note


def test_note_says_recording_unavailable_on_error_status():
    # Production passes status="error" (not "failed") — must also work.
    summary = {"mode": "LIVE", "attempts": [{"call_id": "c1", "success": True, "final_status": {"status": "completed", "answered_by": "human", "duration": 60}}],
               "voicemail_hit": True}
    note = format_call_note("Jane Doe", "robie-audit", "Robie audit", summary,
                            recording_status="error")
    assert "not available" in note


def test_note_says_recording_pending():
    summary = {"mode": "LIVE", "attempts": [{"call_id": "c1", "success": True, "final_status": {"status": "completed", "answered_by": "human", "duration": 60}}]}
    note = format_call_note("Jane Doe", "robie-audit", "Robie audit", summary,
                            recording_status="pending")
    assert "not yet available" in note


def test_note_says_recording_saved_when_ok():
    summary = {"mode": "LIVE", "attempts": [{"call_id": "c1", "success": True, "final_status": {"status": "completed", "answered_by": "human", "duration": 60}}],
               "recording_url": "saved to Documents"}
    note = format_call_note("Jane Doe", "robie-audit", "Robie audit", summary,
                            recording_status="success")
    assert "recording is saved" in note
    assert "unavailable" not in note


def test_note_no_recording_line_when_skipped():
    summary = {"mode": "LIVE", "attempts": [{"call_id": "c1", "success": True, "final_status": {"status": "completed", "answered_by": "human", "duration": 60}}]}
    note = format_call_note("Jane Doe", "robie-audit", "Robie audit", summary,
                            recording_status="skipped")
    assert "Recording" not in note
    assert "unavailable" not in note


# ----------------------------------------------------------------------
# 7. post_chat_alert never raises, handles missing webhook
# ----------------------------------------------------------------------
def test_post_chat_alert_no_webhook_no_raise():
    with patch.dict(os.environ, {}, clear=False):
        env = {k: v for k, v in os.environ.items()
               if k != "ROBIE_HEALTH_CHAT_WEBHOOK"}
        with patch.dict(os.environ, env, clear=True):
            assert post_chat_alert("test") is False  # no webhook -> False, no raise
