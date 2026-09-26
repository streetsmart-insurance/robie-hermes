"""Team Lead Chat end-of-day formatter, webhook gate, and weekday timer."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts.magellan_empty_gate import snapshot_magellan_blocks_delivery
from src.reporters.team_lead_chat import (
    ENV_SECRET,
    TEAM_LEAD_CHAT_SPACE,
    TeamLeadChatWebhookMissing,
    post_team_lead_chat,
    resolve_team_lead_chat_webhook,
    secret_resource_name,
    validate_team_lead_webhook_url,
)
from src.reporters.team_lead_chat_eod import (
    DEPARTMENT_NAMES,
    REQUIRED_DEPARTMENT_KEYS,
    format_eod_message,
    load_day_sources,
    magellan_section_unverified,
    main,
    run_eod,
)

ROOT = Path(__file__).resolve().parents[1]
UNIT_DIR = ROOT / "deploy" / "systemd" / "streetsmart-accountability-prod"
EASTERN = ZoneInfo("America/New_York")
DAY = datetime(2026, 9, 25, tzinfo=EASTERN).date()
NOW = datetime(2026, 9, 25, 17, 6, tzinfo=EASTERN)
WEBHOOK = (
    "https://chat.googleapis.com/v1/spaces/AAQAHYP7Ezg/messages"
    "?key=test-key&token=test-token"
)
PHONE = "7325550100"
EMAIL = "pat.example@streetsmart.insurance"


class _Response:
    def __init__(self, status: int, payload: dict):
        self.status = status
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _department(**overrides) -> dict:
    data = {key: [] for key in REQUIRED_DEPARTMENT_KEYS}
    data.update(overrides)
    return data


def _departments(**overrides) -> dict:
    return {name: _department(**overrides) for name in DEPARTMENT_NAMES}


def _write_snapshot(app_root: Path, departments: dict, *, prepared_at: datetime, break_hash: bool = False) -> None:
    snapshot, prepared, manifest = (
        app_root / "data" / "outputs" / f"department_dashboard_{DAY.isoformat()}.json",
        app_root / "data" / "outputs" / f"department_dashboard_{DAY.isoformat()}.prepared.json",
        app_root / "data" / "inbox" / DAY.isoformat() / "manifest.json",
    )
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(json.dumps(departments), encoding="utf-8")
    manifest.write_text(
        json.dumps({"sources": {"ringcentral": {"status": "ready"}, "magellan": {"status": "ready_api"}}}),
        encoding="utf-8",
    )
    metadata = {
        "target_date": DAY.isoformat(),
        "prepared_at": prepared_at.astimezone(timezone.utc).isoformat(),
        "snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
    }
    if break_hash:
        metadata["snapshot_sha256"] = "0" * 64
    prepared.write_text(json.dumps(metadata), encoding="utf-8")


def _reader(_resource: str) -> str:
    return WEBHOOK


def _opener_factory(calls: list[str]):
    def opener(request, timeout=20):
        del timeout
        body = json.loads(request.data.decode("utf-8"))
        calls.append(body["text"])
        assert WEBHOOK.split("token=")[-1] not in body["text"]
        return _Response(200, {"name": f"{TEAM_LEAD_CHAT_SPACE}/messages/eod-1"})

    return opener


def test_secret_name_defaults_and_rejects_a_url():
    assert secret_resource_name({}).endswith(
        "/secrets/accountability-team-lead-chat-webhook/versions/latest"
    )
    assert secret_resource_name({ENV_SECRET: "custom-secret"}).endswith("/secrets/custom-secret/versions/latest")
    with pytest.raises(TeamLeadChatWebhookMissing):
        secret_resource_name({ENV_SECRET: WEBHOOK})


def test_webhook_must_be_the_team_lead_space():
    validate_team_lead_webhook_url(WEBHOOK)
    wrong_space = WEBHOOK.replace("AAQAHYP7Ezg", "AAAA_OTHER")
    with pytest.raises(TeamLeadChatWebhookMissing):
        validate_team_lead_webhook_url(wrong_space)
    with pytest.raises(TeamLeadChatWebhookMissing):
        resolve_team_lead_chat_webhook(secret_reader=lambda _name: "")


def test_missing_webhook_does_not_post(tmp_path: Path):
    calls: list[str] = []

    def reader(_resource: str) -> str:
        raise TeamLeadChatWebhookMissing("missing")

    result = run_eod(
        app_root=tmp_path,
        day=DAY,
        now=NOW,
        secret_reader=reader,
        opener=_opener_factory(calls),
    )
    assert result["delivered"] is False
    assert result["status"] == "webhook_missing"
    assert calls == []
    assert "test-token" not in json.dumps(result)


def test_wrong_space_does_not_post(tmp_path: Path):
    calls: list[str] = []
    result = run_eod(
        app_root=tmp_path,
        day=DAY,
        now=NOW,
        secret_reader=lambda _name: WEBHOOK.replace("AAQAHYP7Ezg", "AAAA_OTHER"),
        opener=_opener_factory(calls),
    )
    assert result["status"] == "webhook_missing"
    assert calls == []


def test_missing_snapshot_posts_incomplete_status_without_metrics(tmp_path: Path):
    calls: list[str] = []
    result = run_eod(
        app_root=tmp_path,
        day=DAY,
        now=NOW,
        secret_reader=_reader,
        opener=_opener_factory(calls),
    )
    assert result["delivered"] is True
    assert result["status"] == "incomplete"
    text = calls[0]
    assert "2026-09-25" in text
    assert "not ready" in text
    assert "No call count is shown" in text
    assert "Overdue tasks" not in text
    assert PHONE not in text
    assert "gmail" not in text.casefold()


def test_verified_snapshot_counts_sad_rows_and_hides_pii(tmp_path: Path):
    departments = _departments()
    departments["Personal Lines"]["magellan_sad"] = [{
        "Account / caller": "Pat Example",
        "Phone": PHONE,
        "Owner": EMAIL,
        "Sentiment": "Sad",
    }]
    departments["Personal Lines"]["magellan_calls_on_target_date"] = 6
    departments["Operations"]["overdue_tasks"] = [{"Task": "follow up", "Phone": PHONE}]
    departments["Commercial Lines"]["missed_calls"] = [{}]
    departments["Personal Lines"]["policy_changes"] = []
    departments["Operations"]["cois"] = [{}, {}]
    departments["Business Development"]["submissions"] = [{"id": "1"}]
    _write_snapshot(tmp_path, departments, prepared_at=NOW - timedelta(minutes=5))
    calls: list[str] = []
    result = run_eod(
        app_root=tmp_path,
        day=DAY,
        now=NOW,
        secret_reader=_reader,
        opener=_opener_factory(calls),
    )
    assert result["status"] == "summary"
    text = calls[0]
    assert "1 sad or at-risk" in text
    assert "Magellan calls recorded for this day: 6." in text
    assert "- Overdue tasks: 1" in text
    assert "- Missed calls: 1" in text
    assert "- Policy changes: 0" in text
    assert "- Pending COIs: 2" in text
    assert "- Submission Center rows: 1" in text
    assert PHONE not in text
    assert EMAIL not in text
    assert "Pat Example" not in text
    assert "test-token" not in text


def test_unverified_empty_magellan_does_not_claim_zero_sad(tmp_path: Path):
    departments = _departments()
    _write_snapshot(tmp_path, departments, prepared_at=NOW - timedelta(minutes=2))
    text = format_eod_message(load_day_sources(tmp_path, DAY, NOW))
    assert "not verified" in text
    assert "0 sad" not in text
    assert "No sad-call count is shown" in text
    assert magellan_section_unverified(departments) is True


def test_verified_zero_magellan_may_say_zero():
    departments = _departments(
        magellan_calls_on_target_date=0,
        magellan_extract_verified_empty=True,
    )
    sources = load_day_sources(Path("/no/such/app"), DAY, NOW)
    sources.snapshot_verified = True
    sources.departments = departments
    sources.problems.clear()
    text = format_eod_message(sources)
    assert "0 sad or at-risk" in text
    assert magellan_section_unverified(departments) is False


def test_stale_or_unverified_snapshot_is_not_counted(tmp_path: Path):
    departments = _departments()
    departments["Operations"]["overdue_tasks"] = [{"Task": "old"}]
    _write_snapshot(tmp_path, departments, prepared_at=NOW - timedelta(hours=5))
    text = format_eod_message(load_day_sources(tmp_path, DAY, NOW))
    assert "Overdue tasks" not in text
    assert "not ready" in text

    _write_snapshot(tmp_path, departments, prepared_at=NOW - timedelta(minutes=1), break_hash=True)
    text = format_eod_message(load_day_sources(tmp_path, DAY, NOW))
    assert "Overdue tasks" not in text


def test_magellan_cache_is_cited_only_when_complete(tmp_path: Path):
    raw = tmp_path / "data" / "raw"
    raw.mkdir(parents=True)
    (raw / "magellan_latest.json").write_text(json.dumps({
        "target_date": DAY.isoformat(),
        "source_status": "available",
        "pages_complete": True,
        "calls": [
            {"sentiment": "Sad", "from_phone": PHONE, "account": "Pat Example"},
            {"sentiment": "Satisfied", "from_phone": "2015550199"},
        ],
    }), encoding="utf-8")
    text = format_eod_message(load_day_sources(tmp_path, DAY, NOW))
    assert "Magellan cache for this day: 2 calls" in text
    assert "Sad or at-risk rows in that cache: 1." in text
    assert PHONE not in text
    assert "Pat Example" not in text
    assert "Overdue tasks" not in text

    (raw / "magellan_latest.json").write_text(json.dumps({
        "target_date": DAY.isoformat(),
        "source_status": "available",
        "pages_complete": True,
        "older_boundary_reached": False,
        "rows_inspected": 0,
        "calls": [],
        "records_on_target_date": 0,
    }), encoding="utf-8")
    text = format_eod_message(load_day_sources(tmp_path, DAY, NOW))
    assert "not a verified extract" in text
    assert "0 calls" not in text
    assert "0 sad" not in text


def test_eod_magellan_gate_matches_publish_empty_gate():
    cases = [
        _departments(),
        _departments(magellan_calls_on_target_date=0, magellan_extract_verified_empty=True),
        _departments(magellan_sad=[{"Sentiment": "Sad"}], magellan_calls_on_target_date=3),
        {"Personal Lines": {"magellan_sad": "nope"}},
    ]
    for departments in cases:
        assert magellan_section_unverified(departments) is snapshot_magellan_blocks_delivery(
            departments, environ={}
        )


def test_summary_is_not_posted_twice(tmp_path: Path):
    departments = _departments(
        magellan_sad=[{"Sentiment": "Sad"}],
        magellan_calls_on_target_date=2,
    )
    _write_snapshot(tmp_path, departments, prepared_at=NOW - timedelta(minutes=1))
    calls: list[str] = []
    opener = _opener_factory(calls)
    first = run_eod(app_root=tmp_path, day=DAY, now=NOW, secret_reader=_reader, opener=opener)
    second = run_eod(app_root=tmp_path, day=DAY, now=NOW, secret_reader=_reader, opener=opener)
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert len(calls) == 1


def test_incomplete_post_can_be_replaced_by_a_later_summary(tmp_path: Path):
    calls: list[str] = []
    opener = _opener_factory(calls)
    first = run_eod(app_root=tmp_path, day=DAY, now=NOW, secret_reader=_reader, opener=opener)
    departments = _departments(magellan_sad=[{"Sentiment": "Sad"}], magellan_calls_on_target_date=1)
    _write_snapshot(tmp_path, departments, prepared_at=NOW - timedelta(minutes=1))
    second = run_eod(app_root=tmp_path, day=DAY, now=NOW, secret_reader=_reader, opener=opener)
    assert first["status"] == "incomplete"
    assert second["status"] == "summary"
    assert len(calls) == 2


def test_cli_refuses_a_webhook_url_in_the_secret_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ENV_SECRET, WEBHOOK)
    assert main(["--date", DAY.isoformat(), "--app-root", str(tmp_path)]) == 3


def test_dry_run_does_not_read_the_webhook(tmp_path: Path):
    def reader(_resource: str) -> str:
        raise AssertionError("dry-run must not read the webhook")

    result = run_eod(
        app_root=tmp_path,
        day=DAY,
        now=NOW,
        dry_run=True,
        secret_reader=reader,
    )
    assert result["dry_run"] is True
    assert result["delivered"] is False
    assert "not ready" in result["text"]


def test_post_failure_is_not_success(tmp_path: Path):
    import urllib.error

    def opener(_request, timeout=20):
        del timeout
        raise urllib.error.HTTPError(
            url="https://chat.googleapis.com/v1/spaces/AAQAHYP7Ezg/messages",
            code=503,
            msg="unavailable",
            hdrs=None,
            fp=None,
        )

    result = run_eod(
        app_root=tmp_path,
        day=DAY,
        now=NOW,
        secret_reader=_reader,
        opener=opener,
    )
    assert result["delivered"] is False
    assert result["status"] == "post_failed"
    assert "test-token" not in json.dumps(result)
    assert "AAQAHYP7Ezg" not in result["reason"]


def test_modules_and_units_are_chat_only_and_separate_from_collect():
    chat = (ROOT / "src" / "reporters" / "team_lead_chat.py").read_text(encoding="utf-8")
    eod = (ROOT / "src" / "reporters" / "team_lead_chat_eod.py").read_text(encoding="utf-8")
    wrapper = (ROOT / "scripts" / "run_team_lead_chat_eod.sh").read_text(encoding="utf-8")
    service = (UNIT_DIR / "streetsmart-accountability-team-lead-chat-eod.service").read_text(encoding="utf-8")
    timer = (UNIT_DIR / "streetsmart-accountability-team-lead-chat-eod.timer").read_text(encoding="utf-8")
    morning = (UNIT_DIR / "streetsmart-accountability.service").read_text(encoding="utf-8")
    for text in (chat, eod, wrapper, service, timer):
        lowered = text.casefold()
        assert "send_team_lead_report" not in text
        assert "gmail" not in lowered
        assert "run_source_collection_vm.sh" not in text
        assert "--skip-if-prepared" not in text
        assert "--publish" not in text
        assert "--deliver" not in text
    assert "OnCalendar=Mon..Fri *-*-* 17:05:00 America/New_York" in timer
    assert "Unit=streetsmart-accountability-team-lead-chat-eod.service" in timer
    assert "ExecStart=/opt/streetsmart-daily-accountability/scripts/run_team_lead_chat_eod.sh" in service
    assert "run_daily_accountability_vm.sh" in morning
    assert "team_lead_chat_eod" not in morning
    assert "flock -w 60" in wrapper
    assert "15m" not in wrapper
    assert "-m src.reporters.team_lead_chat_eod" in wrapper
    collect_timer = UNIT_DIR / "streetsmart-accountability-collect-evening.timer"
    if collect_timer.exists():
        collect_body = collect_timer.read_text(encoding="utf-8")
        assert "team_lead_chat_eod" not in collect_body
        assert "17:00:00" in collect_body
    runbook = (ROOT / "docs" / "ACCOUNTABILITY_CLOUD_RUNBOOK.md").read_text(encoding="utf-8")
    assert "17:05 Team Lead Chat EOD" in runbook
    assert "17:00 collect" in runbook
    assert "does not send leadership email" in runbook


def test_post_helper_requires_a_message_receipt():
    def opener(_request, timeout=20):
        del timeout
        return _Response(200, {"name": "spaces/SOMEWHERE/messages/1"})

    with pytest.raises(Exception):
        post_team_lead_chat("hello", webhook_url=WEBHOOK, opener=opener)
