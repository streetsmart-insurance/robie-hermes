from pathlib import Path


REPOSITORY_SKILL = Path("skills/ezlynx-overdue-submission-reports/SKILL.md")
DEPLOY_SKILL = Path("deploy/hermes/skills/ezlynx-overdue-submission-reports/SKILL.md")
ONESHOT_WORKFLOW = Path(".github/workflows/oneshot-overdue-clean-test.yml")
ONESHOT_POLLER = Path("scripts/poll_overdue_513_outcome.py")
ONESHOT_RUNNER = Path("scripts/run_one_overdue_clean_sheets.py")


def test_repository_and_deploy_skill_are_exact_mirrors():
    assert REPOSITORY_SKILL.read_bytes() == DEPLOY_SKILL.read_bytes()


def test_skill_stays_test_only_until_three_clean_test_runs():
    content = REPOSITORY_SKILL.read_text(encoding="utf-8")
    assert 'job_type: "ezlynx.overdue_submission_reports"' in content
    assert 'status: "Testing"' in content
    assert "production_ready: false" in content


def test_skill_requires_both_live_red_state_and_day_31_threshold():
    content = REPOSITORY_SKILL.read_text(encoding="utf-8")
    assert "displayed in red/overdue" in content
    assert "run date - Quote Due Date > 30 days" in content
    assert "exactly 30 days overdue does not qualify" in content
    assert "must pass both checks" in content


def test_skill_fails_closed_before_email_when_auth_or_recipient_lookup_is_unavailable():
    content = REPOSITORY_SKILL.read_text(encoding="utf-8")
    assert "stop before classifying records or sending email" in content
    assert "Never guess an address" in content
    assert "Do not change submission statuses" in content


def test_skill_documents_why_agency_wide_pagination_may_run_longer():
    content = REPOSITORY_SKILL.read_text(encoding="utf-8")
    assert "## Perform time" in content
    assert "120-second starting budget" in content
    assert "durable progress" in content
    assert "3600-second" in content


def test_oneshot_requires_exact_deployed_main_release():
    content = ONESHOT_WORKFLOW.read_text(encoding="utf-8")
    assert 'expected="${GITHUB_SHA:0:12}"' in content
    assert r'grep -q \"${expected}\"' in content
    assert "grep -q b92c571" not in content


def test_oneshot_reporter_opens_job_ledger_read_only():
    content = ONESHOT_POLLER.read_text(encoding="utf-8")
    assert "?mode=ro" in content
    assert "uri=True" in content


def test_oneshot_records_durable_progress_for_post_job_audit():
    content = ONESHOT_RUNNER.read_text(encoding="utf-8")
    assert content.count('heartbeat_generic_chat_job(job_id, source="overdue-one-shot-test")') == 2
