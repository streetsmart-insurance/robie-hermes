from pathlib import Path


def test_employee_metadata_probe_avoids_search_filter():
    source = Path("scripts/diagnose-accountability-test-readonly.py").read_text(
        encoding="utf-8"
    )
    employee_block = source.split("if service_account and users:", 1)[1].split(
        "if dashboard.get", 1
    )[0]

    assert ".users().threads().list(" in employee_block
    assert "q=" not in employee_block
