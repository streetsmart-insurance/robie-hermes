from datetime import date

from robie_job_engine.department_accountability import (
    department_totals,
    parse_overdue_activity_detail,
    rank_overdue_offenders,
)


def test_overdue_tasks_are_deduplicated_by_task_id_and_roster_department():
    source = """Applicant ID,Account Name,Task Assigned To,Task Status,Task Due Date,Task Created Date,Task Last Modified Date,Created Date,Task ID,Task Priority,Note,Department
A-1,Example LLC,Jazmin Molina,Open,08/20/2026,08/01/2026,08/25/2026,08/24/2026,T-1,High,Old note,
A-1,Example LLC,Jazmin Molina,Open,08/20/2026,08/01/2026,08/29/2026,08/29/2026,T-1,High,Newest note,
A-2,Closed LLC,Jazmin Molina,Closed,08/10/2026,08/01/2026,08/29/2026,08/29/2026,T-2,Normal,Done,
"""
    tasks = parse_overdue_activity_detail(
        source,
        as_of=date(2026, 8, 31),
        employee_departments={"Jazmin Molina": "Personal Lines"},
    )
    assert len(tasks) == 1
    assert tasks[0].task_id == "T-1"
    assert tasks[0].latest_note == "Newest note"
    assert tasks[0].overdue_days == 11
    assert tasks[0].department == "Personal Lines"
    assert tasks[0].workstream_department == "UNVERIFIED"

    offenders = rank_overdue_offenders(tasks)
    assert offenders[0].owner == "Jazmin Molina"
    assert offenders[0].overdue_count == 1
    assert department_totals(tasks)[0]["overdue_tasks"] == 1
