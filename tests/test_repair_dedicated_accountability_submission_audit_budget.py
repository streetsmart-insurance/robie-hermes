from pathlib import Path

from scripts.repair_dedicated_accountability_submission_audit_budget import (
    IMPORT_NEW,
    IMPORT_OLD,
    LOOP_NEW,
    LOOP_OLD,
    SIG_NEW,
    SIG_OLD,
    repair_browser,
)


def _write(path: Path, *parts: str) -> Path:
    path.write_text("\n".join(parts), encoding="utf-8")
    path.chmod(0o640)
    return path


def test_repairs_browser_audit_budget_and_is_idempotent(tmp_path):
    path = _write(
        tmp_path / "ezlynx_submission_browser.py",
        IMPORT_OLD,
        "from typing import Any\n",
        SIG_OLD,
        "    page = None\n",
        "    positions = {'status': 0}\n",
        "    run_date = None\n",
        "    qualifying = []\n",
        "    dispositions = []\n",
        "    non_closed_statuses = []\n",
        "    pages_reviewed = 0\n",
        "    rows_inspected = 0\n",
        "    non_closed_inspected = 0\n",
        "    first_closed_page = None\n",
        "    first_closed_index = None\n",
        "    first_closed_status = ''\n",
        LOOP_OLD,
        "    return {}\n",
    )
    assert repair_browser(path) == "repaired"
    text = path.read_text(encoding="utf-8")
    assert IMPORT_NEW in text
    assert SIG_NEW.split("deadline_monotonic", 1)[0] in text
    assert "deadline_monotonic" in text
    assert "def _read_page_rows(" in text
    assert LOOP_NEW in text
    assert repair_browser(path) == "already_repaired"


def test_diagnose_workflow_wires_audit_budget_repair_and_deadline():
    workflow = Path(".github/workflows/diagnose-accountability-dedicated.yml").read_text(
        encoding="utf-8"
    )
    assert "repair_dedicated_accountability_submission_audit_budget.py" in workflow
    assert "--deadline-seconds 400" in workflow
    assert "PYTHONUNBUFFERED=1" in workflow


def test_repair_refuses_unexpected_source(tmp_path):
    import pytest

    path = _write(tmp_path / "browser.py", "unexpected")
    with pytest.raises(RuntimeError, match="unexpected import-time source shape"):
        repair_browser(path)
