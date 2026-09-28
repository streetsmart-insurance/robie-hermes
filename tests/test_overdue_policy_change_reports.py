"""Tests for robie_job_engine/overdue_policy_change_reports.py — fake clients, no network, no secrets."""

from datetime import date

import pytest

from robie_job_engine.overdue_policy_change_reports import (
    ACTION,
    CC_CARLO,
    JOB_TYPE,
    RENAG_DAYS,
    NotificationStore,
    OverduePolicyChangeReportVerifier,
    OverduePolicyChangeReportWorker,
    PolicyChangeReportContractError,
    apply_exclusions,
    build_csr_report,
    build_csr_report_html,
    build_roster_maps,
    classify_policy_liveness,
    deconcatenated_variants,
    discussion_context_line,
    load_exclusions,
    load_producer_fallbacks,
    default_producer_fallbacks_path,
    parse_4359_csv,
    qualify_rows,
    resolve_nag_targets,
    select_change_discussion,
)
from robie_job_engine.models import JobStatus

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


# -- CSV parsing ---------------------------------------------------------------


def test_parse_valid_csv():
    rows = parse_4359_csv(csv_bytes(row(), row(policy="275768")))
    assert len(rows) == 2
    assert rows[0]["Policy Number"] == "S 2391821"
    assert rows[0]["CSR"] == "Eimy Ramos"


def test_parse_missing_column_raises():
    bad = HEADER.replace(",CSR,", ",")
    with pytest.raises(PolicyChangeReportContractError, match="missing required columns"):
        parse_4359_csv((bad + "\n" + row() + "\n").encode("utf-8"))


def test_parse_ragged_row_raises():
    with pytest.raises(PolicyChangeReportContractError, match="expected 19"):
        parse_4359_csv(csv_bytes(row() + ",EXTRA"))


def test_parse_empty_raises():
    with pytest.raises(PolicyChangeReportContractError):
        parse_4359_csv(b"")


# -- qualification --------------------------------------------------------------


def test_qualify_open_over_14_days():
    rows = parse_4359_csv(csv_bytes(row()))  # 41 days old
    qualified = qualify_rows(rows, TODAY)
    assert len(qualified) == 1
    assert qualified[0]["age_days"] == 41


def test_qualify_skips_fresh_and_closed():
    rows = parse_4359_csv(csv_bytes(row(created="2026-09-20"), row(policy="X2", status="Closed")))
    assert qualify_rows(rows, TODAY) == []


def test_qualify_blank_created_date_raises():
    rows = parse_4359_csv(csv_bytes(row(created="")))
    with pytest.raises(PolicyChangeReportContractError, match="blank Change Request Created Date"):
        qualify_rows(rows, TODAY)


def test_qualify_14_days_is_not_overdue():
    rows = parse_4359_csv(csv_bytes(row(created="2026-09-13")))  # exactly 14 days
    assert qualify_rows(rows, TODAY) == []


# -- liveness gate ---------------------------------------------------------------


def fake_search(mapping):
    def search(number):
        return [dict(r) for r in mapping.get(number, [])]
    return search


def policy_row(number, account="41055091", status="Active", expiration="2026-12-10"):
    return {"policyNumber": number, "accountId": account, "policyStatus": status,
            "expirationDate": expiration, "premium": 28936.0}


def test_liveness_exact_active_account_match():
    search = fake_search({"S 2391821": [policy_row("S 2391821")]})
    verdict = classify_policy_liveness(search, "S  2391821", "41055091", TODAY)
    assert verdict["verdict"] == "LIVE"


def test_liveness_exact_deleted_is_dead():
    search = fake_search({"OLD123": [policy_row("OLD123", status="Deleted")]})
    verdict = classify_policy_liveness(search, "OLD123", "41055091", TODAY)
    assert verdict["verdict"] == "DEAD"


def test_liveness_only_deleted_history_variants_is_dead():
    search = fake_search({
        "CX132093": [
            policy_row("CX132093_15195201", status="Deleted"),
            policy_row("CX132093_14083034", status="Deleted"),
        ]
    })
    verdict = classify_policy_liveness(search, "CX132093", "41055091", TODAY)
    assert verdict["verdict"] == "DEAD"


def test_liveness_expired_is_dead():
    search = fake_search({"OLD123": [policy_row("OLD123", expiration="2020-01-01")]})
    verdict = classify_policy_liveness(search, "OLD123", "41055091", TODAY)
    assert verdict["verdict"] == "DEAD"


def test_liveness_no_results_is_hold():
    verdict = classify_policy_liveness(fake_search({}), "275768", "999", TODAY)
    assert verdict["verdict"] == "HOLD"
    assert "no results" in verdict["reason"]


def test_liveness_account_mismatch_is_hold_not_live():
    search = fake_search({"S 2391821": [policy_row("S 2391821", account="DIFFERENT")]})
    verdict = classify_policy_liveness(search, "S 2391821", "41055091", TODAY)
    assert verdict["verdict"] == "HOLD"


def test_liveness_variant_fallback_finds_live_policy():
    search = fake_search({
        "BDG-312624001": [policy_row("BDG-312624001_73827270", account="82861889", status="Deleted")],
        "BDG-3126240-01": [],
        "BDG-3126240-02": [policy_row("BDG-3126240-02", account="82861889")],
    })
    verdict = classify_policy_liveness(search, "BDG-312624001", "82861889", TODAY)
    assert verdict["verdict"] == "LIVE"
    assert verdict["matched_policy_number"] == "BDG-3126240-02"


def test_deconcatenated_variants():
    assert deconcatenated_variants("BDG-312624001") == ["BDG-3126240-01", "BDG-3126240-02"]
    assert deconcatenated_variants("04283052") == []
    assert deconcatenated_variants("13WECAT1F8T") == []


# -- discussion context ----------------------------------------------------------


def disc(title, count, last):
    return {"title": title, "noteCount": count, "lastModified": last, "mostRecentNoteId": "1"}


def test_select_discussion_prefers_pcr_title_over_digit_match():
    # Carlo 2026-09-27: a true PCR thread beats an unrelated thread that
    # merely mentions the policy number.
    discussions = [
        disc("Re: S 2391821 endorsement", 8, "2026-09-25"),
        disc("Commercial Auto Policy Change Request - CHANGE ME", 3, "2026-09-20"),
    ]
    picked = select_change_discussion(discussions, "S 2391821")
    assert picked["noteCount"] == 3


def test_select_discussion_prefers_pcr_title_with_digits():
    discussions = [
        disc("Workers Compensation Policy Change Request - general", 2, "2026-09-26"),
        disc("Workers Compensation Policy Change Request - 13WECAT1F8T", 6, "2026-09-14"),
    ]
    picked = select_change_discussion(discussions, "13WECAT1F8T")
    assert picked["noteCount"] == 6


def test_select_discussion_falls_back_to_pcr_title():
    discussions = [disc("Commercial Auto Policy Change Request - CHANGE ME", 3, "2026-09-20")]
    assert select_change_discussion(discussions, "999999")["noteCount"] == 3


def test_select_discussion_rejects_renewal_thread_with_digits():
    # ISCA 2026-09-27: "Renewal Request for: BDG-312624001" is a renewal
    # thread, not the change request — never present it as one.
    discussions = [disc("Renewal Request for: BDG-312624001", 1, "2026-06-22")]
    assert select_change_discussion(discussions, "BDG-312624001", date(2026, 8, 21)) is None


def test_select_discussion_rejects_stale_digit_thread():
    # Guarini 2026-09-27: "04283052-0" last active 1,735 days ago cannot be
    # the thread for a request opened 2026-08-31.
    discussions = [disc("04283052-0", 1, "2021-12-27")]
    assert select_change_discussion(discussions, "04283052", date(2026, 8, 31)) is None


def test_select_discussion_keeps_fresh_digit_fallback():
    # A fresh, non-renewal digit match is still usable context.
    discussions = [disc("Re: S 2391821 endorsement", 8, "2026-09-25")]
    picked = select_change_discussion(discussions, "S 2391821", date(2026, 8, 17))
    assert picked["noteCount"] == 8


def test_select_discussion_none_when_no_match():
    assert select_change_discussion([disc("Renewal docs", 2, "2026-09-01")], "S 2391821") is None


def test_context_line_reports_activity():
    line = discussion_context_line(disc("PCR", 8, "2026-09-25T10:00:00"), TODAY)
    assert "8 notes" in line and "2 days ago" in line
    one = discussion_context_line(disc("PCR", 1, "2026-09-25T10:00:00"), TODAY)
    assert "1 note," in one and "1 notes" not in one


def test_context_line_no_notes_and_no_discussion():
    assert "no notes yet" in discussion_context_line(disc("PCR", 0, ""), TODAY)
    assert "No EZLynx discussion found" in discussion_context_line(None, TODAY)


# -- roster ----------------------------------------------------------------------


def registry():
    return {
        "source_status": "available",
        "employees": {
            "Eimy Ramos": {"role": "Commercial Lines Account Technician",
                           "email": "eimy@streetsmart.insurance",
                           "department": "Commercial Lines", "manager": "", "status": "Active"},
            "Sandy Santana": {"role": "Commercial Lines Department Manager",
                              "email": "sandy@streetsmart.insurance",
                              "department": "Commercial Lines", "manager": "", "status": "Active"},
        },
    }


def test_build_roster_maps():
    maps = build_roster_maps(registry())
    assert maps["directory"]["eimy ramos"] == "eimy@streetsmart.insurance"
    assert maps["managers"]["commercial lines"]["email"] == "sandy@streetsmart.insurance"


def test_build_roster_maps_rejects_unavailable():
    with pytest.raises(PolicyChangeReportContractError, match="unavailable"):
        build_roster_maps({"source_status": "missing", "employees": {}})


def test_build_roster_maps_rejects_ambiguous_name():
    reg = registry()
    reg["employees"]["Eimy  Ramos"] = reg["employees"]["Eimy Ramos"]
    with pytest.raises(PolicyChangeReportContractError, match="ambiguous"):
        build_roster_maps(reg)


def test_resolve_targets_groups_by_csr_with_manager():
    items = [{"CSR": "Eimy Ramos", "Policy Number": "S 2391821"}]
    targets, unresolved_csrs, unresolved_managers = resolve_nag_targets(items, build_roster_maps(registry()))
    assert targets["Eimy Ramos"]["email"] == "eimy@streetsmart.insurance"
    assert targets["Eimy Ramos"]["manager_email"] == "sandy@streetsmart.insurance"


def test_resolve_targets_reports_unknown_csr_without_blocking():
    # A CSR that does not resolve (terminated employee still named on the
    # queue, e.g. Cesar Romero 2026-09-27) is reported, not raised: the rest
    # of the team still gets their emails.
    items = [{"CSR": "Eimy Ramos", "Policy Number": "S 2391821"},
             {"CSR": "Nobody Here", "Policy Number": "13WECAT1F8T"}]
    targets, unresolved_csrs, unresolved_managers = resolve_nag_targets(items, build_roster_maps(registry()))
    assert unresolved_csrs == ["Nobody Here"]
    assert set(targets) == {"Eimy Ramos"}
    assert targets["Eimy Ramos"]["email"] == "eimy@streetsmart.insurance"


def test_resolve_targets_reports_missing_manager_without_blocking():
    # A CSR whose department has no Department Manager in the roster still
    # gets their email (Carlo is always CC'd); the gap is reported.
    reg = registry()
    del reg["employees"]["Sandy Santana"]
    items = [{"CSR": "Eimy Ramos", "Policy Number": "S 2391821"}]
    targets, unresolved_csrs, unresolved_managers = resolve_nag_targets(
        items, build_roster_maps(reg))
    assert unresolved_csrs == []
    assert unresolved_managers == ["Eimy Ramos (department: Commercial Lines)"]
    assert targets["Eimy Ramos"]["email"] == "eimy@streetsmart.insurance"
    assert targets["Eimy Ramos"]["manager_name"] == ""
    assert targets["Eimy Ramos"]["manager_email"] == ""


def test_build_csr_report_without_manager_name():
    # The CC line stays grammatical when there is no department manager.
    body = build_csr_report("Eimy Ramos", [], "", date(2026, 9, 27))
    assert "CC'ing  " not in body
    assert "Looping in the team" in body
    body2 = build_csr_report("Eimy Ramos", [], "", date(2026, 9, 27),
                             cc_names=["", "Taylor Cimei"])
    assert "CC'ing Taylor Cimei so they're in the loop." in body2


def test_csr_report_signs_as_robie():
    # The worker persona is "Robie" — never "Roby" (that's Jake's assistant).
    body = build_csr_report("Eimy Ramos", [], "", date(2026, 9, 27))
    assert body.rstrip().endswith("-Robie")
    assert "-Roby" not in body
    html_body = build_csr_report_html("Eimy Ramos", [], "", date(2026, 9, 27))
    assert "<p>-Robie</p>" in html_body
    assert "-Roby" not in html_body


def test_resolve_targets_collects_distinct_producer():
    reg = registry()
    reg["employees"]["Taylor Cimei"] = {
        "role": "Producer", "email": "taylor@streetsmart.insurance",
        "department": "Commercial Lines", "manager": "", "status": "Active"}
    items = [{"CSR": "Eimy Ramos", "Policy Number": "S 2391821",
              "Assigned Producer": "Taylor Cimei"}]
    targets, unresolved_csrs, unresolved_managers = resolve_nag_targets(items, build_roster_maps(reg))
    target = targets["Eimy Ramos"]
    assert target["producer_emails"] == ["taylor@streetsmart.insurance"]
    assert target["producer_names"] == ["Taylor Cimei"]


def test_resolve_targets_unknown_producer_does_not_block():
    items = [{"CSR": "Eimy Ramos", "Policy Number": "S 2391821",
              "Assigned Producer": "Nobody Here"}]
    targets, unresolved_csrs, unresolved_managers = resolve_nag_targets(items, build_roster_maps(registry()))
    target = targets["Eimy Ramos"]
    assert target["email"] == "eimy@streetsmart.insurance"
    assert target["producer_emails"] == []
    assert target["unresolved_producers"] == ["Nobody Here"]


def test_resolve_targets_uses_producer_fallback(tmp_path):
    # A producer missing from the AppSheet roster resolves via the checked-in
    # fallback file, is CC'd, and the use is reported — not silently dropped.
    fb = tmp_path / "fallbacks.json"
    fb.write_text('{"fallbacks": [{"name": "Andrea Illanes", '
                   '"email": "andrea@streetsmart.insurance"}]}')
    reg = registry()
    reg["employees"]["Lenin Perdomo"] = {
        "role": "Commercial Lines Account Technician",
        "email": "lenin@streetsmart.insurance",
        "department": "Commercial Lines", "manager": "", "status": "Active"}
    items = [{"CSR": "Lenin Perdomo", "Policy Number": "13WECAT1F8T",
              "Assigned Producer": "Andrea Illanes"}]
    fallbacks = load_producer_fallbacks(fb)
    targets, unresolved_csrs, unresolved_managers = resolve_nag_targets(items, build_roster_maps(reg), fallbacks)
    target = targets["Lenin Perdomo"]
    assert target["producer_emails"] == ["andrea@streetsmart.insurance"]
    assert target["producer_names"] == ["Andrea Illanes"]
    assert target["producer_fallback_used"] == ["Andrea Illanes"]
    assert target["unresolved_producers"] == []


def test_resolve_targets_resolves_first_last_alias():
    # The 4359 queue says "Andrea Illanes"; the roster lists
    # "Andrea Nicole Illanes". The unambiguous first+last alias resolves.
    reg = registry()
    reg["employees"]["Andrea Nicole Illanes"] = {
        "role": "Commercial Lines Account Manager",
        "email": "andrea@streetsmart.insurance",
        "department": "Commercial Lines", "manager": "", "status": "Active"}
    reg["employees"]["Lenin Perdomo"] = {
        "role": "Commercial Lines Account Technician",
        "email": "lenin@streetsmart.insurance",
        "department": "Commercial Lines", "manager": "", "status": "Active"}
    items = [{"CSR": "Lenin Perdomo", "Policy Number": "13WECAT1F8T",
              "Assigned Producer": "Andrea Illanes"}]
    targets, unresolved_csrs, unresolved_managers = resolve_nag_targets(items, build_roster_maps(reg))
    target = targets["Lenin Perdomo"]
    assert target["producer_emails"] == ["andrea@streetsmart.insurance"]
    assert target["producer_fallback_used"] == []
    assert target["unresolved_producers"] == []


def test_build_roster_maps_skips_corporate_alias():
    # "Streetsmart Risk Managers Inc." is an entity row in the roster, not a
    # person: no "streetsmart inc." alias may be generated for it.
    reg = registry()
    reg["employees"]["Streetsmart Risk Managers Inc."] = {
        "role": "Operations Technicians", "email": "hello@streetsmart.insurance",
        "department": "Operations", "manager": "", "status": "Active"}
    maps = build_roster_maps(reg)
    assert "streetsmart inc." not in maps["directory"]
    assert "streetsmart risk managers inc." in maps["directory"]


def test_resolve_targets_skips_ambiguous_alias():
    # Two different "Andrea ... Illanes" -> no alias, producer unresolved
    # rather than CC'ing the wrong person.
    reg = registry()
    for full in ("Andrea Nicole Illanes", "Andrea Marie Illanes"):
        reg["employees"][full] = {
            "role": "Commercial Lines Account Manager",
            "email": f"{full.split()[1].lower()}@streetsmart.insurance",
            "department": "Commercial Lines", "manager": "", "status": "Active"}
    reg["employees"]["Lenin Perdomo"] = {
        "role": "Commercial Lines Account Technician",
        "email": "lenin@streetsmart.insurance",
        "department": "Commercial Lines", "manager": "", "status": "Active"}
    items = [{"CSR": "Lenin Perdomo", "Policy Number": "13WECAT1F8T",
              "Assigned Producer": "Andrea Illanes"}]
    targets, unresolved_csrs, unresolved_managers = resolve_nag_targets(items, build_roster_maps(reg))
    target = targets["Lenin Perdomo"]
    assert target["producer_emails"] == []
    assert target["unresolved_producers"] == ["Andrea Illanes"]


def test_load_producer_fallbacks_missing_file_returns_empty(tmp_path):
    assert load_producer_fallbacks(tmp_path / "nope.json") == {}


def test_load_producer_fallbacks_corrupt_file_fails_closed(tmp_path):
    bad = tmp_path / "fallbacks.json"
    bad.write_text("{not json")
    with pytest.raises(PolicyChangeReportContractError, match="not valid JSON"):
        load_producer_fallbacks(bad)


def test_load_producer_fallbacks_rejects_non_agency_email(tmp_path):
    bad = tmp_path / "fallbacks.json"
    bad.write_text('{"fallbacks": [{"name": "X", "email": "x@gmail.com"}]}')
    with pytest.raises(PolicyChangeReportContractError, match="not an agency email"):
        load_producer_fallbacks(bad)


# -- dedupe -----------------------------------------------------------------------


def test_notification_store_renag_after_7_days(tmp_path):
    store = NotificationStore(tmp_path / "sent.json")
    item = {"CSR": "Eimy Ramos", "Policy Number": "S 2391821", "created_date": "2026-08-17"}
    assert store.is_due(item, TODAY)
    store.mark_sent(item, TODAY)
    store.save()
    assert not NotificationStore(tmp_path / "sent.json").is_due(item, TODAY)
    assert NotificationStore(tmp_path / "sent.json").is_due(item, date(2026, 10, 4))


# -- worker ------------------------------------------------------------------------


def make_worker(tmp_path, **overrides):
    sent = []

    def fake_mailer(**kw):
        record = dict(kw)
        record["message_id"] = f"m{len(sent)}"
        sent.append(record)
        return record
    worker = OverduePolicyChangeReportWorker(
        queue_reader=overrides.get("queue_reader", lambda payload: parse_4359_csv(csv_bytes(row()))),
        policy_search=overrides.get("policy_search", fake_search({"S 2391821": [policy_row("S 2391821")]})),
        discussion_lookup=overrides.get(
            "discussion_lookup",
            lambda applicant_id: [disc("Commercial Auto Policy Change Request", 8, "2026-09-25T10:00:00")],
        ),
        directory_loader=overrides.get("directory_loader", lambda manifest: build_roster_maps(registry())),
        mailer=overrides.get("mailer", fake_mailer),
        sent_store=NotificationStore(tmp_path / "sent.json"),
    )
    return worker, sent


def job():
    return {"action_type": ACTION, "payload": {"manifest_path": "/tmp/manifest.json"}}


def test_worker_sends_one_email_per_csr(tmp_path):
    worker, sent = make_worker(tmp_path)
    result = worker.perform(job(), idempotency_key="k1")
    assert result.succeeded
    assert result.destination["csr_count"] == 1
    assert len(sent) == 1
    email = sent[0]
    assert email["to"] == ["eimy@streetsmart.insurance"]
    assert email["cc"] == ["sandy@streetsmart.insurance", CC_CARLO,
                           "jake@streetsmart.insurance",
                           "gabrielac@streetsmart.insurance"]
    assert "SAPP Construction Corp" in email["text_body"]
    assert "S 2391821" in email["text_body"]
    assert f"{(date.today() - date(2026, 8, 17)).days} days ago" in email["text_body"]
    assert "8 notes" in email["text_body"]


def test_worker_ccs_fixed_ccs_and_producer_after_them(tmp_path):
    # Carlo 2026-09-27: Jake, Gabby and Sandy are CC'd on every nag, after
    # the department manager and Carlo, before the assigned producer.
    reg = registry()
    reg["employees"]["Taylor Cimei"] = {
        "role": "Producer", "email": "taylor@streetsmart.insurance",
        "department": "Commercial Lines", "manager": "", "status": "Active"}
    worker, sent = make_worker(
        tmp_path,
        queue_reader=lambda payload: parse_4359_csv(csv_bytes(
            row(producer="Taylor Cimei"))),
        directory_loader=lambda manifest: build_roster_maps(reg),
    )
    result = worker.perform(job(), idempotency_key="k1")
    assert result.succeeded
    assert sent[0]["cc"] == ["sandy@streetsmart.insurance", CC_CARLO,
                             "jake@streetsmart.insurance",
                             "gabrielac@streetsmart.insurance",
                             "taylor@streetsmart.insurance"]
    cc_names_line = [line for line in sent[0]["text_body"].splitlines()
                     if line.startswith("CC'ing")]
    assert len(cc_names_line) == 1
    for name in ("Sandy Santana", "Jake Ferrara", "Gabriela Chutin",
                 "Taylor Cimei"):
        assert name in cc_names_line[0]
    assert cc_names_line[0].count("Sandy Santana") == 1


def test_worker_subject_matches_carlo_approved_nag(tmp_path):
    worker, sent = make_worker(tmp_path)
    worker.perform(job(), idempotency_key="k1")
    assert sent[0]["subject"] == "Overdue policy change requests need an update"


def test_csr_report_includes_sop_closure_gate():
    body = build_csr_report("Eimy Ramos", [], "Sandy Santana", date(2026, 9, 27))
    for point in (
        "carrier's endorsement or revised declarations page is received and filed",
        "compared field by field",
        "premium or billing impact is recorded, or confirmed as not applicable",
        "Marking a task complete does not close the change request itself",
    ):
        assert point in body
    html_body = build_csr_report_html("Eimy Ramos", [], "Sandy Santana",
                                      date(2026, 9, 27))
    for point in (
        "carrier&#x27;s endorsement or revised declarations page is received and filed",
        "compared field by field",
        "Marking a task complete does not close the change request itself",
    ):
        assert point in html_body


def test_worker_rerun_does_not_renag(tmp_path):
    worker, sent = make_worker(tmp_path)
    worker.perform(job(), idempotency_key="k1")
    result = worker.perform(job(), idempotency_key="k2")
    assert result.succeeded
    assert result.destination["due_for_nag"] == 0
    assert len(sent) == 1


def test_worker_excludes_dead_and_holds_noresult(tmp_path):
    rows = parse_4359_csv(csv_bytes(
        row(),
        row(account="Dead Co", policy="OLD1", applicant="111", csr="Eimy Ramos", created="2026-08-01"),
        row(account="Ghost Co", policy="NOPE", applicant="222", csr="Eimy Ramos", created="2026-08-01"),
    ))
    search = fake_search({
        "S 2391821": [policy_row("S 2391821")],
        "OLD1": [policy_row("OLD1", account="111", status="Deleted")],
    })
    worker, sent = make_worker(tmp_path, queue_reader=lambda payload: rows, policy_search=search)
    result = worker.perform(job(), idempotency_key="k1")
    assert result.succeeded
    assert result.destination["overdue_live"] == 1
    assert result.destination["overdue_dead_excluded"] == 1
    assert result.destination["on_hold"] == 1
    assert len(sent) == 1  # only the live item is emailed


def test_worker_skips_excluded_stale_rows(tmp_path):
    import json
    exclusions = tmp_path / "exclusions.json"
    exclusions.write_text(json.dumps({"exclusions": [{
        "policy_number": "S 2391821", "created_date": "2026-08-17",
        "reason": "stale queue row per Carlo"}]}))
    rows = parse_4359_csv(csv_bytes(row()))
    search = fake_search({"S 2391821": [policy_row("S 2391821")]})
    worker, sent = make_worker(tmp_path, queue_reader=lambda payload: rows,
                               policy_search=search)
    payload_job = {"action_type": ACTION,
                   "payload": {"manifest_path": "/tmp/manifest.json",
                               "exclusions_path": str(exclusions)}}
    result = worker.perform(payload_job, idempotency_key="k1")
    assert result.succeeded
    assert result.destination["excluded_stale"] == 1
    assert result.destination["overdue_live"] == 0
    assert len(sent) == 0


def test_worker_ccs_distinct_producer(tmp_path):
    reg = registry()
    reg["employees"]["Taylor Cimei"] = {
        "role": "Producer", "email": "taylor@streetsmart.insurance",
        "department": "Commercial Lines", "manager": "", "status": "Active"}
    rows = parse_4359_csv(csv_bytes(row(producer="Taylor Cimei")))
    worker, sent = make_worker(
        tmp_path, queue_reader=lambda payload: rows,
        directory_loader=lambda manifest: build_roster_maps(reg))
    result = worker.perform(job(), idempotency_key="k1")
    assert result.succeeded
    email = sent[0]
    assert email["cc"] == ["sandy@streetsmart.insurance", CC_CARLO,
                           "jake@streetsmart.insurance",
                           "gabrielac@streetsmart.insurance",
                           "taylor@streetsmart.insurance"]
    assert "CC'ing Sandy Santana and Jake Ferrara, Gabriela Chutin, Taylor Cimei" in email["text_body"]


def test_load_exclusions_missing_file_is_empty(tmp_path):
    assert load_exclusions(tmp_path / "nope.json") == set()


def test_load_exclusions_corrupt_fails_closed(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(PolicyChangeReportContractError, match="not valid JSON"):
        load_exclusions(bad)


def test_apply_exclusions_matches_normalized_policy_and_date():
    rows = [{"Policy Number": "S 2391821", "created_date": "2026-08-17"},
            {"Policy Number": "OTHER1", "created_date": "2026-08-01"}]
    kept, excluded = apply_exclusions(rows, {("S 2391821", "2026-08-17")})
    assert [r["Policy Number"] for r in kept] == ["OTHER1"]
    assert [r["Policy Number"] for r in excluded] == ["S 2391821"]


def test_worker_marks_unverified_on_discussion_failure(tmp_path):
    def boom(applicant_id):
        raise RuntimeError("socket exploded")

    worker, sent = make_worker(tmp_path, discussion_lookup=boom)
    result = worker.perform(job(), idempotency_key="k1")
    assert result.succeeded  # run continues...
    assert "UNVERIFIED" in sent[0]["text_body"]  # ...but the item is flagged


def test_worker_fails_closed_when_discussion_api_down(tmp_path):
    # default_discussion_lookup converts a DiscussionApi transport/auth
    # failure into the contract error -> the whole run fails closed.
    def api_down(applicant_id):
        raise PolicyChangeReportContractError("DiscussionApi lookup failed: transport failed")

    worker, sent = make_worker(tmp_path, discussion_lookup=api_down)
    result = worker.perform(job(), idempotency_key="k1")
    assert not result.succeeded
    assert result.hold_status == JobStatus.NEEDS_CLARIFICATION
    assert sent == []


def test_worker_fails_closed_on_roster_problem(tmp_path):
    def bad_loader(manifest):
        raise PolicyChangeReportContractError("roster unavailable")

    worker, sent = make_worker(tmp_path, directory_loader=bad_loader)
    result = worker.perform(job(), idempotency_key="k1")
    assert not result.succeeded
    assert sent == []


def test_worker_rejects_wrong_action(tmp_path):
    worker, sent = make_worker(tmp_path)
    result = worker.perform({"action_type": "something.else", "payload": {}}, idempotency_key="k1")
    assert not result.succeeded
    assert result.hold_status == JobStatus.NEEDS_CLARIFICATION
    assert sent == []


def test_worker_requires_manifest(tmp_path):
    worker, sent = make_worker(tmp_path)
    result = worker.perform({"action_type": ACTION, "payload": {}}, idempotency_key="k1")
    assert not result.succeeded
    assert sent == []


def test_email_body_is_plain_and_concise():
    items = [{
        "Account Name": "SAPP Construction Corp", "Policy Number": "S 2391821",
        "Master Company": "Selective Insurance", "Line Of Business": "Commercial Pkg",
        "created_date": "2026-08-17", "age_days": 41,
        "discussion": disc("Commercial Auto Policy Change Request", 8, "2026-09-25T10:00:00"),
    }]
    body = build_csr_report("Eimy Ramos", items, "Sandy Santana", TODAY)
    assert "Hi Eimy," in body
    assert "status update" in body
    assert "CC'ing Sandy Santana" in body
    assert "S 2391821" in body


def test_email_signs_as_robie():
    items = [{
        "Account Name": "SAPP Construction Corp", "Policy Number": "S 2391821",
        "Master Company": "Selective Insurance", "Line Of Business": "Commercial Pkg",
        "created_date": "2026-08-17", "age_days": 41,
        "discussion": disc("Commercial Auto Policy Change Request", 8, "2026-09-25T10:00:00"),
    }]
    body = build_csr_report("Eimy Ramos", items, "Sandy Santana", TODAY)
    assert body.rstrip().endswith("-Robie")
    assert "-Roby" not in body
    assert "on behalf of Carlo" not in body


def test_email_names_producer_in_cc_line():
    items = [{
        "Account Name": "SAPP Construction Corp", "Policy Number": "S 2391821",
        "Master Company": "Selective Insurance", "Line Of Business": "Commercial Pkg",
        "created_date": "2026-08-17", "age_days": 41,
        "discussion": disc("Commercial Auto Policy Change Request", 8, "2026-09-25T10:00:00"),
    }]
    body = build_csr_report("Eimy Ramos", items, "Sandy Santana", TODAY,
                            cc_names=["Sandy Santana", "Taylor Cimei"])
    assert "CC'ing Sandy Santana and Taylor Cimei so they're in the loop." in body


def test_bullet_includes_change_request_subject():
    items = [{
        "Account Name": "SAPP Construction Corp", "Policy Number": "S 2391821",
        "Master Company": "Selective Insurance", "Line Of Business": "Commercial Pkg",
        "created_date": "2026-08-17", "age_days": 41,
        "discussion": disc("Commercial Auto Policy Change Request - Add driver", 8,
                           "2026-09-25T10:00:00"),
    }]
    body = build_csr_report("Eimy Ramos", items, "Sandy Santana", TODAY)
    assert 'Change request: "Commercial Auto Policy Change Request - Add driver"' in body
    assert "8 notes" in body


# -- verifier -----------------------------------------------------------------------


def test_verifier_accepts_matching_receipts():
    verifier = OverduePolicyChangeReportVerifier(
        delivery_readback=lambda receipts: (True, [{"exists_in_sent_mailbox": True} for _ in receipts])
    )
    action = {"destination": {"delivery_receipts": [{"message_id": "m0"}], "csr_count": 1}}
    result = verifier.verify({}, action)
    assert result.verified


def test_verifier_rejects_count_mismatch():
    verifier = OverduePolicyChangeReportVerifier(
        delivery_readback=lambda receipts: (True, [{"exists_in_sent_mailbox": True}])
    )
    action = {"destination": {"delivery_receipts": [{"message_id": "m0"}], "csr_count": 2}}
    result = verifier.verify({}, action)
    assert not result.verified


def test_verifier_zero_case_with_no_receipts():
    verifier = OverduePolicyChangeReportVerifier()
    result = verifier.verify({}, {"destination": {"delivery_receipts": [], "csr_count": 0}})
    assert result.verified
