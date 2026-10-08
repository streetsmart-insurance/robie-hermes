"""Production hardening tests for the Bland dispatcher.

Covers the gaps found in the 2026-10-03 hardening audit:
- fail-closed live gate (kill switch / autodial flag) on the production path
- deterministic writeback uses the Zap's discussion_id (not most-recent)
- Bland call failure flips ok=False (was: ok=True if the note wrote)
- EZLynx retries (transient) + OAuth refresh on 401/403
- phone garbage filtering in extract_contact
- writeback falls back when the trigger discussion is gone
- webhook input validation (400s), note_body cap, idempotency
- Bland circuit breaker
- recording upload failure surfaced
"""
import sys
import os
import time
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from dispatcher import (
    dispatch, route_campaign, canonical_campaign_id, _writeback,
)
from ezlynx_client import EZLynxClient
from bland_client import BlandClient


# ----------------------------------------------------------------------
# Shared fakes
# ----------------------------------------------------------------------
APPLICANT = {
    "FirstName": "Jane", "LastName": "Doe",
    "CellPhone": "(555) 123-4567", "BusinessName": "",
}
DISCUSSIONS = [
    {"discussionId": "d1", "title": "Call about renewal",
     "lastModified": "2026-10-02T10:00:00", "applicantId": "123"},
    {"discussionId": "d2", "title": "Untitled",
     "lastModified": "2026-10-01T10:00:00", "applicantId": "123"},
]


class FakeEZ:
    def __init__(self, applicant=None, discussions=None):
        self._applicant = applicant if applicant is not None else APPLICANT
        self._discussions = discussions if discussions is not None else DISCUSSIONS
        self.appended = []

    def get_applicant(self, applicant_id):
        return {"status": "success", "applicant": self._applicant}

    def get_applicant_policies(self, applicant_id):
        return {"status": "success", "policies": []}

    def get_discussions(self, applicant_id):
        return self._discussions

    def append_note(self, discussion_id, body):
        self.appended.append(discussion_id)
        return {"status": "success", "data": {"noteId": "n1"}}

    def upload_document(self, applicant_id, document_name, file_bytes,
                        content_type="audio/mpeg"):
        return {"status": "success", "document_id": "doc-1"}


class FakeBlandOk:
    """Successful call double honoring the dry_run flag."""

    def __init__(self):
        self.calls = []

    def call_with_double_dial(self, phone, task, first_sentence,
                              voicemail_message, metadata=None, dry_run=False, halt_check=None):
        self.calls.append({"phone": phone, "dry_run": dry_run})
        if dry_run:
            return {"mode": "DRY_RUN", "attempts": [
                {"attempt": 1, "mode": "DRY_RUN"}]}
        return {"mode": "LIVE_BLAND_AI", "attempts": [
            {"attempt": 1, "call_id": "c1", "success": True,
             "mode": "LIVE_BLAND_AI", "final_status": {"status": "completed", "answered_by": "human", "duration": 60}}]}

    def get_call(self, call_id):
        return {}


class FakeBlandFail:
    def call_with_double_dial(self, phone, task, first_sentence,
                              voicemail_message, metadata=None, dry_run=False, halt_check=None):
        return {"mode": "LIVE_BLAND_AI", "attempts": [
            {"attempt": 1, "success": False, "error": "HTTP 500",
             "mode": "LIVE_BLAND_AI"}]}

    def get_call(self, call_id):
        return {}


# ----------------------------------------------------------------------
# 1. Fail-closed live gate (production default path only)
# ----------------------------------------------------------------------
def test_live_gate_forces_dry_run_when_autodial_unset():
    """DRY_RUN=0 but ROBIE_VOICE_AUTODIAL_LIVE unset -> no dial."""
    bland = FakeBlandOk()
    with patch.object(Config, "DRY_RUN", False), \
         patch.object(Config, "LIVE_AUTODIAL", False), \
         patch.object(Config, "KILL_SWITCH", False), \
         patch.object(Config, "BLAND_API_KEY", "k"):
        result = dispatch("123", "robie-unresponsive", "Robie unresponsive",
                          "", discussion_id="d1", dry_run=None,
                          ez=FakeEZ(), bland=bland)
    assert result["dry_run"] is True
    assert "live_blocked_reason" in result
    assert bland.calls and bland.calls[0]["dry_run"] is True


def test_live_gate_forces_dry_run_on_kill_switch():
    bland = FakeBlandOk()
    with patch.object(Config, "DRY_RUN", False), \
         patch.object(Config, "LIVE_AUTODIAL", True), \
         patch.object(Config, "KILL_SWITCH", True), \
         patch.object(Config, "BLAND_API_KEY", "k"):
        result = dispatch("123", "robie-unresponsive", "Robie unresponsive",
                          "", discussion_id="d1", dry_run=None,
                          ez=FakeEZ(), bland=bland)
    assert result["dry_run"] is True
    assert "kill switch" in result["live_blocked_reason"]


def test_live_gate_allows_when_fully_configured():
    bland = FakeBlandOk()
    with patch.object(Config, "DRY_RUN", False), \
         patch.object(Config, "LIVE_AUTODIAL", True), \
         patch.object(Config, "KILL_SWITCH", False), \
         patch.object(Config, "BLAND_API_KEY", "k"):
        result = dispatch("123", "robie-unresponsive", "Robie unresponsive",
                          "", discussion_id="d1", dry_run=None,
                          ez=FakeEZ(), bland=bland)
    assert result["dry_run"] is False
    assert "live_blocked_reason" not in result
    assert bland.calls[0]["dry_run"] is False


def test_explicit_dry_run_param_bypasses_gate():
    """The dry_run parameter is a test seam; the gate only guards the
    production default path (dry_run=None)."""
    bland = FakeBlandOk()
    with patch.object(Config, "DRY_RUN", False), \
         patch.object(Config, "LIVE_AUTODIAL", False), \
         patch.object(Config, "KILL_SWITCH", False), \
         patch.object(Config, "BLAND_API_KEY", "k"):
        result = dispatch("123", "robie-unresponsive", "Robie unresponsive",
                          "", discussion_id="d1", dry_run=False,
                          ez=FakeEZ(), bland=bland)
    assert result["dry_run"] is False


# ----------------------------------------------------------------------
# 2. Deterministic writeback targets the trigger discussion
# ----------------------------------------------------------------------
def test_deterministic_preserves_trigger_discussion_id():
    ez = FakeEZ()
    result = dispatch("123", "robie-unresponsive", "Robie unresponsive", "",
                      discussion_id="d1", dry_run=True,
                      ez=ez, bland=FakeBlandOk())
    assert result["trigger_discussion_id"] == "d1"


def test_deterministic_writeback_posts_to_trigger_discussion():
    ez = FakeEZ()
    result = dispatch("123", "robie-unresponsive", "Robie unresponsive", "",
                      discussion_id="d2", dry_run=False,
                      ez=ez, bland=FakeBlandOk())
    assert result["ok"] is True
    assert ez.appended == ["d2"], f"note went to {ez.appended}, want ['d2']"


# ----------------------------------------------------------------------
# 3. Bland call failure flips ok
# ----------------------------------------------------------------------
def test_call_failure_flips_ok_and_surfaces_error():
    result = dispatch("123", "robie-unresponsive", "Robie unresponsive", "",
                      discussion_id="d1", dry_run=False,
                      ez=FakeEZ(), bland=FakeBlandFail())
    assert result["ok"] is False
    assert "call_error" in result
    assert "500" in result["call_error"]


# ----------------------------------------------------------------------
# 4. EZLynx retries + OAuth refresh
# ----------------------------------------------------------------------
def _resp(status, payload=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else {}
    r.text = "err"
    return r


def test_append_note_retries_transient_then_succeeds():
    import ezlynx_client as ezm
    client = EZLynxClient()
    client._oauth_token = "tok"
    client._oauth_expires_at = time.time() + 3600
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            return _resp(500)
        return _resp(200, {"noteId": "n9"})

    with patch.object(ezm.requests, "post", side_effect=fake_post):
        with patch.object(ezm.time, "sleep", return_value=None):
            res = client.append_note("d1", "hello")
    assert res["status"] == "success"
    assert calls["n"] == 3


def test_append_note_gives_up_after_retries():
    import ezlynx_client as ezm
    client = EZLynxClient()
    client._oauth_token = "tok"
    client._oauth_expires_at = time.time() + 3600
    with patch.object(ezm.requests, "post", return_value=_resp(503)):
        with patch.object(ezm.time, "sleep", return_value=None):
            res = client.append_note("d1", "hello")
    assert res["status"] == "error"
    assert res["error"] == "request failed after retries"


def test_get_discussions_403_forces_token_refresh():
    import ezlynx_client as ezm
    client = EZLynxClient()
    client._oauth_token = "stale"
    client._oauth_expires_at = time.time() + 3600
    seen_force = []

    def fake_token(force=False):
        seen_force.append(force)
        client._oauth_token = "fresh" if force else "stale"
        client._oauth_expires_at = time.time() + 3600
        return client._oauth_token

    def fake_get(*a, **k):
        auth = k["headers"]["Authorization"]
        if "stale" in auth:
            return _resp(403)
        return _resp(200, [{"discussionId": "d1", "title": "t",
                           "lastModified": "2026-10-03"}])

    with patch.object(client, "_oauth_token_get", side_effect=fake_token):
        with patch.object(ezm.requests, "get", side_effect=fake_get):
            with patch.object(ezm.time, "sleep", return_value=None):
                discs = client.get_discussions("123")
    assert discs and discs[0]["discussionId"] == "d1"
    assert True in seen_force, "expected a forced token refresh after 403"


# ----------------------------------------------------------------------
# 5. Phone filtering
# ----------------------------------------------------------------------
def test_extract_contact_drops_garbage_phones():
    c = EZLynxClient.extract_contact({
        "FirstName": "J", "LastName": "D",
        "CellPhone": "N/A", "HomePhone": "0", "BusinessPhone": "555-1234",
    })
    assert c["phone"] == "555-1234"
    assert c["phones"] == ["555-1234"]


def test_extract_contact_no_usable_phone_fails_closed():
    c = EZLynxClient.extract_contact({"FirstName": "J", "CellPhone": "none"})
    assert c["phone"] == ""
    result = dispatch("123", "robie-unresponsive", "Robie unresponsive", "",
                      discussion_id="d1", dry_run=False,
                      ez=FakeEZ(applicant={"FirstName": "J", "CellPhone": "none"}),
                      bland=FakeBlandOk())
    assert result["ok"] is False
    assert result["error"] == "no phone number on applicant"


# ----------------------------------------------------------------------
# 6. Writeback fallback when trigger discussion is gone
# ----------------------------------------------------------------------
def test_writeback_falls_back_when_trigger_discussion_missing():
    ez = FakeEZ()  # discussions d1 (newer), d2
    bland = FakeBlandOk()
    out = _writeback(ez, "123", "robie-unresponsive", "Robie unresponsive",
                     "gone-discussion", "Jane Doe",
                     {"mode": "LIVE", "attempts": []}, bland)
    assert out["note"]["status"] == "success"
    assert ez.appended == ["d1"], f"expected fallback to most-recent d1, got {ez.appended}"


# ----------------------------------------------------------------------
# 7. Webhook input validation + idempotency
# ----------------------------------------------------------------------
def _post_webhook(data):
    from app import app
    with app.test_client() as c:
        return c.post("/webhook", data=data)


def test_webhook_rejects_non_numeric_applicant_id():
    r = _post_webhook({"applicant_id": "../../etc", "campaign_id": "robie-audit"})
    assert r.status_code == 400


def test_webhook_rejects_unknown_campaign():
    r = _post_webhook({"applicant_id": "123", "campaign_id": "robie-nonsense"})
    assert r.status_code == 400


def test_webhook_accepts_aliased_campaign():
    # Must not 400: the dispatcher canonicalizes Zap variants.
    assert route_campaign("robie-e-sign") == "deterministic"
    assert canonical_campaign_id("robie-renewal-reach-out") == "robie-renewal-reachout"


def test_webhook_duplicate_suppressed():
    import app as appmod
    with appmod._seen_lock:
        appmod._seen.clear()
    d1 = {"applicant_id": "999888", "campaign_id": "robie-audit",
          "discussion_id": "dup-disc"}
    r1 = _post_webhook(d1)
    assert r1.status_code == 200
    assert r1.get_json()["status"] == "received"
    r2 = _post_webhook(d1)
    assert r2.status_code == 202
    assert r2.get_json()["status"] == "duplicate_suppressed"
    # Different discussion is NOT a duplicate.
    d2 = dict(d1, discussion_id="other-disc")
    r3 = _post_webhook(d2)
    assert r3.get_json()["status"] == "received"


def test_note_body_capped():
    from app import _parse_payload, app as flask_app, NOTE_BODY_MAX_CHARS
    assert NOTE_BODY_MAX_CHARS == 2000
    big = "x" * (NOTE_BODY_MAX_CHARS + 500)
    with flask_app.test_request_context(
            "/webhook", method="POST",
            data={"applicant_id": "1", "campaign_id": "robie-audit",
                  "note_body": big}):
        p = _parse_payload()
        assert len(p["note_body"]) == NOTE_BODY_MAX_CHARS


# ----------------------------------------------------------------------
# 8. Bland circuit breaker
# ----------------------------------------------------------------------
def test_circuit_breaker_opens_after_consecutive_failures():
    import bland_client as bm
    BlandClient._reset_circuit_for_tests()
    client = BlandClient(api_key="k")

    def boom(*a, **k):
        raise bm.requests.exceptions.ConnectionError("down")

    with patch.object(bm.requests, "post", side_effect=boom):
        with patch.object(bm.time, "sleep", return_value=None):
            for _ in range(5):
                r = client.place_call("5551234567", "t", "f")
                assert r["success"] is False
            # 6th call short-circuits: requests.post must NOT be called.
            with patch.object(bm.requests, "post") as no_call:
                r = client.place_call("5551234567", "t", "f")
                assert r["success"] is False
                assert "circuit breaker" in r["error"]
                no_call.assert_not_called()
    BlandClient._reset_circuit_for_tests()


def test_circuit_breaker_resets_on_success():
    import bland_client as bm
    BlandClient._reset_circuit_for_tests()
    client = BlandClient(api_key="k")

    def fail_twice_then_ok(*a, **k):
        fail_twice_then_ok.n += 1
        if fail_twice_then_ok.n <= 2:
            raise bm.requests.exceptions.ConnectionError("down")
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {"call_id": "c1"}
        return r
    fail_twice_then_ok.n = 0

    with patch.object(bm.requests, "post", side_effect=fail_twice_then_ok):
        with patch.object(bm.time, "sleep", return_value=None):
            r = client.place_call("5551234567", "t", "f")
            assert r["success"] is True
    with BlandClient._cb_lock:
        assert BlandClient._cb_failures == 0
    BlandClient._reset_circuit_for_tests()


def test_place_call_retries_transient_500():
    import bland_client as bm
    BlandClient._reset_circuit_for_tests()
    client = BlandClient(api_key="k")
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            r = MagicMock()
            r.status_code = 500
            r.json.return_value = {"message": "boom"}
            return r
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {"call_id": "c9"}
        return r

    with patch.object(bm.requests, "post", side_effect=flaky):
        with patch.object(bm.time, "sleep", return_value=None):
            r = client.place_call("5551234567", "t", "f")
    assert r["success"] is True and r["call_id"] == "c9"
    assert calls["n"] == 3
    BlandClient._reset_circuit_for_tests()


# ----------------------------------------------------------------------
# 9. Recording failure surfaced
# ----------------------------------------------------------------------
def test_recording_error_surfaced_but_note_ok_keeps_ok_true():
    class FakeEZRecFail(FakeEZ):
        def upload_document(self, *a, **k):
            return {"status": "error", "error": "DocumentApi 500"}

    class BlandWithRecording:
        def call_with_double_dial(self, *a, **k):
            return {"mode": "LIVE_BLAND_AI", "attempts": [
                {"attempt": 1, "call_id": "c1", "success": True, "final_status": {"status": "completed", "answered_by": "human", "duration": 60}}]}

        def get_call(self, call_id):
            return {"recording_url": "https://x/y.mp3"}

    import dispatcher as dm
    fake_mp3 = b"ID3" + b"\x00" * 2000  # realistic: ID3 header, >1KB
    with patch.object(dm, "_download_bytes", return_value=fake_mp3):
        result = dispatch("123", "robie-unresponsive", "Robie unresponsive",
                          "", discussion_id="d1", dry_run=False,
                          ez=FakeEZRecFail(), bland=BlandWithRecording())
    assert result["ok"] is True  # note wrote fine
    assert "recording_error" in result
