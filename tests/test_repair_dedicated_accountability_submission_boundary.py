from pathlib import Path

from scripts.repair_dedicated_accountability_submission_boundary import (
    ADAPTER_REQUIRED_NEW,
    ADAPTER_REQUIRED_OLD,
    ADAPTER_VALIDATION_NEW,
    ADAPTER_VALIDATION_OLD,
    BROWSER_INIT_NEW,
    BROWSER_INIT_OLD,
    BROWSER_LOOP_NEW,
    BROWSER_LOOP_OLD,
    BROWSER_RANGE_NEW,
    BROWSER_RANGE_OLD,
    BROWSER_RESULT_NEW,
    BROWSER_RESULT_OLD,
    repair_adapter,
    repair_browser,
)


def _write(path: Path, *parts: str) -> Path:
    path.write_text("\n".join(parts), encoding="utf-8")
    path.chmod(0o640)
    return path


def test_repairs_browser_boundary_and_is_idempotent(tmp_path):
    path = _write(
        tmp_path / "ezlynx_submission_browser.py",
        BROWSER_INIT_OLD,
        BROWSER_LOOP_OLD,
        BROWSER_RESULT_OLD,
        BROWSER_RANGE_OLD,
    )
    assert repair_browser(path) == "repaired"
    text = path.read_text(encoding="utf-8")
    assert BROWSER_INIT_NEW in text
    assert BROWSER_LOOP_NEW in text
    assert BROWSER_RESULT_NEW in text
    assert BROWSER_RANGE_NEW in text
    assert repair_browser(path) == "already_repaired"
    backup = path.with_suffix(path.suffix + ".pre-full-exhaustion-boundary")
    assert backup.is_file()
    assert backup.stat().st_mode & 0o777 == 0o600


def test_repairs_adapter_validation_and_is_idempotent(tmp_path):
    path = _write(
        tmp_path / "ezlynx_submission_center.py",
        ADAPTER_REQUIRED_OLD,
        ADAPTER_VALIDATION_OLD,
    )
    assert repair_adapter(path) == "repaired"
    text = path.read_text(encoding="utf-8")
    assert ADAPTER_REQUIRED_NEW in text
    assert ADAPTER_VALIDATION_NEW in text
    assert repair_adapter(path) == "already_repaired"


def test_repair_refuses_unexpected_source(tmp_path):
    browser = _write(tmp_path / "browser.py", "unexpected")
    adapter = _write(tmp_path / "adapter.py", "unexpected")
    import pytest

    with pytest.raises(RuntimeError, match="unexpected browser-init source shape"):
        repair_browser(browser)
    with pytest.raises(RuntimeError, match="unexpected adapter-required source shape"):
        repair_adapter(adapter)
