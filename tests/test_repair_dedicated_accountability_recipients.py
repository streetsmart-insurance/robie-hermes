from pathlib import Path

import pytest

from scripts.repair_dedicated_accountability_recipients import (
    APPROVED_RECIPIENTS,
    repair,
)


def _load_recipients(source: str, monkeypatch: pytest.MonkeyPatch, override: str | None):
    if override is None:
        monkeypatch.delenv("ACCOUNTABILITY_DELIVERY_RECIPIENTS", raising=False)
    else:
        monkeypatch.setenv("ACCOUNTABILITY_DELIVERY_RECIPIENTS", override)
    namespace: dict[str, object] = {}
    exec(source, namespace)
    return namespace["RECIPIENTS"]


def test_repairs_stale_recipient_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
    assert _load_recipients(updated, monkeypatch, None) == list(APPROVED_RECIPIENTS)
    assert repair(target) == "already_repaired"


def test_allows_only_explicit_carlo_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    target = tmp_path / "email_delivery.py"
    target.write_text(
        'RECIPIENTS = ["carlo@streetsmart.insurance", "jake@streetsmart.insurance"]\n',
        encoding="utf-8",
    )
    assert repair(target) == "repaired"
    assert _load_recipients(
        target.read_text(encoding="utf-8"),
        monkeypatch,
        "carlo@streetsmart.insurance",
    ) == ["carlo@streetsmart.insurance"]


@pytest.mark.parametrize(
    "override",
    [
        "jake@streetsmart.insurance",
        "carlo@streetsmart.insurance,jake@streetsmart.insurance",
        "outside@example.com",
    ],
)
def test_refuses_every_other_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    override: str,
):
    target = tmp_path / "email_delivery.py"
    target.write_text(
        'RECIPIENTS = ["carlo@streetsmart.insurance", "jake@streetsmart.insurance"]\n',
        encoding="utf-8",
    )
    assert repair(target) == "repaired"
    with pytest.raises(RuntimeError, match="unapproved accountability recipient override"):
        _load_recipients(target.read_text(encoding="utf-8"), monkeypatch, override)


def test_refuses_unknown_recipient_source(tmp_path: Path):
    target = tmp_path / "email_delivery.py"
    target.write_text("RECIPIENTS = []\nRECIPIENTS = []\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unexpected recipient source shape"):
        repair(target)
