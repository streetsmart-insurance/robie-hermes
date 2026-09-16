from pathlib import Path

import pytest

from scripts.repair_dedicated_accountability_recipients import (
    APPROVED_RECIPIENTS,
    repair,
)


def test_repairs_stale_recipient_list(tmp_path: Path):
    target = tmp_path / "email_delivery.py"
    target.write_text(
        'RECIPIENTS = [\n'
        '    "carlo@streetsmart.insurance",\n'
        '    "sandy@streetsmart.insurance",\n'
        ']\n',
        encoding="utf-8",
    )

    assert repair(target) == "repaired"
    updated = target.read_text(encoding="utf-8")
    for address in APPROVED_RECIPIENTS:
        assert updated.count(address) == 1
    assert "sandy@streetsmart.insurance" not in updated
    assert target.with_suffix(".py.pre-recipient-repair").is_file()
    assert repair(target) == "already_repaired"


def test_refuses_unknown_recipient_source(tmp_path: Path):
    target = tmp_path / "email_delivery.py"
    target.write_text("RECIPIENTS = []\nRECIPIENTS = []\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unexpected recipient source shape"):
        repair(target)
