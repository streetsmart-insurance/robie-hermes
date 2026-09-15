"""Tests for robie_job_engine.zapier_tasks payload validation.

Covers the standing agency rule: every Zapier EZLynx follow-up task payload
must include a valid ISO YYYY-MM-DD due_date, or firing is refused.
"""

import pytest

from robie_job_engine import zapier_tasks


def _payload(**overrides):
    base = {
        "applicant_id": "220250093",
        "task_title": "Ascend cancellation notice - Test LLC",
        "assignee": "Erika Palacios",
        "source": "inbox-triage",
        "due_date": "2026-09-20",
    }
    base.update(overrides)
    return base


def test_valid_payload_passes_and_normalizes_due_date():
    payload = _payload(due_date="2026-9-5")
    zapier_tasks.validate_task_payload(payload)
    assert payload["due_date"] == "2026-09-05"


def test_missing_due_date_rejected():
    payload = _payload()
    del payload["due_date"]
    with pytest.raises(ValueError, match="due_date"):
        zapier_tasks.validate_task_payload(payload)


def test_empty_due_date_rejected():
    with pytest.raises(ValueError, match="due_date"):
        zapier_tasks.validate_task_payload(_payload(due_date=""))


@pytest.mark.parametrize(
    "bad",
    ["tomorrow", "09/20/2026", "20-09-2026", "2026-13-01", "2026-02-30", "next Friday", "2026-9-31"],
)
def test_malformed_due_date_rejected(bad):
    with pytest.raises(ValueError, match="due_date"):
        zapier_tasks.validate_task_payload(_payload(due_date=bad))


def test_validate_due_date_returns_canonical_form():
    assert zapier_tasks.validate_due_date("2026-09-20") == "2026-09-20"
    assert zapier_tasks.validate_due_date(" 2026-09-20 ") == "2026-09-20"


def test_non_dict_payload_rejected():
    with pytest.raises(ValueError):
        zapier_tasks.validate_task_payload(["not", "a", "dict"])


def test_other_required_keys_still_enforced():
    for key in ("applicant_id", "task_title", "assignee", "source"):
        payload = _payload()
        del payload[key]
        with pytest.raises(ValueError, match=key):
            zapier_tasks.validate_task_payload(payload)


def test_fire_task_validates_before_touching_script():
    # A bad payload must raise ValueError before fire_task looks for the
    # zap-trigger script or fires anything.
    with pytest.raises(ValueError, match="due_date"):
        zapier_tasks.fire_task(_payload(due_date="ASAP"), dry_run=True)
