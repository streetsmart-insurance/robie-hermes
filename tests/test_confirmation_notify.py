"""Tests for confirmation_notify.py (Chat + email fan-out, idempotent)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robie_job_engine import confirmation_notify as notify
from robie_job_engine import confirmations
from robie_job_engine.store import JobStore


@pytest.fixture()
def db(tmp_path):
    return str(tmp_path / "jobs.db")


def _request(store, **kw):
    args = dict(
        loop_job_id="loop-1",
        job_type="policy_change",
        draft_summary="Raise written premium",
        changes_json={"policy_number": "HO-1", "changes": {"writtenPremium": "2450"}},
        requested_by="robie",
    )
    args.update(kw)
    return confirmations.request_confirmation(store=store, **args)


class FakeChat:
    def __init__(self):
        self.posts: list[str] = []

    def __call__(self, text):
        self.posts.append(text)
        return {"space": "spaces/DM", "message_name": "spaces/DM/messages/1"}


class FakeGmail:
    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []

    def __call__(self, to, subject, body):
        self.sent.append((to, subject, body))
        return {"message_id": "msg-1", "to": to}


def test_notify_sends_chat_and_email(db):
    store = JobStore(db)
    cid = _request(store)
    chat, gmail = FakeChat(), FakeGmail()
    result = notify.notify_requested(store, cid, chat_poster=chat, gmail_sender=gmail)
    assert len(result["notified"]) == 2
    assert result["errors"] == []
    assert len(chat.posts) == 1
    assert "needs your approval" in chat.posts[0]
    assert "Confirmations" in chat.posts[0]
    assert len(gmail.sent) == 1
    to, subject, body = gmail.sent[0]
    assert to == "carlo@streetsmart.insurance"
    assert "[ROBIE] Approval needed" in subject
    assert "Nothing moves until you decide" in body


TEST_KEY = "test-decision-signing-key-0123456789abcdef"


def _tokens_in(text):
    return [line.strip() for line in text.splitlines()
            if line.strip().startswith(confirmations.DECISION_TOKEN_PREFIX)]


def test_notify_delivers_signed_decision_tokens(db):
    """With a signing key, the Chat DM and email carry APPROVE/REJECT tokens
    minted for the approver inbox -- the magic-link delivery leg."""
    store = JobStore(db)
    cid = _request(store)
    chat, gmail = FakeChat(), FakeGmail()
    result = notify.notify_requested(
        store, cid, chat_poster=chat, gmail_sender=gmail, decision_key=TEST_KEY
    )
    assert result["errors"] == []

    chat_tokens = _tokens_in(chat.posts[0])
    assert len(chat_tokens) == 2  # APPROVE + REJECT
    decisions = set()
    for token in chat_tokens:
        verified = confirmations.verify_decision_token(token, key=TEST_KEY)
        assert verified["confirmation_id"] == cid
        assert verified["principal"] == notify.approver_principal()
        decisions.add(verified["decision"])
    assert decisions == {"APPROVE", "REJECT"}

    _, _, body = gmail.sent[0]
    assert len(_tokens_in(body)) == 2
    assert "display-only" not in chat.posts[0]


def test_delivered_token_applies_on_the_board(db):
    """End to end: token delivered in the Chat DM, pasted on the sheet, is
    the decision that lands. A bare typed word on the same run is not."""
    from robie_job_engine import confirmation_board

    store = JobStore(db)
    cid = _request(store)
    chat = FakeChat()
    notify.notify_requested(store, cid, chat_poster=chat, decision_key=TEST_KEY)
    approve_token = [t for t in _tokens_in(chat.posts[0])
                     if confirmations.verify_decision_token(t, key=TEST_KEY)["decision"] == "APPROVE"][0]

    values, sheets = _board_apis()
    confirmation_board.sync_confirmations(db, _board_sheet_id(values), values_api=values, sheets_api=sheets)
    rows = values.cells[(_board_sheet_id(values), "Confirmations")]
    rows[1][confirmation_board.DECISION] = approve_token
    result = confirmation_board.sync_confirmations(
        db, _board_sheet_id(values), values_api=values, sheets_api=sheets,
        decision_key=TEST_KEY,
    )
    assert result["decisions_applied"] == 1
    assert confirmations.get(cid, store=store)["status"] == "APPROVED"
    assert result["applied"][0]["decided_by"] == notify.approver_principal()


class _FakeExecutable:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeValuesApi:
    """Minimal in-memory stand-in for the Sheets values API."""

    def __init__(self):
        self.cells: dict[tuple[str, str], list[list[str]]] = {}
        self.titles: set[str] = set()

    def get(self, spreadsheetId, range):  # noqa: N803
        tab = range.split("!", 1)[0]
        return _FakeExecutable({"values": [list(r) for r in self.cells.get((spreadsheetId, tab), [])]})

    def update(self, spreadsheetId, range, valueInputOption, body):  # noqa: N803
        tab = range.split("!", 1)[0]
        self.titles.add(tab)
        self.cells[(spreadsheetId, tab)] = [list(r) for r in body["values"]]
        return _FakeExecutable({"updatedRange": range})

    def clear(self, spreadsheetId, range):  # noqa: N803
        tab = range.split("!", 1)[0]
        self.cells[(spreadsheetId, tab)] = []
        return _FakeExecutable({})


class _FakeSheetsApi:
    def __init__(self, values):
        self.values = values

    def get(self, spreadsheetId, fields):  # noqa: N803
        return _FakeExecutable({
            "sheets": [{"properties": {"title": t}} for t in sorted(self.values.titles)]
        })

    def batchUpdate(self, spreadsheetId, body):  # noqa: N803
        for req in body.get("requests", []):
            title = req.get("addSheet", {}).get("properties", {}).get("title")
            if title:
                self.values.titles.add(title)
        return _FakeExecutable({})


def _board_apis():
    values = _FakeValuesApi()
    return values, _FakeSheetsApi(values)


def _board_sheet_id(values):
    return "notify-board-sheet"


def test_notify_without_key_stays_display_only(db, monkeypatch):
    """No key: the message must say the tab is display-only, not imply that
    typing APPROVE on the sheet works."""
    monkeypatch.delenv(confirmations.DECISION_TOKEN_ENV, raising=False)
    store = JobStore(db)
    cid = _request(store)
    chat, gmail = FakeChat(), FakeGmail()
    result = notify.notify_requested(store, cid, chat_poster=chat, gmail_sender=gmail)
    assert result["errors"] == []
    assert len(result["notified"]) == 2
    assert "display-only" in chat.posts[0]
    assert "display-only" in gmail.sent[0][2]
    assert _tokens_in(chat.posts[0]) == []
    assert _tokens_in(gmail.sent[0][2]) == []


def test_notify_misconfigured_key_degrades_without_crashing(db):
    """A <16-byte key must not kill the notification fan-out: warn, send
    without tokens, keep both channels alive."""
    store = JobStore(db)
    cid = _request(store)
    chat, gmail = FakeChat(), FakeGmail()
    result = notify.notify_requested(
        store, cid, chat_poster=chat, gmail_sender=gmail, decision_key="short"
    )
    assert result["errors"] == []
    assert len(result["notified"]) == 2
    assert _tokens_in(chat.posts[0]) == []
    assert "display-only" in chat.posts[0]


def test_notify_is_idempotent(db):
    store = JobStore(db)
    cid = _request(store)
    chat, gmail = FakeChat(), FakeGmail()
    notify.notify_requested(store, cid, chat_poster=chat, gmail_sender=gmail)
    result = notify.notify_requested(store, cid, chat_poster=chat, gmail_sender=gmail)
    assert result["notified"] == []
    assert result["errors"] == []
    assert len(chat.posts) == 1
    assert len(gmail.sent) == 1


def test_notify_skips_non_pending(db):
    store = JobStore(db)
    cid = _request(store)
    confirmations.approve(cid, "Carlo Ferrara", store=store)
    chat, gmail = FakeChat(), FakeGmail()
    result = notify.notify_requested(store, cid, chat_poster=chat, gmail_sender=gmail)
    assert result["skipped"] == "not PENDING"
    assert chat.posts == [] and gmail.sent == []


def test_one_channel_failure_does_not_block_the_other(db):
    store = JobStore(db)
    cid = _request(store)

    def bad_chat(text):
        raise RuntimeError("chat down")

    gmail = FakeGmail()
    result = notify.notify_requested(store, cid, chat_poster=bad_chat, gmail_sender=gmail)
    assert len(result["errors"]) == 1
    assert result["errors"][0]["channel"] == "google_chat"
    assert len(gmail.sent) == 1
    # Failed channel retries next time; the good one does not resend.
    chat2 = FakeChat()
    result2 = notify.notify_requested(store, cid, chat_poster=chat2, gmail_sender=gmail)
    assert len(chat2.posts) == 1
    assert len(gmail.sent) == 1
    assert result2["errors"] == []


def test_unknown_confirmation_raises(db):
    store = JobStore(db)
    with pytest.raises(ValueError):
        notify.notify_requested(store, "nope", chat_poster=FakeChat(), gmail_sender=FakeGmail())


class FakeZap:
    def __init__(self):
        self.payloads: list[dict] = []

    def __call__(self, payload):
        self.payloads.append(dict(payload))
        return {"zap_output": "ok"}


def test_assign_requester_task_fires_zap_with_login_username(db):
    store = JobStore(db)
    cid = _request(store, requested_by="Carlo Ferrara")
    zap = FakeZap()
    result = notify.assign_requester_task(
        store, cid, applicant_id="220250093", zap_trigger=zap
    )
    assert result["assignee"] == "Carlo1"
    assert len(zap.payloads) == 1
    payload = zap.payloads[0]
    assert payload["applicant_id"] == "220250093"
    assert payload["assignee"] == "Carlo1"
    assert payload["due_date"]  # Carlo's rule: always a due date
    assert "Confirmations tab" in payload["task_description"]


def test_assign_requester_task_is_idempotent(db):
    store = JobStore(db)
    cid = _request(store, requested_by="Karla Brown")
    zap = FakeZap()
    notify.assign_requester_task(store, cid, applicant_id="220250093", zap_trigger=zap)
    result = notify.assign_requester_task(
        store, cid, applicant_id="220250093", zap_trigger=zap
    )
    assert result["skipped"] == "already assigned"
    assert len(zap.payloads) == 1


def test_assign_requester_task_fails_closed_on_unknown_requester(db):
    store = JobStore(db)
    cid = _request(store, requested_by="Some Stranger")
    with pytest.raises(ValueError, match="no known EZLynx login username"):
        notify.assign_requester_task(
            store, cid, applicant_id="220250093", zap_trigger=FakeZap()
        )


def test_assign_requester_task_env_override_adds_a_login(db, monkeypatch):
    store = JobStore(db)
    cid = _request(store, requested_by="Jake Ferrara")
    monkeypatch.setenv("ROBIE_EZLYNX_LOGIN_JAKE_FERRARA", "JakeSS")
    zap = FakeZap()
    result = notify.assign_requester_task(
        store, cid, applicant_id="220250093", zap_trigger=zap
    )
    assert result["assignee"] == "JakeSS"


def test_assign_requester_task_requires_applicant_id(db):
    store = JobStore(db)
    cid = _request(store, requested_by="Carlo Ferrara")
    with pytest.raises(ValueError, match="applicant_id is required"):
        notify.assign_requester_task(store, cid, applicant_id="", zap_trigger=FakeZap())


def test_assign_requester_task_skips_decided(db):
    store = JobStore(db)
    cid = _request(store, requested_by="Carlo Ferrara")
    confirmations.approve(cid, "Carlo Ferrara", store=store)
    zap = FakeZap()
    result = notify.assign_requester_task(
        store, cid, applicant_id="220250093", zap_trigger=zap
    )
    assert result["skipped"] == "not PENDING"
    assert zap.payloads == []


class FakeThreadPoster:
    def __init__(self):
        self.posts: list[tuple[str, str, str | None]] = []

    def __call__(self, space, text, thread=None):
        self.posts.append((space, text, thread))
        return {"space": space, "message_name": "spaces/X/messages/2"}


def test_origin_chat_posts_back_to_originating_thread(db):
    store = JobStore(db)
    cid = _request(
        store,
        requested_by="Jake Ferrara",
        origin_platform="chat",
        origin_ref={"space": "spaces/AAA", "thread": "spaces/AAA/threads/BBB"},
    )
    chat, gmail, thread_poster = FakeChat(), FakeGmail(), FakeThreadPoster()
    result = notify.notify_requested(
        store, cid, chat_poster=chat, gmail_sender=gmail,
        chat_thread_poster=thread_poster, zap_trigger=FakeZap(),
    )
    # Carlo still gets his two; the requester gets the thread post.
    assert len(thread_poster.posts) == 1
    space, text, thread = thread_poster.posts[0]
    assert space == "spaces/AAA"
    assert thread == "spaces/AAA/threads/BBB"
    assert "waiting on a human decision" in text
    channels = [n["channel"] for n in result["notified"]]
    assert "requester_chat" in channels


def test_origin_email_goes_to_requester_address(db):
    store = JobStore(db)
    cid = _request(
        store,
        requested_by="Jake Ferrara",
        origin_platform="email",
        origin_ref={"to": "jake@streetsmart.insurance"},
    )
    gmail = FakeGmail()
    result = notify.notify_requested(
        store, cid, chat_poster=FakeChat(), gmail_sender=gmail,
        zap_trigger=FakeZap(),
    )
    to_addresses = [to for to, _, _ in gmail.sent]
    assert "jake@streetsmart.insurance" in to_addresses
    assert "carlo@streetsmart.insurance" in to_addresses
    channels = [n["channel"] for n in result["notified"]]
    assert "requester_email" in channels


def test_origin_ezlynx_assigns_task_to_requester(db):
    store = JobStore(db)
    cid = _request(
        store,
        requested_by="Karla Brown",
        origin_platform="ezlynx",
        origin_ref={"applicant_id": "220250093"},
    )
    zap = FakeZap()
    result = notify.notify_requested(
        store, cid, chat_poster=FakeChat(), gmail_sender=FakeGmail(),
        zap_trigger=zap,
    )
    assert len(zap.payloads) == 1
    assert zap.payloads[0]["assignee"] == "KarlaSS"
    channels = [n["channel"] for n in result["notified"]]
    assert "requester_ezlynx" in channels


def test_origin_routing_is_idempotent(db):
    store = JobStore(db)
    cid = _request(
        store,
        requested_by="Jake Ferrara",
        origin_platform="chat",
        origin_ref={"space": "spaces/AAA"},
    )
    thread_poster = FakeThreadPoster()
    notify.notify_requested(
        store, cid, chat_poster=FakeChat(), gmail_sender=FakeGmail(),
        chat_thread_poster=thread_poster, zap_trigger=FakeZap(),
    )
    result = notify.notify_requested(
        store, cid, chat_poster=FakeChat(), gmail_sender=FakeGmail(),
        chat_thread_poster=thread_poster, zap_trigger=FakeZap(),
    )
    assert len(thread_poster.posts) == 1
    assert result["notified"] == []
    assert result["errors"] == []


def test_no_origin_platform_skips_requester_routing(db):
    store = JobStore(db)
    cid = _request(store)
    chat, gmail = FakeChat(), FakeGmail()
    result = notify.notify_requested(store, cid, chat_poster=chat, gmail_sender=gmail)
    channels = [n["channel"] for n in result["notified"]]
    assert channels == ["google_chat", "email"]


def test_bad_origin_platform_rejected(db):
    store = JobStore(db)
    with pytest.raises(ValueError, match="origin_platform must be"):
        _request(store, origin_platform="smoke-signal")


def test_old_rows_without_origin_columns_still_read(db):
    # A database created before origin_platform/origin_ref existed must
    # migrate in place and keep working.
    import sqlite3

    store = JobStore(db)
    cid = _request(store)
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE plan_confirmations DROP COLUMN origin_platform")
    conn.execute("ALTER TABLE plan_confirmations DROP COLUMN origin_ref")
    conn.commit()
    conn.close()
    record = confirmations.get(cid, store=store)
    assert record["id"] == cid
    assert record.get("origin_platform") is None
    result = notify.notify_requested(
        store, cid, chat_poster=FakeChat(), gmail_sender=FakeGmail()
    )
    channels = [n["channel"] for n in result["notified"]]
    assert channels == ["google_chat", "email"]


class TestRequesterLoginDirectory:
    """The EZLynx login directory (Agency Admin > Manage Users, 2026-09-22).

    The Zap's Task Assignee field needs the login username; a display
    name like "Accounting Team" is rejected by EZLynx, so every entry
    must be a real User Name from the directory.
    """

    def test_full_directory_resolves(self):
        expected = {
            "zeus quezada": "Zeus12",
            "taylor cimei": "TCimei",
            "steffany canales": "SCanales",
            "sandy santana": "Sandy11",
            "sandeep yadav": "Sandeep11",
            "robie ai": "SSRobie",
            "ricardo aguilar": "Ricardo2",
            "nicole segovia": "SSNicole",
            "nelson maldonado": "Nmaldonado2",
            "mitchell slagle": "Mitch1",
            "mike sosa": "MikeS1",
            "matthew mancina": "Mancina1",
            "maria bara": "MariaB12",
            "lenin perdomo": "Lperdomo1",
            "karla brown": "KarlaSS",
            "jose cabrera": "Josecabrera",
            "jimmy ferrara": "Jimmy1",
            "jazmin molina": "Jazmin11",
            "jake ferrara": "jferrara3",
            "jackie arriola": "Jackie_Arriola",
            "gabriela chutin": "Gabrielac1",
            "eunice iraheta": "Eunice",
            "erika palacios": "Erika11",
            "eimy ramos": "Eramos1",
            "diana cabrera": "Diana12",
            "daniela aguilar": "Daniela_Aguilar",
            "carlo ferrara": "Carlo1",
            "ashley huntley": "ahuntley",
            "angie valladarez": "AngieV",
            "andrea illanes": "a_illanes",
            "andrea martinez": "Amartinez21",
            "ana flores": "anaflores",
            "amber voigt": "Amber14",
            "alejandro zelaya": "Alejandro11",
            "accounting team": "Markley1",
        }
        for name, login in expected.items():
            assert notify.requester_login(name) == login, name
        # Bare first names resolve where unambiguous.
        for first, login in [
            ("zeus", "Zeus12"), ("taylor", "TCimei"), ("steffany", "SCanales"),
            ("sandy", "Sandy11"), ("sandeep", "Sandeep11"), ("robie", "SSRobie"),
            ("ricardo", "Ricardo2"), ("nicole", "SSNicole"),
            ("nelson", "Nmaldonado2"), ("mitchell", "Mitch1"),
            ("mike", "MikeS1"), ("matthew", "Mancina1"),
            ("maria", "MariaB12"), ("markley", "Markley1"),
            ("karla", "KarlaSS"), ("lenin", "Lperdomo1"),
            ("jose", "Josecabrera"), ("jimmy", "Jimmy1"),
            ("jazmin", "Jazmin11"), ("jake", "jferrara3"),
            ("jackie", "Jackie_Arriola"), ("gabriela", "Gabrielac1"),
            ("eunice", "Eunice"), ("erika", "Erika11"), ("eimy", "Eramos1"),
            ("diana", "Diana12"), ("daniela", "Daniela_Aguilar"),
            ("carlo", "Carlo1"), ("ashley", "ahuntley"), ("angie", "AngieV"),
            ("ana", "anaflores"), ("amber", "Amber14"),
            ("alejandro", "Alejandro11"),
        ]:
            assert notify.requester_login(first) == login, first

    def test_markley_is_login_not_display_name(self):
        # Regression: "Accounting Team" is the display name and EZLynx
        # rejects it as a task assignee; the login is Markley1.
        assert notify.requester_login("markley") == "Markley1"
        assert notify.requester_login("accounting team") == "Markley1"

    def test_ambiguous_first_name_fails_closed(self):
        assert notify.requester_login("andrea") is None

    def test_unknown_requester_fails_closed(self):
        assert notify.requester_login("somebody nobody") is None

    def test_env_override_wins(self, monkeypatch):
        # The env key is built from the full normalized name.
        monkeypatch.setenv("ROBIE_EZLYNX_LOGIN_JAKE_FERRARA", "JakeCustom")
        assert notify.requester_login("jake ferrara") == "JakeCustom"
        assert notify.requester_login("jake") == "jferrara3"
