"""Tests for robie_job_engine/policy_change_liveness_health.py — fakes only, no network."""

from datetime import date

import pytest

from robie_job_engine.overdue_policy_change_reports import PolicyChangeReportContractError
from robie_job_engine.policy_change_liveness_health import (
    check_alias_targets,
    check_held_rows,
    format_alert,
    load_held_rows,
    run_health_check,
)

TODAY = date(2026, 9, 29)


def live_row(number, account="48641902"):
    return {"policyNumber": number, "accountId": account, "policyStatus": "Active",
            "expirationDate": "2027-01-28"}


def dead_row(number, account="21587960"):
    return {"policyNumber": number, "accountId": account, "policyStatus": "Deleted",
            "expirationDate": "2019-09-25"}


def search_for(mapping):
    def search(number):
        return [dict(r) for r in mapping.get(number, [])]
    return search


ALIASES = {"275768": "WC5-33S-B276B9-026", "WC 104180 01": "WC PI 2739418-001"}


def healthy_search():
    return search_for({
        "WC5-33S-B276B9-026": [live_row("WC5-33S-B276B9-026")],
        "WC533SB276B9026": [live_row("WC5-33S-B276B9-026")],
        "WC PI 2739418-001": [live_row("WC PI 2739418-001", account="88789196")],
        "WCPI2739418001": [live_row("WC PI 2739418-001", account="88789196")],
    })


def held_rows():
    return [
        {"account_name": "Nicholas & Erica Infantolino", "applicant_id": "37717821",
         "policy_number": "SPD-0000132", "reason": "no results"},
        {"account_name": "Darlin Fernandez", "applicant_id": "33729805",
         "policy_number": "UJH 6139546 01 29", "reason": "no results"},
    ]


# -- alias checks -------------------------------------------------------------


def test_alias_check_healthy_is_silent():
    assert check_alias_targets(healthy_search(), ALIASES, TODAY) == []


def test_alias_check_rotted_target_is_a_finding():
    rotted = search_for({"WC PI 2739418-001": [dead_row("WC PI 2739418-001", "88789196")]})
    findings = check_alias_targets(rotted, {"WC 104180 01": "WC PI 2739418-001"}, TODAY)
    assert len(findings) == 1
    assert "WC 104180 01" in findings[0] and "WC PI 2739418-001" in findings[0]
    assert "no longer points at a live policy" in findings[0]


def test_alias_check_missing_target_is_a_finding():
    findings = check_alias_targets(search_for({}), {"275768": "WC5-33S-B276B9-026"}, TODAY)
    assert len(findings) == 1
    assert "275768" in findings[0]


def test_alias_check_search_error_fails_closed():
    def boom(number):
        raise RuntimeError("api down")
    findings = check_alias_targets(boom, {"275768": "WC5-33S-B276B9-026"}, TODAY)
    assert len(findings) == 1
    assert "could not be checked" in findings[0]


def test_alias_check_matching_anchor_is_silent():
    anchors = {"275768": "48641902", "WC 104180 01": "88789196"}
    assert check_alias_targets(healthy_search(), ALIASES, TODAY, anchors) == []


def test_alias_check_wrong_account_target_is_a_finding():
    # The target resolves to a live policy, but on a different account than
    # the recorded applicant: the number was reissued — a finding, in plain
    # English, naming both accounts.
    moved = search_for({
        "WC5-33S-B276B9-026": [live_row("WC5-33S-B276B9-026", account="00000000")],
    })
    findings = check_alias_targets(
        moved, {"275768": "WC5-33S-B276B9-026"}, TODAY, {"275768": "48641902"})
    assert len(findings) == 1
    assert "275768" in findings[0] and "WC5-33S-B276B9-026" in findings[0]
    assert "00000000" in findings[0] and "48641902" in findings[0]
    assert "not the verified account" in findings[0]


def test_alias_check_anchor_absent_keeps_legacy_behavior():
    # Without anchors the check behaves exactly as before: a live row on any
    # account is healthy.
    moved = search_for({
        "WC5-33S-B276B9-026": [live_row("WC5-33S-B276B9-026", account="00000000")],
    })
    assert check_alias_targets(moved, {"275768": "WC5-33S-B276B9-026"}, TODAY) == []


# -- held-row checks -----------------------------------------------------------


def test_held_rows_still_holding_is_silent():
    assert check_held_rows(healthy_search(), held_rows(), {}, TODAY) == []


def test_held_row_now_live_is_a_finding():
    now_live = search_for({"SPD-0000132": [live_row("SPD-0000132", account="37717821")]})
    findings = check_held_rows(now_live, [held_rows()[0]], {}, TODAY)
    assert len(findings) == 1
    assert "LIVE" in findings[0] and "SPD-0000132" in findings[0]


def test_held_row_missing_ids_is_a_finding():
    findings = check_held_rows(healthy_search(), [{"account_name": "X"}], {}, TODAY)
    assert len(findings) == 1
    assert "cannot be re-checked" in findings[0]


def test_held_row_with_anchored_alias_now_live_is_a_finding():
    # Anchors pass through to the worker's classification: the held alias
    # row now resolves LIVE on the verified account.
    now_live = search_for({
        "WC5-33S-B276B9-026": [live_row("WC5-33S-B276B9-026")],
    })
    held = [{"account_name": "Shoreline Builders LLC", "applicant_id": "48641902",
             "policy_number": "275768", "reason": "held"}]
    findings = check_held_rows(now_live, held,
                               {"275768": "WC5-33S-B276B9-026"}, TODAY,
                               {"275768": "48641902"})
    assert len(findings) == 1
    assert "LIVE" in findings[0] and "275768" in findings[0]


# -- evidence loading ----------------------------------------------------------


def test_load_held_rows_missing_file_fails_closed(tmp_path):
    with pytest.raises(PolicyChangeReportContractError, match="evidence file not found"):
        load_held_rows(str(tmp_path / "nope.json"))


def test_load_held_rows_reads_held(tmp_path):
    path = tmp_path / "evidence.json"
    path.write_text('{"held": [{"policy_number": "X"}], "due_for_nag": []}')
    assert load_held_rows(str(path)) == [{"policy_number": "X"}]


# -- end to end -----------------------------------------------------------------


def test_run_health_check_healthy():
    healthy, findings = run_health_check(healthy_search(), ALIASES, held_rows(), TODAY)
    assert healthy and findings == []


def test_run_health_check_broken():
    rotted = search_for({"WC PI 2739418-001": [dead_row("WC PI 2739418-001", "88789196")]})
    healthy, findings = run_health_check(rotted, ALIASES, held_rows(), TODAY)
    assert not healthy
    message = format_alert(findings)
    assert "4359 policy-change liveness check" in message
    assert "WC 104180 01" in message


def test_main_alert_posts_findings_to_health_chat(tmp_path, monkeypatch):
    import robie_job_engine.chat_app_post as chat_app_post
    import robie_job_engine.policy_change_liveness_health as health

    posted = []

    def fake_post(space_name, text):
        posted.append((space_name, text))
        return {"name": "spaces/xxx/messages/yyy"}

    monkeypatch.setattr(chat_app_post, "post_as_chat_app", fake_post)
    monkeypatch.setenv("ROBIE_HEALTH_CHAT_SPACE", "spaces/TESTSPACE")

    evidence = tmp_path / "evidence.json"
    evidence.write_text('{"held": [{"account_name": "SAPP Construction Corp", '
                        '"applicant_id": "41055091", "policy_number": "S  2391821", '
                        '"reason": "held"}]}')
    aliases = tmp_path / "aliases.json"
    aliases.write_text('{"275768": "WC5-33S-B276B9-026"}')
    # Fake the policy search: SAPP's number is LIVE now, so the held row is a mis-hold.
    monkeypatch.setattr(
        health, "default_policy_search",
        search_for({"S  2391821": [dict(policyNumber="S  2391821", accountId="41055091",
                                        policyStatus="Active", expirationDate="2027-12-10")]}),
    )
    rc = health.main(["--alert", "--evidence", str(evidence), "--aliases", str(aliases)])
    assert rc == 1
    assert len(posted) == 1
    space_name, text = posted[0]
    assert space_name == "spaces/TESTSPACE"
    assert "SAPP Construction Corp" in text and "LIVE" in text


def test_main_healthy_is_quiet_no_post(tmp_path, monkeypatch):
    import robie_job_engine.chat_app_post as chat_app_post
    import robie_job_engine.policy_change_liveness_health as health

    posted = []
    monkeypatch.setattr(chat_app_post, "post_as_chat_app",
                        lambda space_name, text: posted.append((space_name, text)))
    monkeypatch.setenv("ROBIE_HEALTH_CHAT_SPACE", "spaces/TESTSPACE")

    evidence = tmp_path / "evidence.json"
    evidence.write_text('{"held": []}')
    aliases = tmp_path / "aliases.json"
    aliases.write_text('{"275768": "WC5-33S-B276B9-026"}')
    monkeypatch.setattr(
        health, "default_policy_search",
        search_for({"WC5-33S-B276B9-026": [dict(policyNumber="WC5-33S-B276B9-026",
                                                accountId="48641902", policyStatus="Active",
                                                expirationDate="2027-01-28")],
                    "WC533SB276B9026": []}),
    )
    rc = health.main(["--alert", "--evidence", str(evidence), "--aliases", str(aliases)])
    assert rc == 0
    assert posted == []
