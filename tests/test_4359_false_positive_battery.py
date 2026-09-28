"""False-positive battery for the 4359 weekly runner (Carlo 2026-09-27).

"Run a lot of tests on this to make sure that it's reliable and that we
don't pick up anything we're not looking for."

Every test here proves the worker does NOT nag about something it
shouldn't: dead policies, unverifiable liveness, stale/missing reports,
unresolvable CSRs, duplicates, already-sent items, non-Open rows — plus
the tricky real cases from the 2026-09-27 queue (Arellano / ISCA / Guarini)
and policy-number variant handling. Fake clients only: no network, no
secrets, no real sends.
"""

from datetime import date, timedelta

import pytest

from robie_job_engine.overdue_policy_change_reports import (
    ACTION,
    CC_CARLO,
    NotificationStore,
    OverduePolicyChangeReportWorker,
    PolicyChangeReportContractError,
    build_roster_maps,
    classify_policy_liveness,
    liveness_identity_note,
    parse_4359_csv,
    qualify_rows,
)

TODAY = date(2026, 9, 27)

HEADER = (
    "Account Name,Applicant ID,Policy Number,Line Of Business,Effective Date,"
    "Master Company,Request Status,Created By,Written Premium,Premium - Annualized,"
    "Branch,Department,Service Team,Assigned Producer,CSR,Preferred Language,"
    "Applicant Labels,Policy Labels,Change Request Created Date"
)


def csv_bytes(*rows: str) -> bytes:
    return ("\n".join([HEADER, *rows]) + "\n").encode("utf-8")


def row(account="SAPP Construction Corp", applicant="41055091", policy="S 2391821",
        created="2026-08-17", csr="Eimy Ramos", status="Open",
        carrier="Selective Insurance", lob="Commercial Pkg",
        producer="Sandy Santana") -> str:
    return (
        f"{account},{applicant},{policy},{lob},2025-12-10,{carrier},{status},"
        f"\"Quezada, Zeus\",$28936.00,$28936.00,Streetsmart Insurance,"
        f"Commercial Lines,,{producer},{csr},English,,,{created}"
    )


def fake_search(mapping):
    def search(number):
        return [dict(r) for r in mapping.get(number, [])]
    return search


def policy_row(number, account="41055091", status="Active",
               expiration="2026-12-10", **extra):
    base = {"policyNumber": number, "accountId": account,
            "policyStatus": status, "expirationDate": expiration,
            "premium": 28936.0}
    base.update(extra)
    return base


def disc(title, count, last):
    return {"title": title, "noteCount": count, "lastModified": last,
            "mostRecentNoteId": "1"}


def roster():
    """Active-employee roster with the real 2026-09-27 people."""
    return {
        "source_status": "available",
        "employees": {
            "Eimy Ramos": {"role": "Commercial Lines Account Technician",
                           "email": "eimy@streetsmart.insurance",
                           "department": "Commercial Lines", "manager": "",
                           "status": "Active"},
            "Lenin Perdomo": {"role": "Commercial Lines Account Technician",
                               "email": "lenin@streetsmart.insurance",
                               "department": "Commercial Lines", "manager": "",
                               "status": "Active"},
            "Erika Palacios": {"role": "Commercial Lines Account Manager",
                                "email": "erika@streetsmart.insurance",
                                "department": "Commercial Lines", "manager": "",
                                "status": "Active"},
            "Jackie Arriola": {"role": "Commercial Lines Account Manager",
                                "email": "jackie@streetsmart.insurance",
                                "department": "Commercial Lines", "manager": "",
                                "status": "Active"},
            "Sandy Santana": {"role": "Commercial Lines Department Manager",
                               "email": "sandy@streetsmart.insurance",
                               "department": "Commercial Lines", "manager": "",
                               "status": "Active"},
            "Andrea Nicole Illanes": {"role": "Commercial Lines Account Manager",
                                      "email": "andrea@streetsmart.insurance",
                                      "department": "Commercial Lines",
                                      "manager": "", "status": "Active"},
            "Angie Valladarez": {"role": "Commercial Lines Account Manager",
                                 "email": "angie@streetsmart.insurance",
                                 "department": "Commercial Lines", "manager": "",
                                 "status": "Active"},
            "Taylor Cimei": {"role": "Producer",
                             "email": "taylor@streetsmart.insurance",
                             "department": "Commercial Lines", "manager": "",
                             "status": "Active"},
        },
    }


def make_worker(tmp_path, rows, search_map, discussions, roster_override=None,
                sent_store=None):
    sent = []

    def fake_mailer(**kw):
        record = dict(kw)
        record["message_id"] = f"m{len(sent)}"
        sent.append(record)
        return record

    worker = OverduePolicyChangeReportWorker(
        queue_reader=lambda payload: parse_4359_csv(csv_bytes(*rows)),
        policy_search=fake_search(search_map),
        discussion_lookup=lambda applicant_id: discussions,
        directory_loader=lambda manifest: build_roster_maps(
            roster_override or roster()),
        mailer=fake_mailer,
        sent_store=sent_store or NotificationStore(tmp_path / "sent.json"),
    )
    return worker, sent


def job():
    return {"action_type": ACTION, "payload": {"manifest_path": "/tmp/manifest.json"}}


def live_discussions():
    return [disc("Commercial Auto Policy Change Request", 8, "2026-09-25T10:00:00")]


# -- fail-closed: report problems --------------------------------------------


def test_stale_report_fails_closed_zero_sends(tmp_path):
    worker, sent = make_worker(tmp_path, [], {}, [])
    worker.queue_reader = lambda payload: (_ for _ in ()).throw(
        PolicyChangeReportContractError("no fresh 4359 report was collected"))
    result = worker.perform(job(), idempotency_key="k1")
    assert not result.succeeded
    assert "4359 report" in result.error
    assert sent == []


def test_queue_reader_transport_failure_fails_closed(tmp_path):
    def boom(payload):
        raise PolicyChangeReportContractError("Gmail read failed: timeout")
    worker = OverduePolicyChangeReportWorker(
        queue_reader=boom,
        policy_search=fake_search({}),
        discussion_lookup=lambda aid: [],
        directory_loader=lambda m: build_roster_maps(roster()),
        mailer=lambda **kw: (_ for _ in ()).throw(AssertionError("must not send")),
        sent_store=NotificationStore(tmp_path / "sent.json"),
    )
    result = worker.perform(job(), idempotency_key="k1")
    assert not result.succeeded
    assert sent_missing(result)


def sent_missing(result):
    return result.destination.get("delivery_receipts", []) == []


def test_discussion_api_down_fails_closed_zero_sends(tmp_path):
    def down(aid):
        raise PolicyChangeReportContractError("DiscussionApi lookup failed: 401")
    worker = OverduePolicyChangeReportWorker(
        queue_reader=lambda payload: parse_4359_csv(csv_bytes(row())),
        policy_search=fake_search({"S 2391821": [policy_row("S 2391821")]}),
        discussion_lookup=down,
        directory_loader=lambda m: build_roster_maps(roster()),
        mailer=lambda **kw: (_ for _ in ()).throw(AssertionError("must not send")),
        sent_store=NotificationStore(tmp_path / "sent.json"),
    )
    result = worker.perform(job(), idempotency_key="k1")
    assert not result.succeeded
    assert "DiscussionApi" in result.error


def test_per_applicant_discussion_failure_marks_unverified_not_blocking(tmp_path):
    # One applicant's lookup blowing up must not kill the other CSR's email,
    # but the affected item's context is flagged UNVERIFIED in the email.
    def flaky(aid):
        if aid == "999":
            raise RuntimeError("transient")
        return live_discussions()
    rows = [row(), row(account="Other Co", applicant="999", policy="P2",
                       created="2026-08-01")]
    search_map = {"S 2391821": [policy_row("S 2391821")],
                  "P2": [policy_row("P2", account="999")]}
    worker = OverduePolicyChangeReportWorker(
        queue_reader=lambda payload: parse_4359_csv(csv_bytes(*rows)),
        policy_search=fake_search(search_map),
        discussion_lookup=flaky,
        directory_loader=lambda m: build_roster_maps(roster()),
        mailer=lambda **kw: {"message_id": "m", **kw},
        sent_store=NotificationStore(tmp_path / "sent.json"),
    )
    result = worker.perform(job(), idempotency_key="k1")
    assert result.succeeded
    assert "UNVERIFIED" in result.destination["delivery_receipts"][0]["text_body"]


# -- fail-closed: identity problems ------------------------------------------


def test_unknown_csr_no_send_reported(tmp_path):
    rows = [row(), row(csr="Nobody Here", policy="ZZ1", applicant="1",
                       account="Ghost Co", created="2026-08-01")]
    search_map = {"S 2391821": [policy_row("S 2391821")],
                  "ZZ1": [policy_row("ZZ1", account="1")]}
    worker, sent = make_worker(tmp_path, rows, search_map, live_discussions())
    result = worker.perform(job(), idempotency_key="k1")
    assert result.succeeded
    assert result.destination["unresolved_csrs"] == ["Nobody Here"]
    assert len(sent) == 1  # only the resolvable CSR is emailed
    assert sent[0]["to"] == ["eimy@streetsmart.insurance"]
    assert "Ghost Co" not in sent[0]["text_body"]


def test_terminated_csr_no_send(tmp_path):
    # Cesar Romero was terminated but is still named on the queue: the
    # active-only roster must not resolve him, and nothing goes out for him.
    rows = [row(csr="Cesar Romero", policy="ZZ9", applicant="9",
                account="Old Co", created="2026-08-01")]
    search_map = {"ZZ9": [policy_row("ZZ9", account="9")]}
    worker, sent = make_worker(tmp_path, rows, search_map, live_discussions())
    result = worker.perform(job(), idempotency_key="k1")
    assert result.succeeded
    assert result.destination["unresolved_csrs"] == ["Cesar Romero"]
    assert sent == []
    assert result.destination["csr_count"] == 0


def test_blank_csr_fails_closed_zero_sends(tmp_path):
    rows = [row(csr="", policy="ZZ2", applicant="2", created="2026-08-01")]
    search_map = {"ZZ2": [policy_row("ZZ2", account="2")]}
    worker, sent = make_worker(tmp_path, rows, search_map, live_discussions())
    result = worker.perform(job(), idempotency_key="k1")
    assert not result.succeeded
    assert "no CSR" in result.error
    assert sent == []


# -- dedup / re-nag -----------------------------------------------------------


def test_duplicate_rows_single_email(tmp_path):
    rows = [row(), row()]  # byte-identical duplicate queue rows
    search_map = {"S 2391821": [policy_row("S 2391821")]}
    store = NotificationStore(tmp_path / "sent.json")
    worker, sent = make_worker(tmp_path, rows, search_map, live_discussions(),
                                sent_store=store)
    result = worker.perform(job(), idempotency_key="k1")
    assert result.succeeded
    assert len(sent) == 1
    assert sent[0]["text_body"].count("SAPP Construction Corp") == 2  # both items listed
    assert len(store._sent) == 1  # one dedup key


def test_renag_boundary_worker_level(tmp_path):
    rows = [row(policy="P6", applicant="6", created="2026-08-01"),
            row(policy="P7", applicant="7", created="2026-08-01")]
    search_map = {"P6": [policy_row("P6", account="6")],
                  "P7": [policy_row("P7", account="7")]}
    store = NotificationStore(tmp_path / "sent.json")
    # Relative to the real today (perform uses date.today()): P6 was nagged
    # 6 days ago (not due), P7 7 days ago (due for re-nag).
    store.mark_sent({"CSR": "Eimy Ramos", "Policy Number": "P6",
                     "created_date": "2026-08-01"}, date.today() - timedelta(days=6))
    store.mark_sent({"CSR": "Eimy Ramos", "Policy Number": "P7",
                     "created_date": "2026-08-01"}, date.today() - timedelta(days=7))
    store.save()
    worker, sent = make_worker(tmp_path, rows, search_map, live_discussions(),
                                sent_store=NotificationStore(tmp_path / "sent.json"))
    result = worker.perform(job(), idempotency_key="k1")
    assert result.succeeded
    assert result.destination["due_for_nag"] == 1
    assert len(sent) == 1
    assert "policy P7" in sent[0]["text_body"]
    assert "policy P6" not in sent[0]["text_body"]


# -- status gating ------------------------------------------------------------


@pytest.mark.parametrize("status", ["Closed", "Completed", "Cancelled", "In Progress", ""])
def test_non_open_status_never_qualifies(status):
    rows = parse_4359_csv(csv_bytes(row(status=status)))
    assert qualify_rows(rows, TODAY) == []


def test_arellano_open_pcr_still_nags_despite_complete_task(tmp_path):
    # Arellano 2026-09-27: the EZLynx task was Complete but the PCR was
    # still Open and the address was not updated anywhere. The worker has
    # no task input at all — Open status alone must trigger the nag.
    rows = [row(account="Arellano's Future Landscaping LLC", applicant="111",
                policy="13WECAT1F8T", created="2026-09-04",
                csr="Lenin Perdomo", carrier="Hartford", producer="Andrea Illanes")]
    search_map = {"13WECAT1F8T": [policy_row("13WECAT1F8T", account="111")]}
    worker, sent = make_worker(tmp_path, rows, search_map, live_discussions())
    result = worker.perform(job(), idempotency_key="k1")
    assert result.succeeded
    assert len(sent) == 1
    assert sent[0]["to"] == ["lenin@streetsmart.insurance"]
    # Producer "Andrea Illanes" resolves via the first+last alias to
    # Andrea Nicole Illanes — the right mailbox, not a wrong CC.
    assert "andrea@streetsmart.insurance" in sent[0]["cc"]
    assert "13WECAT1F8T" in sent[0]["text_body"]
    assert f"{(date.today() - date(2026, 9, 4)).days} days ago" in sent[0]["text_body"]


def test_guarini_email_carries_discussion_context(tmp_path):
    # Guarini: a 9/15 "endorsement received" note exists but the queue row
    # is still Open — the nag must show the CSR the last activity so they
    # see what already happened.
    rows = [row(account="John Guarini", applicant="222", policy="04283052",
                created="2026-08-31", csr="Jackie Arriola",
                producer="Taylor Cimei")]
    search_map = {"04283052": [policy_row("04283052", account="222")]}
    discussions = [disc("Commercial Auto Policy Change Request", 5,
                        "2026-09-15T10:00:00")]
    worker, sent = make_worker(tmp_path, rows, search_map, discussions)
    result = worker.perform(job(), idempotency_key="k1")
    assert result.succeeded
    last_activity_days = (date.today() - date(2026, 9, 15)).days
    assert f"5 notes, last activity {last_activity_days} days ago" in sent[0]["text_body"]


# -- policy-number variants ----------------------------------------------------


def test_isca_variant_match_surfaces_live_policy_number(tmp_path):
    # ISCA: queue "BDG-312624001" is a concatenation; the live policy is
    # BDG-3126240-02. The email must name the live policy explicitly —
    # never silently match the wrong record.
    rows = [row(account="ISCA Contracting INC", applicant="333",
                policy="BDG-312624001", created="2026-08-21",
                csr="Erika Palacios", producer="Angie Valladarez")]
    search_map = {
        "BDG-312624001": [],
        "BDG-3126240-01": [],
        "BDG-3126240-02": [policy_row("BDG-3126240-02", account="333",
                                      insuredName="ISCA Contracting INC")],
    }
    worker, sent = make_worker(tmp_path, rows, search_map, live_discussions())
    result = worker.perform(job(), idempotency_key="k1")
    assert result.succeeded
    assert len(sent) == 1
    assert "BDG-3126240-02" in sent[0]["text_body"]
    assert "de-concatenated variant" in sent[0]["text_body"]


def test_isca_name_mismatch_surfaces_in_email(tmp_path):
    # Carrier emails spell "ICSA Construction Inc" vs EZLynx
    # "ISCA Contracting INC": the mismatch must be visible in the email.
    rows = [row(account="ISCA Contracting INC", applicant="333",
                policy="BDG-312624001", created="2026-08-21",
                csr="Erika Palacios", producer="Angie Valladarez")]
    search_map = {
        "BDG-312624001": [],
        "BDG-3126240-01": [],
        "BDG-3126240-02": [policy_row("BDG-3126240-02", account="333",
                                      insuredName="ICSA Construction Inc")],
    }
    worker, sent = make_worker(tmp_path, rows, search_map, live_discussions())
    result = worker.perform(job(), idempotency_key="k1")
    assert result.succeeded
    assert "Name check:" in sent[0]["text_body"]
    assert "ICSA Construction Inc" in sent[0]["text_body"]


def test_name_case_only_difference_does_not_flag(tmp_path):
    rows = [row(account="ISCA Contracting INC", applicant="333",
                policy="BDG-312624001", created="2026-08-21",
                csr="Erika Palacios", producer="Angie Valladarez")]
    search_map = {
        "BDG-312624001": [],
        "BDG-3126240-01": [],
        "BDG-3126240-02": [policy_row("BDG-3126240-02", account="333",
                                      insuredName="ISCA Contracting Inc.")],
    }
    worker, sent = make_worker(tmp_path, rows, search_map, live_discussions())
    result = worker.perform(job(), idempotency_key="k1")
    assert result.succeeded
    assert "Name check:" not in sent[0]["text_body"]


def test_variant_deleted_exact_with_live_variant_nags_once(tmp_path):
    # Exact search hits a DELETED record under the queue number, but a
    # live de-concatenated variant exists: LIVE, exactly one email.
    search_map = {
        "BDG-312624001": [policy_row("BDG-312624001", account="333",
                                     status="Deleted")],
        "BDG-3126240-01": [],
        "BDG-3126240-02": [policy_row("BDG-3126240-02", account="333")],
    }
    verdict = classify_policy_liveness(
        fake_search(search_map), "BDG-312624001", "333", TODAY)
    assert verdict["verdict"] == "LIVE"
    assert verdict["matched_policy_number"] == "BDG-3126240-02"


def test_variant_only_deleted_history_is_dead_not_held(tmp_path):
    search_map = {
        "BDG-312624001": [policy_row("BDG-312624001", account="333",
                                     status="Deleted")],
        "BDG-3126240-01": [],
        "BDG-3126240-02": [],
    }
    rows = [row(account="ISCA Contracting INC", applicant="333",
                policy="BDG-312624001", created="2026-08-21",
                csr="Erika Palacios", producer="Angie Valladarez")]
    worker, sent = make_worker(tmp_path, rows, search_map, live_discussions())
    result = worker.perform(job(), idempotency_key="k1")
    assert result.succeeded
    assert result.destination["overdue_dead_excluded"] == 1
    assert sent == []


def test_liveness_identity_note_unit():
    item = {"Policy Number": "BDG-312624001", "Account Name": "ISCA Contracting INC",
            "liveness": {"matched_policy_number": "BDG-3126240-02",
                         "reason": "live policy found via de-concatenated variant",
                         "live_account_name": "ISCA Contracting INC"}}
    note = liveness_identity_note(item)
    assert "BDG-3126240-02" in note
    assert "Name check:" not in note
    item2 = {"Policy Number": "S 2391821", "Account Name": "SAPP",
             "liveness": {"matched_policy_number": "S 2391821",
                          "reason": "exact", "live_account_name": ""}}
    assert liveness_identity_note(item2) == ""


# -- recipient safety ----------------------------------------------------------


def test_recipient_safety_exact_addresses(tmp_path):
    reg = {
        "source_status": "available",
        "employees": {
            "Eimy Ramos": {"role": "Commercial Lines Account Technician",
                           "email": "eimy@streetsmart.insurance",
                           "department": "Test Dept", "manager": "",
                           "status": "Active"},
            "Dana Manager": {"role": "Test Dept Department Manager",
                             "email": "dana@streetsmart.insurance",
                             "department": "Test Dept", "manager": "",
                             "status": "Active"},
            "Taylor Cimei": {"role": "Producer",
                             "email": "taylor@streetsmart.insurance",
                             "department": "Test Dept", "manager": "",
                             "status": "Active"},
        },
    }
    rows = [row(csr="Eimy Ramos", producer="Taylor Cimei")]
    search_map = {"S 2391821": [policy_row("S 2391821")]}
    worker, sent = make_worker(tmp_path, rows, search_map, live_discussions(),
                                roster_override=reg)
    result = worker.perform(job(), idempotency_key="k1")
    assert result.succeeded
    assert sent[0]["to"] == ["eimy@streetsmart.insurance"]
    assert sent[0]["cc"] == ["dana@streetsmart.insurance",
                             CC_CARLO,
                             "jake@streetsmart.insurance",
                             "gabrielac@streetsmart.insurance",
                             "sandy@streetsmart.insurance",
                             "taylor@streetsmart.insurance"]
    assert len(set(sent[0]["cc"])) == len(sent[0]["cc"])  # no duplicates
    for addr in sent[0]["to"] + sent[0]["cc"]:
        assert addr.endswith("@streetsmart.insurance"), addr  # no outside addresses


# -- EZLynx OAuth resolution: explicit env wins, else Secret Manager ---------
from robie_job_engine.overdue_policy_change_reports import _resolve_ezlynx_oauth  # noqa: E402


def _clear_oauth_env(monkeypatch, prefix):
    for suffix in ("TOKEN_ENDPOINT", "CLIENT_ID", "CLIENT_SECRET",
                   "USERNAME", "INTEGRATION_GROUP_ID"):
        monkeypatch.delenv(f"{prefix}_{suffix}", raising=False)


def test_oauth_explicit_env_wins_over_secret_manager(monkeypatch):
    import robie_job_engine.overdue_policy_change_reports as mod
    monkeypatch.setenv("EZLYNX_POLICY_API_TOKEN_ENDPOINT", "https://t")
    monkeypatch.setenv("EZLYNX_POLICY_API_CLIENT_ID", "cid")
    monkeypatch.setenv("EZLYNX_POLICY_API_CLIENT_SECRET", "csec")

    def boom():
        raise AssertionError("Secret Manager must not be consulted when env is set")

    monkeypatch.setitem(__import__("sys").modules, "x", None)  # no-op guard
    import robie_job_engine.ezlynx_api as ez
    monkeypatch.setattr(ez, "load_ezlynx_api_config", boom)
    oauth = _resolve_ezlynx_oauth("EZLYNX_POLICY_API")
    assert oauth["token_endpoint"] == "https://t"
    assert oauth["client_id"] == "cid"


def test_oauth_falls_back_to_secret_manager(monkeypatch):
    import robie_job_engine.ezlynx_api as ez

    _clear_oauth_env(monkeypatch, "EZLYNX_POLICY_API")

    class FakeConfig:
        token_endpoint = "https://sm/token"
        client_id = "sm-cid"
        client_secret = "sm-csec"
        username = "sm-user"
        integration_group_id = "sm-ig"

    monkeypatch.setattr(ez, "load_ezlynx_api_config", lambda *a, **k: FakeConfig())
    oauth = _resolve_ezlynx_oauth("EZLYNX_POLICY_API")
    assert oauth["token_endpoint"] == "https://sm/token"
    assert oauth["client_id"] == "sm-cid"
    assert oauth["integration_group_id"] == "sm-ig"


def test_oauth_unconfigured_fails_closed(monkeypatch):
    import robie_job_engine.ezlynx_api as ez

    _clear_oauth_env(monkeypatch, "EZLYNX_DISCUSSION")

    def missing():
        raise RuntimeError("ROBIE_ENV must be TEST or PRODUCTION")

    monkeypatch.setattr(ez, "load_ezlynx_api_config", missing)
    with pytest.raises(PolicyChangeReportContractError, match="not configured"):
        _resolve_ezlynx_oauth("EZLYNX_DISCUSSION")
