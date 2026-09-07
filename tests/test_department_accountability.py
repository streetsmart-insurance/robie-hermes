from datetime import date
import pytest

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


@pytest.mark.parametrize('new_status,new_due', [('Closed', '08/20/2026'), ('Open', '09/10/2026'), ('Open', '')])
@pytest.mark.parametrize('reverse', [False, True])
def test_latest_task_state_removes_stale_overdue_rows(new_status, new_due, reverse):
    header = 'Task ID,Task Status,Task Due Date,Task Last Modified Date,Created Date,Note\n'
    rows = [
        'T-1,Open,08/20/2026,08/25/2026,08/30/2026,Old task state',
        f'T-1,{new_status},{new_due},08/31/2026,08/24/2026,Updated task state',
    ]
    if reverse:
        rows.reverse()
    assert parse_overdue_activity_detail(header + '\n'.join(rows), as_of=date(2026, 9, 1)) == []


def test_reopened_task_is_counted_once_with_latest_discussion():
    source = '''Task ID,Task Status,Task Due Date,Task Last Modified Date,Created Date,Note
T-1,Closed,08/20/2026,08/25/2026,08/25/2026,Closed
T-1,Open,08/20/2026,08/31/2026,08/29/2026,Earlier discussion
T-1,Open,08/20/2026,08/31/2026,08/30/2026,Latest discussion
'''
    tasks = parse_overdue_activity_detail(source, as_of=date(2026, 9, 1))
    assert len(tasks) == 1
    assert tasks[0].latest_note == 'Latest discussion'
