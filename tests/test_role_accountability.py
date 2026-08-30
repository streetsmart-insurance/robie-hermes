from robie_job_engine.role_accountability import build_role_rows, normalize_role


def test_producer_is_not_graded_on_inbound_queue_answer_rate():
    rows = build_role_rows(
        {"Karla Producer": {"role": "Sales Producer", "email": "karla@example.com"}},
        call_data={"source_status": "available", "employee_rows": [{"employee": "Karla Producer", "calls_presented": 1, "answered": 0, "outbound": 2, "unreturned": 1}]},
        task_data={"source_status": "available", "overdue_by_rep": {}},
        sales_data={"source_status": "available", "exceptions": []},
        email_data={"source_status": "available", "by_employee": {"karla@example.com": {"stalled_threads": 0}}},
        magellan_data={"source_status": "available", "by_employee": {"Karla Producer": {"score": 98, "call_count": 2}}},
    )
    assert normalize_role("Sales Producer") == "producer"
    facts = " | ".join(rows[0]["facts"])
    assert "outbound calls: 2" in facts
    assert "answered" not in facts
    assert "INSUFFICIENT_SAMPLE" in facts


def test_service_role_uses_phone_tasks_and_email_evidence():
    rows = build_role_rows(
        {"Jackie": {"role": "Commercial Lines Account Manager", "email": "jackie@example.com"}},
        call_data={"source_status": "available", "employee_rows": [{"employee": "Jackie", "calls_presented": 8, "answered": 0, "outbound": 0, "unreturned": 8}]},
        task_data={"source_status": "available", "overdue_by_rep": {"Jackie": 4}},
        sales_data={"source_status": "available", "exceptions": []},
        email_data={"source_status": "available", "by_employee": {"jackie@example.com": {"stalled_threads": 3}}},
        magellan_data={"source_status": "not supplied"},
    )
    facts = " | ".join(rows[0]["facts"])
    assert "0/8 answered, 8 unreturned" in facts
    assert "overdue EZLynx tasks: 4" in facts
    assert "email awaiting employee >24h: 3" in facts


def test_missing_sales_evidence_is_not_reported_as_zero():
    rows = build_role_rows(
        {"Karla": {"role": "Producer"}},
        call_data={"source_status": "not supplied", "employee_rows": []},
        task_data={"source_status": "not supplied", "overdue_by_rep": {}},
        sales_data={"source_status": "not supplied"},
        email_data={"source_status": "not supplied"},
        magellan_data={"source_status": "not supplied"},
    )
    assert "inactive sales accounts: UNVERIFIED" in rows[0]["facts"]
