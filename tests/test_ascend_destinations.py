"""Live destination ports, mapping, pagination, and the Test runner."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace
from typing import Any
from urllib import error

import pytest

from robie_job_engine import ascend_destination_mapping as mapping
from robie_job_engine import ezlynx_task_api as tapi
from robie_job_engine import ezlynx_write_scope
from robie_job_engine.ascend_delivery_state import DurableLedger, ReliableDelivery, paginate
from robie_job_engine.ascend_destinations import (
    EZLynxAscendDestination,
    QBODepositDestination,
    marker,
    parse_marker,
)

BUSTER = "26356199"
APP = {
    "client_id": "cid", "client_secret": "csec", "scope": "DiscussionApi openid",
    "token_endpoint": "https://app.ezlynx.com/auth/connect/token",
    "integration_group_id": "159", "discussion_base": "https://app.ezlynx.com/DiscussionApi",
}


class _Resp:
    def __init__(self, status, body):
        self.status = status
        self._body = body if isinstance(body, str) else json.dumps(body)

    def read(self):
        return self._body.encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeEZLynx:
    """Stateful Discussion API: discussions per applicant, notes per discussion."""

    def __init__(self):
        self.discussions: dict[str, dict[str, Any]] = {}
        self.next_id = 100
        self.posts = 0
        self.fail_task_post: Exception | None = None

    def _new_id(self):
        self.next_id += 1
        return self.next_id

    def add_discussion(self, applicant, title):
        did = str(self._new_id())
        self.discussions[did] = {"applicant": applicant, "title": title, "notes": []}
        return did

    def _store_note(self, did, note):
        stored = dict(note, noteId=self._new_id(), discussionId=int(did))
        if stored.get("type") == "TaskCreationNote":
            stored["task"] = dict(stored["task"], taskId=self._new_id())
        self.discussions[did]["notes"].append(stored)
        return stored["noteId"]

    def __call__(self, req, timeout):
        url, method = req.full_url, req.get_method()
        body = json.loads(req.data) if req.data and not url.endswith("/token") else None
        if url.endswith("/connect/token"):
            return _Resp(200, {"access_token": "tok", "expires_in": 3600})
        if "/by-applicant" in url:
            applicant = url.split("applicantId=")[1]
            return _Resp(200, [{"discussionId": int(d), "title": v["title"]}
                               for d, v in self.discussions.items() if v["applicant"] == applicant])
        if url.endswith("/with-note") and method == "POST":
            self.posts += 1
            did = self.add_discussion(str(body["applicantId"]), body["discussion"]["title"])
            self._store_note(did, body["note"])
            return _Resp(200, int(did))
        if url.endswith("/with-notes"):
            did = url.split("/v8/discussions/")[1].split("/")[0]
            return _Resp(200, {"notes": self.discussions[did]["notes"]})
        if url.endswith("/notes") and method == "POST":
            self.posts += 1
            did = url.split("/v8/discussions/")[1].split("/")[0]
            if body.get("type") == "TaskCreationNote" and self.fail_task_post is not None:
                raise self.fail_task_post
            return _Resp(201, {"noteId": self._store_note(did, body)})
        if url.endswith("/v8/notes/query"):
            wanted = {str(i) for i in body["noteIds"]}
            return _Resp(200, [n for v in self.discussions.values() for n in v["notes"]
                               if str(n["noteId"]) in wanted])
        if "/v8/discussions/" in url and method == "GET":
            did = url.rsplit("/", 1)[1]
            v = self.discussions[did]
            return _Resp(200, {"discussionId": int(did), "title": v["title"],
                               "applicantId": v["applicant"], "noteCount": len(v["notes"])})
        raise AssertionError(f"unexpected {method} {url}")


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    tapi.clear_runtime_caches()
    monkeypatch.setenv(tapi.STATE_DIR_ENV, str(tmp_path / "state"))
    monkeypatch.setenv(tapi.ACT_AS_ENV, "carlo1")
    monkeypatch.delenv(tapi.DIRECT_TASK_API_ENV, raising=False)
    monkeypatch.setattr(tapi, "load_app_config", lambda accessor=None: (dict(APP), None))
    allowed = {BUSTER}

    def gate(value):
        if str(value) not in allowed:
            raise ezlynx_write_scope.EzlynxWriteScopeError("not on allowlist")
        return str(value)

    monkeypatch.setattr(ezlynx_write_scope, "require_allowed_ezlynx_write_applicant", gate)
    yield
    tapi.clear_runtime_caches()


def cancellation(**kw):
    event = {"key": "cr_1", "kind": "cancellation", "applicant_id": BUSTER,
             "assignee": "SSRobie", "assignee_user_id": 438318, "title": "Ascend cancellation",
             "note_text": "Cancellation return", "task_text": "Call the insured",
             "due_date": "2026-10-06"}
    event.update(kw)
    return event


def deliver(fake, event, ledger):
    return ReliableDelivery(ledger, EZLynxAscendDestination(urlopen=fake)).process(event)


# --- EZLynx port ----------------------------------------------------------------


def test_marker_round_trip():
    assert parse_marker("x " + marker("signed_ab-12", "task_id")) == ("signed_ab-12", "task_id")
    assert parse_marker("no marker") == ("", "")


def test_ezlynx_note_and_task_delivered_with_readback(tmp_path):
    fake = FakeEZLynx()
    ledger = DurableLedger(str(tmp_path / "l.db"))
    assert deliver(fake, cancellation(), ledger) == "delivered_readback"
    state = ledger["cr_1"]
    assert state.delivered and set(state.destination_ids) == {"note_id", "task_id"}
    (did, disc), = fake.discussions.items()
    assert disc["title"] == "Tasks by Robie" and disc["applicant"] == BUSTER
    types = [n["type"] for n in disc["notes"]]
    assert types == ["Note", "TaskCreationNote"]
    assert disc["notes"][1]["task"]["assignedUserId"] == 438318


def test_second_run_with_a_fresh_ledger_finds_writes_by_marker_and_sends_nothing(tmp_path):
    fake = FakeEZLynx()
    deliver(fake, cancellation(), DurableLedger(str(tmp_path / "a.db")))
    posts = fake.posts
    assert deliver(fake, cancellation(), DurableLedger(str(tmp_path / "b.db"))) == "delivered_readback"
    assert fake.posts == posts


def test_task_refused_before_send_is_retryable_not_complete(tmp_path):
    # #759's contract on the new path: a task that was not created leaves the
    # event undelivered and retryable; nothing reports success.
    fake = FakeEZLynx()
    ledger = DurableLedger(str(tmp_path / "l.db"))
    event = cancellation(assignee="SCanales", assignee_user_id=None)
    assert deliver(fake, event, ledger) == "not_sent_retryable"
    state = ledger["cr_1"]
    assert not state.delivered
    assert "task_id" not in state.attempted_components  # nothing left: retry is safe
    assert set(state.destination_ids) == {"note_id"}  # the note landed and read back
    # Once the assignee resolves, the next run sends only the task.
    assert deliver(fake, cancellation(), ledger) == "delivered_readback"


def test_task_post_timeout_is_recovery_only_never_resent(tmp_path):
    fake = FakeEZLynx()
    fake.fail_task_post = TimeoutError()
    ledger = DurableLedger(str(tmp_path / "l.db"))
    assert deliver(fake, cancellation(), ledger) == "destination_unverified"
    assert not ledger["cr_1"].delivered
    fake.fail_task_post = None
    posts = fake.posts
    assert deliver(fake, cancellation(), ledger) == "attempt_uncertain_recovery_only"
    assert fake.posts == posts


def test_non_allowlisted_applicant_is_not_sent(tmp_path):
    fake = FakeEZLynx()
    ledger = DurableLedger(str(tmp_path / "l.db"))
    assert deliver(fake, cancellation(applicant_id="999"), ledger) == "not_sent_retryable"
    assert fake.posts == 0


def test_marker_for_another_key_is_not_a_receipt(tmp_path):
    fake = FakeEZLynx()
    did = fake.add_discussion(BUSTER, "Tasks by Robie")
    fake._store_note(did, {"type": "Note", "body": "old " + marker("cr_OTHER", "note_id")})
    ledger = DurableLedger(str(tmp_path / "l.db"))
    assert deliver(fake, cancellation(), ledger) == "delivered_readback"
    assert len(fake.discussions[did]["notes"]) == 3


# --- QBO port -------------------------------------------------------------------


class FakeQBO:
    def __init__(self, *, production=False, realm="r1", amount_override=None):
        self.config = SimpleNamespace(realm_id=realm, is_production=production)
        self.deposits: dict[str, dict] = {}
        self.posts = 0
        self.amount_override = amount_override

    def _request(self, method, resource, json_body=None, query_params=None):
        if method == "GET" and resource == "query":
            return {"QueryResponse": {"Deposit": list(self.deposits.values())}}
        if method == "POST" and resource == "deposit":
            self.posts += 1
            dep = dict(json_body, Id=str(500 + self.posts),
                       TotalAmt=self.amount_override or json_body["Line"][0]["Amount"])
            self.deposits[dep["Id"]] = dep
            return {"Deposit": dep}
        if method == "GET" and resource.startswith("deposit/"):
            return {"Deposit": self.deposits[resource.split("/")[1]]}
        raise AssertionError((method, resource))


def payout(**kw):
    event = {"key": "payout_p1", "kind": "commission_payout", "source_id": "p1",
             "realm_id": "r1", "account_id": "35", "income_account_id": "79",
             "payee_type": "Vendor", "payee_id": "12", "amount_cents": 12345, "currency": "USD",
             "txn_date": "2026-10-03"}
    event.update(kw)
    return event


def test_qbo_deposit_delivered_with_exact_readback(tmp_path):
    qb = FakeQBO()
    ledger = DurableLedger(str(tmp_path / "l.db"))
    assert ReliableDelivery(ledger, QBODepositDestination(qb)).process(payout()) == "delivered_readback"
    dep = next(iter(qb.deposits.values()))
    assert dep["DepositToAccountRef"] == {"value": "35"}
    line = dep["Line"][0]
    assert line["Amount"] == 123.45
    assert line["DepositLineDetail"] == {"AccountRef": {"value": "79"},
                                         "Entity": {"value": "12", "type": "Vendor"}}
    assert marker("payout_p1", "deposit_id") in dep["PrivateNote"]


def test_qbo_wrong_amount_on_readback_is_not_complete(tmp_path):
    qb = FakeQBO(amount_override=99.99)
    ledger = DurableLedger(str(tmp_path / "l.db"))
    assert ReliableDelivery(ledger, QBODepositDestination(qb)).process(payout()) == "destination_unverified"
    assert not ledger["payout_p1"].delivered


def test_qbo_production_company_is_refused_before_any_write(tmp_path, monkeypatch):
    monkeypatch.delenv("ROBIE_ASCEND_QBO_PRODUCTION_WRITES", raising=False)
    qb = FakeQBO(production=True)
    ledger = DurableLedger(str(tmp_path / "l.db"))
    assert ReliableDelivery(ledger, QBODepositDestination(qb)).process(payout()) == "not_sent_retryable"
    assert qb.posts == 0


def test_qbo_existing_marked_deposit_is_found_not_resent(tmp_path):
    qb = FakeQBO()
    ReliableDelivery(DurableLedger(str(tmp_path / "a.db")), QBODepositDestination(qb)).process(payout())
    assert ReliableDelivery(DurableLedger(str(tmp_path / "b.db")), QBODepositDestination(qb)).process(payout()) == "delivered_readback"
    assert qb.posts == 1


# --- mapping --------------------------------------------------------------------


class PolicyClient:
    def __init__(self, rows):
        self.rows = rows

    def search_policy_by_number(self, number):
        return {"data": self.rows}


def test_map_applicant_requires_one_applicant_and_confirmed_csr_id():
    # CSR comes from the Ascend program producer (PolicyApi rows carry no CSR
    # field) — the same resolve_cancellation_csr rule as the notice driver.
    program = {"producer": {"email": "karla@streetsmart.insurance",
                            "first_name": "Karla", "last_name": "Brown"}}
    ev = {"kind": "cancellation", "policy_number": "HO-1", "program": program}
    ok, why = mapping.map_applicant(ev, PolicyClient([
        {"PolicyNumber": "HO-1", "ApplicantId": BUSTER}]))
    assert why == "" and ok == {"applicant_id": BUSTER, "assignee": "KarlaSS",
                                "assignee_user_id": 356806}
    _, why = mapping.map_applicant(ev, PolicyClient([
        {"PolicyNumber": "HO-1", "ApplicantId": "1"},
        {"PolicyNumber": "HO-1", "ApplicantId": "2"}]))
    assert why == "unmatched_policy_ambiguous"
    unconfirmed = {"producer": {"email": "steffany@streetsmart.insurance",
                                "first_name": "Steffany", "last_name": "Canales"}}
    _, why = mapping.map_applicant(
        {"kind": "cancellation", "policy_number": "HO-1", "program": unconfirmed},
        PolicyClient([{"PolicyNumber": "HO-1", "ApplicantId": "1"}]))
    assert why == "unmatched_assignee_id_unconfirmed"
    _, why = mapping.map_applicant({"kind": "cancellation", "insured_name": "Buster"},
                                   PolicyClient([]))
    assert why == "unmatched_no_policy_number"
    _, why = mapping.map_applicant(
        {"kind": "cancellation", "policy_number": "HO-1"},
        PolicyClient([{"PolicyNumber": "HO-1", "ApplicantId": BUSTER}]))
    assert why == "unmatched_no_assignee_login"


def test_map_payout_needs_every_binding(monkeypatch):
    for env in (mapping.QBO_DEPOSIT_ACCOUNT_ENV, mapping.QBO_INCOME_ACCOUNT_ENV, mapping.QBO_PAYEE_ENV):
        monkeypatch.delenv(env, raising=False)
    _, why = mapping.map_payout({"amount_cents": 100, "currency": "USD"}, "r1")
    assert why.startswith("payout_binding_missing_")
    monkeypatch.setenv(mapping.QBO_DEPOSIT_ACCOUNT_ENV, "35")
    monkeypatch.setenv(mapping.QBO_INCOME_ACCOUNT_ENV, "79")
    monkeypatch.setenv(mapping.QBO_PAYEE_ENV, "12")
    _, why = mapping.map_payout({"amount_cents": 100, "currency": "USD"}, "r1")
    assert why == "payout_payee_setting_invalid"  # bare id: Vendor or Customer?
    monkeypatch.setenv(mapping.QBO_PAYEE_ENV, "Vendor:12")
    got, why = mapping.map_payout({"amount_cents": 100, "currency": "usd"}, "r1")
    assert why == "" and got["currency"] == "USD" and got["amount_cents"] == 100
    assert (got["payee_type"], got["payee_id"]) == ("Vendor", "12")
    _, why = mapping.map_payout({"amount_cents": 0, "currency": "USD"}, "r1")
    assert why == "payout_amount_not_positive"


# --- pagination -----------------------------------------------------------------


def _pages(pages):
    calls = []

    def get(path, query):
        calls.append(dict(query))
        return pages[len(calls) - 1]

    return get, calls


def test_page_number_contract_reads_every_page():
    get, calls = _pages([
        {"data": [{"id": i} for i in range(50)], "meta": {"next": 2}},
        {"data": [{"id": i} for i in range(50, 60)], "meta": {"next": None}},
    ])
    assert len(paginate(get, "/v1/payouts")) == 60
    assert calls[1]["page"] == 2


def test_page_number_truncation_bug_is_fixed():
    # Before: meta {"next": 2} on a full page returned only page one.
    get, _ = _pages([{"data": [{"id": 1}], "meta": {"next": 2}},
                     {"data": [{"id": 2}], "meta": {"next": None}}])
    assert [r["id"] for r in paginate(get, "/v1/x", page_size=1)] == [1, 2]


def test_repeated_page_and_duplicate_ids_raise():
    get, _ = _pages([{"data": [{"id": 1}], "meta": {"next": 2}},
                     {"data": [{"id": 2}], "meta": {"next": 2}}])
    with pytest.raises(ValueError):
        paginate(get, "/v1/x", page_size=1)
    get, _ = _pages([{"data": [{"id": 1}], "meta": {"next": 2}},
                     {"data": [{"id": 1}], "meta": {"next": None}}])
    with pytest.raises(ValueError, match="duplicate"):
        paginate(get, "/v1/x", page_size=1)


def test_full_page_with_unknown_meta_raises():
    get, _ = _pages([{"data": [{"id": i} for i in range(50)], "meta": {"total": 99}}])
    with pytest.raises(ValueError, match="unverified_full_page"):
        paginate(get, "/v1/x")


# --- Test runner ----------------------------------------------------------------


class AscendFake:
    def fetch_cancelation_returns(self):
        return [{"id": "cr_buster", "billable": {"id": "bill-1", "policy_number": "BB-1"}},
                {"id": "cr_other", "billable": {"id": "bill-2", "policy_number": "OT-1"}}]

    def fetch_billable(self, billable_id):
        return {"id": billable_id, "program_id": "prog-1",
                "policy_number": "BB-1" if billable_id == "bill-1" else "OT-1"}

    def fetch_program(self, program_id):
        return {"id": program_id,
                "producer": {"email": "karla@streetsmart.insurance",
                             "first_name": "Karla", "last_name": "Brown"}}

    def fetch_programs(self):
        return []

    def fetch_payouts(self):
        return []


def test_deliver_test_once_only_sends_for_buster_and_reports_complete(tmp_path, monkeypatch):
    from robie_job_engine.ascend_sync import AscendEZLynxSyncManager, AscendSyncStore

    monkeypatch.setenv("ROBIE_ENV", "TEST")
    fake = FakeEZLynx()
    policies = PolicyClient([
        {"PolicyNumber": "BB-1", "ApplicantId": BUSTER, "AssignedUsername": "SSRobie"},
        {"PolicyNumber": "OT-1", "ApplicantId": "777", "AssignedUsername": "SSRobie"},
    ])

    class Rows(PolicyClient):
        def search_policy_by_number(self, number):
            return {"data": [r for r in self.rows if r["PolicyNumber"] == number]}

    manager = AscendEZLynxSyncManager(api_client=AscendFake(), store=AscendSyncStore(str(tmp_path / "legacy.db")),
                                      matcher=object(), poster=object(), quickbooks_client=object())
    summary = manager.deliver_test_once(ledger_path=str(tmp_path / "l.db"),
                                        destination=EZLynxAscendDestination(urlopen=fake),
                                        ezlynx_client=Rows(policies.rows))
    assert [c["key"] for c in summary["complete"]] == ["cr_buster"]
    assert summary["reasons"] == {"delivered_readback": 1, "staged_no_live_destination": 1}
    assert {d["applicant"] for d in fake.discussions.values()} == {BUSTER}


def test_deliver_test_once_refuses_outside_test(tmp_path, monkeypatch):
    from robie_job_engine.ascend_sync import AscendEZLynxSyncManager, AscendSyncStore

    monkeypatch.setenv("ROBIE_ENV", "PRODUCTION")
    manager = AscendEZLynxSyncManager(api_client=AscendFake(), store=AscendSyncStore(str(tmp_path / "legacy.db")),
                                      matcher=object(), poster=object(), quickbooks_client=object())
    with pytest.raises(RuntimeError, match="TEST"):
        manager.deliver_test_once(ledger_path=str(tmp_path / "l.db"))


# --- QBO payee entity type (Vendor and Customer ids overlap) -----------------


def test_parse_payee_requires_the_entity_type():
    assert mapping.parse_payee("Vendor:12") == ("Vendor", "12", "")
    assert mapping.parse_payee("customer: 12") == ("Customer", "12", "")
    for bad in ("12", "Employee:12", "Vendor:", "Vendor:abc", ""):
        assert mapping.parse_payee(bad)[2], bad


class OverlapQBO(FakeQBO):
    """Vendor 12 and Customer 12 both exist and are different records."""

    def __init__(self, vendor_active=True, customer_active=True, **kw):
        super().__init__(**kw)
        self.records = {
            "vendor/12": {"Vendor": {"Id": "12", "Active": vendor_active, "DisplayName": "Ascend Vendor"}},
            "customer/12": {"Customer": {"Id": "12", "Active": customer_active, "DisplayName": "Some Customer"}},
            "account/35": {"Account": {"Id": "35", "Active": True, "AccountType": "Bank", "Name": "Operating"}},
            "account/79": {"Account": {"Id": "79", "Active": True, "AccountType": "Income", "Name": "Commission"}},
        }
        self.gets = []

    def _request(self, method, resource, json_body=None, query_params=None):
        if method == "GET" and resource in self.records:
            self.gets.append(resource)
            return self.records[resource]
        return super()._request(method, resource, json_body, query_params)


def _qbo_env(monkeypatch, payee):
    monkeypatch.setenv(mapping.QBO_DEPOSIT_ACCOUNT_ENV, "35")
    monkeypatch.setenv(mapping.QBO_INCOME_ACCOUNT_ENV, "79")
    monkeypatch.setenv(mapping.QBO_PAYEE_ENV, payee)


def test_verifier_reads_only_the_configured_entity_type(monkeypatch):
    _qbo_env(monkeypatch, "Customer:12")
    qb = OverlapQBO()
    found = mapping.verify_qbo_mapping(qb)
    assert found["ok"] and found["payee"]["type"] == "Customer"
    assert found["payee"]["name"] == "Some Customer"
    assert "vendor/12" not in qb.gets


def test_verifier_never_falls_back_to_the_other_type(monkeypatch):
    # Before: an inactive/missing Vendor 12 fell through to Customer 12.
    _qbo_env(monkeypatch, "Vendor:12")
    qb = OverlapQBO(vendor_active=False)
    found = mapping.verify_qbo_mapping(qb)
    assert not found["ok"] and found["payee"]["type"] == "Vendor"
    assert "customer/12" not in qb.gets


def test_verifier_rejects_a_bare_payee_id(monkeypatch):
    _qbo_env(monkeypatch, "12")
    found = mapping.verify_qbo_mapping(OverlapQBO())
    assert not found["ok"] and "bare id" in found["payee"]["reason"]


def test_deposit_with_the_other_entity_type_is_not_complete(tmp_path):
    class WrongType(FakeQBO):
        def _request(self, method, resource, json_body=None, query_params=None):
            if method == "POST":
                json_body = json.loads(json.dumps(json_body))
                json_body["Line"][0]["DepositLineDetail"]["Entity"]["type"] = "Customer"
            return super()._request(method, resource, json_body, query_params)

    ledger = DurableLedger(str(tmp_path / "l.db"))
    assert ReliableDelivery(ledger, QBODepositDestination(WrongType())).process(payout()) == "destination_unverified"


def test_deposit_line_without_type_is_checked_by_name(tmp_path):
    class NoType(OverlapQBO):
        def __init__(self, name, **kw):
            super().__init__(**kw)
            self.name = name

        def _request(self, method, resource, json_body=None, query_params=None):
            res = super()._request(method, resource, json_body, query_params)
            if resource.startswith("deposit/"):
                dep = json.loads(json.dumps(res))
                ent = dep["Deposit"]["Line"][0]["DepositLineDetail"]["Entity"]
                ent.pop("type", None)
                ent["name"] = self.name
                return dep
            return res

    ok = ReliableDelivery(DurableLedger(str(tmp_path / "a.db")),
                          QBODepositDestination(NoType("Ascend Vendor"))).process(payout())
    assert ok == "delivered_readback"
    bad = ReliableDelivery(DurableLedger(str(tmp_path / "b.db")),
                           QBODepositDestination(NoType("Some Customer"))).process(payout())
    assert bad == "destination_unverified"
