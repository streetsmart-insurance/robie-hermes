from pathlib import Path


REPOSITORY_SKILL = Path("skills/ezlynx-overdue-submission-reports/SKILL.md")
DEPLOY_SKILL = Path("deploy/hermes/skills/ezlynx-overdue-submission-reports/SKILL.md")


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

