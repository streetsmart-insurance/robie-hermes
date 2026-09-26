from pathlib import Path

import pytest

from scripts.repair_dedicated_accountability_magellan_empty import (
    NEW_CALL,
    NEW_EXCEPT,
    NEW_FETCH,
    NEW_PUBLISH,
    NEW_REUSE,
    NEW_RETURN,
    NEW_SIGNATURE,
    OLD_CALL,
    OLD_EXCEPT,
    OLD_FETCH,
    OLD_PUBLISH,
    OLD_REUSE,
    OLD_RETURN,
    OLD_SIGNATURE,
    repair,
)


def _legacy_source() -> str:
    return (
        "from pathlib import Path\n"
        "from typing import Any, Dict\n"
        "import json\n"
        "\n"
        "class SourceGateError(Exception):\n"
        "    pass\n"
        "\n"
        f"{OLD_SIGNATURE}\n"
        "    departments = {}\n"
        f"{OLD_FETCH}"
        "    sad = []\n"
        "    for row in sad:\n"
        "        departments[department][\"magellan_sad\"].append(row)\n"
        "        departments[department][\"customer_response_risks\"].append({\n"
        f"{OLD_RETURN}"
        "\n"
        "def run(publish=False, deliver=False):\n"
        "    reused = False\n"
        "    try:\n"
        f"{OLD_REUSE}"
        "    except Exception:\n"
        "        departments = None\n"
        "    if departments is None:\n"
        f"{OLD_CALL}"
        f"{OLD_PUBLISH}"
        "        return departments\n"
        "\n"
        "def main():\n"
        "    try:\n"
        "        return None\n"
        f"{OLD_EXCEPT}"
    )


def test_repair_installs_fail_closed_gate_and_is_idempotent(tmp_path: Path):
    target = tmp_path / "production_main.py"
    target.write_text(_legacy_source(), encoding="utf-8")
    target.chmod(0o644)

    assert repair(target) == "repaired"
    repaired = target.read_text(encoding="utf-8")
    assert NEW_SIGNATURE in repaired
    assert NEW_FETCH in repaired
    assert NEW_RETURN in repaired
    assert NEW_CALL in repaired
    assert NEW_REUSE in repaired
    assert NEW_PUBLISH in repaired
    assert NEW_EXCEPT in repaired
    assert OLD_SIGNATURE not in repaired
    assert OLD_CALL not in repaired
    assert "raise SystemExit(3)" in repaired
    assert target.with_suffix(".py.pre-magellan-empty-gate").is_file()

    namespace: dict = {}
    exec(compile(repaired, str(target), "exec"), namespace)
    empty_table = {
        "calls": [],
        "source_status": "available",
        "pages_complete": True,
        "older_boundary_reached": False,
        "pagination_exhausted": True,
        "rows_inspected": 0,
    }
    with pytest.raises(namespace["MagellanEmptyExtractError"], match="Refusing to publish or deliver"):
        namespace["refuse_unverified_empty_magellan"](empty_table, publish=True, deliver=True)
    namespace["refuse_unverified_empty_magellan"](empty_table, publish=False, deliver=False)
    verified = dict(empty_table, older_boundary_reached=True, rows_inspected=8)
    namespace["refuse_unverified_empty_magellan"](verified, publish=True, deliver=True)
    assert namespace["snapshot_magellan_blocks_delivery"](
        {"Personal Lines": {"magellan_sad": []}}
    ) is True

    assert repair(target) == "already_repaired"


def test_repair_refuses_unknown_and_partial_shapes(tmp_path: Path):
    unknown = tmp_path / "unknown.py"
    unknown.write_text("def run():\n    return None\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unexpected Magellan empty-extract shape"):
        repair(unknown)

    partial = tmp_path / "partial.py"
    partial.write_text(
        "def refuse_unverified_empty_magellan(magellan, *, publish, deliver, environ=None):\n"
        "    return None\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="partial Magellan empty-extract repair"):
        repair(partial)
