"""Reliability battery for the phase-2 4359 carrier-contact worker.

Every test proves the worker does NOT contact the wrong carrier, does NOT
email about an endorsement that already downloaded, does NOT duplicate
outreach, and NEVER sends client policy data anywhere it shouldn't go.
Fake clients only: no network, no secrets, no real sends.
"""

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from robie_job_engine.carrier_policy_change_routes import (
    emailable_routes,
    load_routing_table,
    manual_routes,
    normalize_carrier_name,
    resolve_carrier,
)
from robie_job_engine.overdue_policy_change_reports import (
    notification_key,
    parse_4359_csv,
)
from robie_job_engine.policy_change_carrier_contact import (
    GRACE_DAYS,
    RECONTACT_DAYS,
    CarrierContactStore,
    DryRunCarrierContactStore,
    PolicyChangeCarrierContactWorker,
    build_carrier_email,
    eligibility,
    find_endorsement_documents,
)

TODAY = date(2026, 9, 27)

HEADER = (
    "Account Name,Applicant ID,Policy Number,Line Of Business,Effective Date,"
    "Master Company,Request Status,Created By,Written Premium,Premium - Annualized,"
    "Branch,Department,Service Team,Assigned Producer,CSR,Preferred Language,"
    "Applicant Labels,Policy Labels,Change Request Created Date"
)


def row(account="SAPP Construction Corp", applicant="41055091", policy="S 2391821",
        created="2026-08-17", csr="Eimy Ramos", status="Open",
        carrier="Merchants Insurance Group", lob="Commercial Pkg",
        producer="Sandy Santana") -> str:
    return (
        f"{account},{applicant},{policy},{lob},2025-12-10,{carrier},{status},"
        f"\"Quezada, Zeus\",$28936.00,$28936.00,Streetsmart Insurance,"
        f"Commercial Lines,,{producer},{csr},English,,,{created}"
    )


def csv_bytes(*rows: str) -> bytes:
    return ("\n".join([HEADER, *rows]) + "\n").encode("utf-8")


def fake_table():
    return {
        "carriers": [
            {"record_id": "120", "name": "Merchants Insurance Group",
             "route_status": "ok",
             "routes": [{"label": "Policy Changes", "type": "email",
                         "email": "MidlanticOffice@Merchantsgroup.com",
                         "cc_emails": [], "phone": None, "url": None, "notes": ""}]},
            {"record_id": "152", "name": "Progressive Insurance",
             "route_status": "ok",
             "routes": [{"label": "Policy Changes", "type": "email",
                         "email": "commercialauto@email.progressive.com",
                         "cc_emails": [], "phone": None, "url": None, "notes": ""}]},
            {"record_id": "147", "name": "Personal Umbrella",
             "route_status": "manual",
             "routes": [{"label": "Policy Changes", "type": "phone",
                         "email": None, "cc_emails": [],
                         "phone": "(510) 903-3313", "url": None,
                         "notes": "phone-only"}]},
            {"record_id": "8", "name": "Allstate Insurance",
             "route_status": "manual",
             "routes": [{"label": "Policy Changes", "type": "portal",
                         "email": None, "cc_emails": [], "phone": None,
                         "url": None, "notes": "done on website"}]},
            {"record_id": "102", "name": "Integon National Insurance Company",
             "route_status": "manual",
             "routes": [{"label": "Policy Changes", "type": "fax",
                         "email": None, "cc_emails": [],
                         "phone": "(201) 368-8649", "url": None,
                         "notes": "Fax Only — not a callable phone route"}]},
            {"record_id": "127", "name": "National Continental Ins",
             "route_status": "manual",
             "routes": [{"label": "Policy Changes", "type": "email",
                         "email": "caipcsr@nationalcontinental.com",
                         "cc_emails": [], "phone": "8009372247", "url": None,
                         "notes": "address in testing phase — verify first"}]},
            {"record_id": "188", "name": "Tuscano Agency",
             "route_status": "missing", "routes": []},
        ]
    }


class FakeFollowupStore:
    """Duck-typed stand-in for phase 3's FollowupStore."""

    def __init__(self, statuses: dict | None = None):
        self.statuses = statuses or {}
        self.events: list[tuple] = []
        self.set_status_calls: list[tuple] = []

    def get(self, key: str) -> dict:
        return {"status": self.statuses.get(key), "history": []}

    def record_event(self, key, event, detail, today):
        self.events.append((key, event, detail, today))

    def set_status(self, key, status, today, detail=""):
        self.set_status_calls.append((key, status))


def make_worker(tmp_path, table=None, docs=None, statuses=None,
                nag_days_ago: int | None = 10, rows=None):
    sent = {}
    mails: list[dict] = []
    parsed = parse_4359_csv(csv_bytes(*(rows or [row()])))
    for r in parsed:
        key = notification_key(r["CSR"], r["Policy Number"],
                               r["Change Request Created Date"])
        if nag_days_ago is not None:
            sent[key] = (TODAY - timedelta(days=nag_days_ago)).isoformat()
    sent_path = tmp_path / "sent.json"
    sent_path.write_text(json.dumps(sent))

    def document_search(applicant_id):
        return list((docs or {}).get(str(applicant_id), []))

    def mailer(*, to, cc, subject, text_body, html_body):
        mails.append({"to": to, "cc": cc, "subject": subject,
                      "text_body": text_body, "html_body": html_body})
        return {"message_id": "fake"}

    worker = PolicyChangeCarrierContactWorker(
        queue_reader=lambda payload: parsed,
        document_search=document_search,
        mailer=mailer,
        routing_table=table or fake_table(),
    )
    payload = {
        "sent_store_path": str(sent_path),
        "carrier_store_path": str(tmp_path / "carrier_contact.json"),
        "followup_store": FakeFollowupStore(statuses),
    }
    return worker, payload, mails, tmp_path


def run_worker(worker, payload, dry_run=False):
    job = {"action_type": "contact_carriers", "payload": payload}
    return worker.perform(job, dry_run=dry_run, today=TODAY)


# -- carrier resolution --------------------------------------------------------

def test_exact_carrier_match_real_table():
    table = load_routing_table()
    carrier = resolve_carrier("Merchants Insurance Group", table)
    assert carrier is not None and carrier["record_id"] == "120"


def test_progressive_does_not_match_asi_progressive_email():
    """ASI's route uses an @email.progressive.com address; the carrier
    'Progressive' must still resolve to the Progressive record (#152)."""
    table = load_routing_table()
    carrier = resolve_carrier("Progressive", table)
    assert carrier is not None and carrier["record_id"] == "152"


def test_ambiguous_carrier_name_never_guessed():
    table = load_routing_table()
    assert resolve_carrier("National", table) is None


def test_unknown_carrier_returns_none():
    assert resolve_carrier("Nonexistent Mutual Insurance", fake_table()) is None
    assert resolve_carrier("", fake_table()) is None
    assert resolve_carrier(None, fake_table()) is None


def test_normalize_strips_suffixes():
    assert normalize_carrier_name("Merchants Insurance Group") == "merchants"
    assert normalize_carrier_name("The Hartford Service Center") == "the hartford"


def test_routing_table_rejects_bad_email(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"carriers": [
        {"record_id": "1", "name": "X",
         "routes": [{"type": "email", "email": "not-an-email"}]}]}))
    with pytest.raises(ValueError):
        load_routing_table(bad)


def test_fax_only_route_never_emailed(tmp_path):
    rows = [row(carrier="Integon National Insurance Company")]
    worker, payload, mails, _ = make_worker(tmp_path, rows=rows)
    evidence = run_worker(worker, payload)
    assert evidence["summary"]["emailed"] == 0
    assert len(evidence["summary"]["manual_action"]) == 1
    assert mails == []


def test_ambiguous_manual_email_route_never_auto_sent(tmp_path):
    # #127-style: the directory lists an email, but flagged "in testing
    # phase" — the worker must not auto-send; an agent verifies first.
    rows = [row(carrier="National Continental Ins")]
    worker, payload, mails, _ = make_worker(tmp_path, rows=rows)
    evidence = run_worker(worker, payload)
    assert evidence["summary"]["emailed"] == 0
    assert len(evidence["summary"]["manual_action"]) == 1
    assert mails == []


def test_shipped_table_has_expected_coverage():
    table = load_routing_table()
    carriers = table["carriers"]
    assert len(carriers) == 206
    assert len({c["record_id"] for c in carriers}) == 206
    statuses = {c["route_status"] for c in carriers}
    assert statuses <= {"ok", "manual", "missing"}
    ok = [c for c in carriers if c["route_status"] == "ok"]
    manual = [c for c in carriers if c["route_status"] == "manual"]
    assert ok and manual
    for carrier in carriers:
        for route in carrier["routes"]:
            assert route["type"] in ("email", "portal", "phone", "fax")


def test_integon_fax_not_phone_in_shipped_table():
    table = load_routing_table()
    carrier = table["by_record"]["102"]
    assert carrier["route_status"] == "manual"
    assert carrier["routes"][0]["type"] == "fax"
    assert emailable_routes(carrier) == []


# -- endorsement-landed check ----------------------------------------------------

def test_endorsement_already_downloaded_suppresses_email(tmp_path):
    docs = {"41055091": [{"id": "1", "name": "Endorsement S2391821 revised dec"}]}
    worker, payload, mails, _ = make_worker(tmp_path, docs=docs)
    evidence = run_worker(worker, payload)
    assert evidence["succeeded"]
    assert evidence["summary"]["endorsement_found"] == 1
    assert evidence["summary"]["emailed"] == 0
    assert mails == []


def test_endorsement_for_different_policy_does_not_suppress(tmp_path):
    docs = {"41055091": [{"id": "1", "name": "Endorsement S9999999 revised dec"}]}
    worker, payload, mails, _ = make_worker(tmp_path, docs=docs)
    evidence = run_worker(worker, payload)
    assert evidence["summary"]["emailed"] == 1
    assert len(mails) == 1


def test_document_api_outage_fails_closed_no_email(tmp_path):
    def boom(applicant_id):
        from robie_job_engine.overdue_policy_change_reports import (
            PolicyChangeReportContractError)
        raise PolicyChangeReportContractError("DocumentApi down")

    worker, payload, mails, _ = make_worker(tmp_path)
    worker.document_search = boom
    evidence = run_worker(worker, payload)
    assert evidence["succeeded"]
    assert len(evidence["summary"]["held"]) == 1
    assert evidence["summary"]["emailed"] == 0
    assert mails == []


def test_find_endorsement_documents_requires_policy_digits():
    docs = [{"id": "1", "name": "Endorsement revised dec page"}]
    assert find_endorsement_documents(docs, "S 2391821") == []


# -- dedup / grace / re-contact ----------------------------------------------------

def test_grace_period_blocks_early_contact(tmp_path):
    worker, payload, mails, _ = make_worker(tmp_path, nag_days_ago=GRACE_DAYS - 1)
    evidence = run_worker(worker, payload)
    assert evidence["summary"]["emailed"] == 0
    assert evidence["summary"]["skipped"] == 1
    assert mails == []


def test_contact_after_grace_period(tmp_path):
    worker, payload, mails, _ = make_worker(tmp_path, nag_days_ago=GRACE_DAYS + 3)
    evidence = run_worker(worker, payload)
    assert evidence["summary"]["emailed"] == 1
    assert mails[0]["to"] == ["MidlanticOffice@Merchantsgroup.com"]


def test_grace_boundary_exactly_5_days_is_eligible(tmp_path):
    # Carlo approved 5 days on 2026-09-27: nagged exactly 5 days ago
    # with no CSR progress -> the carrier may be contacted.
    assert GRACE_DAYS == 5
    worker, payload, mails, _ = make_worker(tmp_path, nag_days_ago=5)
    evidence = run_worker(worker, payload)
    assert evidence["summary"]["emailed"] == 1
    assert mails[0]["to"] == ["MidlanticOffice@Merchantsgroup.com"]


def test_grace_blocks_at_4_days(tmp_path):
    worker, payload, mails, _ = make_worker(tmp_path, nag_days_ago=4)
    evidence = run_worker(worker, payload)
    assert evidence["summary"]["emailed"] == 0
    assert evidence["summary"]["skipped"] == 1
    assert mails == []


def test_portal_actions_carry_url_in_manual_queue(tmp_path):
    # Portal routes surface their URL in the manual-action queue as
    # PortalAction records: the human (later the portal worker) starts
    # from the URL.
    from robie_job_engine.policy_change_carrier_contact import (
        build_portal_action,
        portal_actions_for,
    )
    carrier = {
        "name": "Kingstone Insurance",
        "record_id": "110",
        "route_status": "manual",
        "routes": [
            {"label": "Policy Changes", "type": "portal",
             "email": "pldocs@kingstoneic.com", "url": "https://goo.gl/woUNvK",
             "notes": "directory: 'Done Online via System'; email as fallback"},
        ],
    }
    item = {"Policy Number": "KX123", "Account Name": "Test LLC"}
    actions = portal_actions_for(item, carrier)
    assert len(actions) == 1
    action = actions[0]
    assert action["type"] == "portal_action"
    assert action["carrier"] == "Kingstone Insurance"
    assert action["portal_url"] == "https://goo.gl/woUNvK"
    assert action["policy_number"] == "KX123"
    assert "action_needed" in action and action["action_needed"]
    # build_portal_action tolerates a route with no URL (directory names
    # a portal without one): the URL field is None, the human fills it in.
    no_url = dict(carrier["routes"][0]); no_url["url"] = None
    action2 = build_portal_action(item, carrier, no_url)
    assert action2["portal_url"] is None
    # phone/fax routes never produce portal actions.
    phone_only = dict(carrier); phone_only["routes"] = [
        {"label": "Policy Changes", "type": "phone", "phone": "(800) 555-0100",
         "url": None, "email": None, "notes": ""}]
    assert portal_actions_for(item, phone_only) == []


def test_no_recontact_inside_window(tmp_path):
    worker, payload, mails, _ = make_worker(tmp_path)
    store = CarrierContactStore(payload["carrier_store_path"])
    key = notification_key("Eimy Ramos", "S 2391821", "2026-08-17")
    store.record_contact(key, "MidlanticOffice@Merchantsgroup.com",
                         TODAY - timedelta(days=RECONTACT_DAYS - 9))
    store.save()
    evidence = run_worker(worker, payload)
    assert evidence["summary"]["emailed"] == 0
    assert mails == []


def test_recontact_allowed_after_window(tmp_path):
    worker, payload, mails, _ = make_worker(tmp_path)
    store = CarrierContactStore(payload["carrier_store_path"])
    key = notification_key("Eimy Ramos", "S 2391821", "2026-08-17")
    store.record_contact(key, "MidlanticOffice@Merchantsgroup.com",
                         TODAY - timedelta(days=RECONTACT_DAYS))
    store.save()
    evidence = run_worker(worker, payload)
    assert evidence["summary"]["emailed"] == 1


def test_never_nagged_change_is_skipped(tmp_path):
    worker, payload, mails, _ = make_worker(tmp_path, nag_days_ago=None)
    evidence = run_worker(worker, payload)
    assert evidence["summary"]["emailed"] == 0
    assert mails == []


# -- phase-3 interplay --------------------------------------------------------------

def test_csr_engaged_gets_check_only_no_email(tmp_path):
    key = notification_key("Eimy Ramos", "S 2391821", "2026-08-17")
    worker, payload, mails, _ = make_worker(tmp_path, statuses={key: "in_progress"})
    evidence = run_worker(worker, payload)
    assert evidence["summary"]["check_only"] == 1
    assert evidence["summary"]["emailed"] == 0
    assert mails == []


def test_phase3_owned_statuses_skipped_entirely(tmp_path):
    searched = []
    for status in ("docs_claimed", "confirmed", "discrepancy", "needs_human"):
        key = notification_key("Eimy Ramos", "S 2391821", "2026-08-17")
        worker, payload, mails, _ = make_worker(tmp_path, statuses={key: status})

        def tracking_search(applicant_id):
            searched.append(applicant_id)
            return []

        worker.document_search = tracking_search
        evidence = run_worker(worker, payload)
        assert evidence["summary"]["emailed"] == 0, status
        assert mails == [], status
    assert searched == [], "phase-3-owned changes must not even hit DocumentApi"


def test_cross_post_never_changes_phase3_status(tmp_path):
    key = notification_key("Eimy Ramos", "S 2391821", "2026-08-17")
    worker, payload, mails, _ = make_worker(tmp_path)
    followup = payload["followup_store"]
    run_worker(worker, payload)
    assert followup.set_status_calls == []
    assert any(e[1] == "carrier_contacted" for e in followup.events)


def test_terminated_csr_change_still_worked(tmp_path):
    # No CSR-active gate in this worker: a change from a CSR who later left
    # still gets the carrier chase.
    rows = [row(csr="Ghost Agent")]
    worker, payload, mails, _ = make_worker(tmp_path, rows=rows)
    evidence = run_worker(worker, payload)
    assert evidence["summary"]["emailed"] == 1


# -- routing queues ------------------------------------------------------------------

def test_missing_route_goes_to_awaiting_directory(tmp_path):
    rows = [row(carrier="Tuscano Agency")]
    worker, payload, mails, _ = make_worker(tmp_path, rows=rows)
    evidence = run_worker(worker, payload)
    assert evidence["summary"]["emailed"] == 0
    assert len(evidence["summary"]["awaiting_directory"]) == 1
    assert mails == []


def test_unresolved_carrier_goes_to_awaiting_directory(tmp_path):
    rows = [row(carrier="Nonexistent Mutual Insurance Company")]
    worker, payload, mails, _ = make_worker(tmp_path, rows=rows)
    evidence = run_worker(worker, payload)
    assert evidence["summary"]["emailed"] == 0
    assert len(evidence["summary"]["awaiting_directory"]) == 1


def test_phone_only_route_never_emailed(tmp_path):
    rows = [row(carrier="Personal Umbrella")]
    worker, payload, mails, _ = make_worker(tmp_path, rows=rows)
    evidence = run_worker(worker, payload)
    assert evidence["summary"]["emailed"] == 0
    assert len(evidence["summary"]["manual_action"]) == 1
    assert mails == []


def test_portal_route_never_emailed(tmp_path):
    rows = [row(carrier="Allstate Insurance")]
    worker, payload, mails, _ = make_worker(tmp_path, rows=rows)
    evidence = run_worker(worker, payload)
    assert evidence["summary"]["emailed"] == 0
    assert len(evidence["summary"]["manual_action"]) == 1


def test_manual_routes_helper():
    table = fake_table()
    allstate = resolve_carrier("Allstate Insurance", table)
    assert emailable_routes(allstate) == []
    assert len(manual_routes(allstate)) == 1


# -- dry-run & email content ---------------------------------------------------------------

def test_dry_run_sends_nothing_and_mutates_nothing(tmp_path):
    worker, payload, mails, _ = make_worker(tmp_path)
    evidence = run_worker(worker, payload, dry_run=True)
    assert evidence["succeeded"]
    assert mails == []
    assert not Path(payload["carrier_store_path"]).exists()
    assert payload["followup_store"].events == []
    assert len(evidence["receipts"]) == 1
    assert evidence["receipts"][0]["note"] == "dry-run: not sent"


def test_dry_run_store_never_persists(tmp_path):
    store = DryRunCarrierContactStore(str(tmp_path / "cc.json"))
    store.record_contact("k", "e@x.com", TODAY)
    store.save()
    assert not (tmp_path / "cc.json").exists()


def test_carrier_email_signs_robie_and_names_policy():
    item = {"Account Name": "SAPP Construction Corp", "Policy Number": "S 2391821",
            "Line Of Business": "Commercial Pkg", "Effective Date": "2025-12-10",
            "created_date": "2026-08-17", "age_days": 41,
            "change_description": "Add 2024 Ford F-150"}
    subject, text, html_body = build_carrier_email(item, {"email": "x@y.com"}, TODAY)
    assert "S 2391821" in subject
    assert "SAPP Construction Corp" in text
    assert "Add 2024 Ford F-150" in text
    assert text.rstrip().endswith("-Robie\nStreetSmart Insurance")
    assert "Roby" not in text and "Roby" not in html_body


def test_non_open_rows_ignored(tmp_path):
    rows = [row(status="Complete")]
    worker, payload, mails, _ = make_worker(tmp_path, rows=rows)
    evidence = run_worker(worker, payload)
    assert evidence["summary"]["open_queue_rows"] == 0
    assert mails == []


def test_eligibility_contract():
    store = CarrierContactStore("/tmp/does-not-matter.json")
    item = {"age_days": 41}
    assert eligibility(item, key="k", today=TODAY, nag_dates={},
                       carrier_store=store)["action"] == "skip"
    assert eligibility(item, key="k", today=TODAY,
                       nag_dates={"k": (TODAY - timedelta(days=2)).isoformat()},
                       carrier_store=store)["action"] == "skip"
    assert eligibility(item, key="k", today=TODAY,
                       nag_dates={"k": (TODAY - timedelta(days=10)).isoformat()},
                       carrier_store=store)["action"] == "email"
