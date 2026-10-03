"""Same-day playground health probe.

Flag off exits 0. Flag on exits 1 when the AI provider is missing or
today's Chat jobs all failed. The message never includes a secret value.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_queue import DurableChatEventQueue
from robie_job_engine.models import JobStatus
from robie_job_engine.store import JobStore


def _probe():
    path = Path(__file__).resolve().parents[1] / "scripts" / "check_robie_playground_health.py"
    spec = importlib.util.spec_from_file_location("check_robie_playground_health", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


PROBE = _probe()


def _chat_job(db: str, *, status: JobStatus, when: datetime, name: str) -> str:
    store = JobStore(db)
    created = store.create_job(
        "hermes.plain_english",
        {"text": name},
        idempotency_key=name,
    )
    stamp = when.isoformat()
    with store.connect() as conn:
        conn.execute(
            "UPDATE jobs SET status=?, created_at=?, updated_at=? WHERE id=?",
            (status.value, stamp, stamp, created["id"]),
        )
    DurableChatEventQueue(db).link_conversation_job(
        conversation_id="spaces/health",
        job_id=created["id"],
        message_id=f"spaces/health/messages/{name}",
        event_id=name,
        relation="CREATED",
    )
    return created["id"]


def test_flag_off_exits_zero_even_when_jobs_failed_and_provider_is_missing():
    today = datetime.now(timezone.utc)
    with durable_temporary_directory() as tmp:
        db = str(Path(tmp) / "jobs.db")
        _chat_job(db, status=JobStatus.FAILED, when=today, name="failed-today")
        code, message = PROBE.evaluate({"ROBIE_PLAYGROUND": ""}, db_path=db, limit=5)
    assert code == 0
    assert "off" in message.casefold()


def test_flag_on_without_a_provider_exits_nonzero_and_hides_secrets():
    secret = "sk-test-do-not-print"
    code, message = PROBE.evaluate(
        {"ROBIE_PLAYGROUND": "1", "OPENAI_API_KEY": ""},
        db_path="/no/such/jobs.db",
        limit=5,
        hermes_home="/no/such/hermes",
    )
    assert code == 1
    assert "no AI provider" in message
    assert secret not in message


def test_flag_on_with_a_key_and_a_successful_job_exits_zero():
    today = datetime.now(timezone.utc)
    secret = "sk-live-value-must-not-leak"
    with durable_temporary_directory() as tmp:
        db = str(Path(tmp) / "jobs.db")
        _chat_job(db, status=JobStatus.FAILED, when=today, name="one-fail")
        _chat_job(db, status=JobStatus.COMPLETE, when=today, name="one-ok")
        code, message = PROBE.evaluate(
            {"ROBIE_PLAYGROUND": "1", "OPENAI_API_KEY": secret},
            db_path=db,
            limit=5,
        )
    assert code == 0
    assert secret not in message


def test_flag_on_fails_when_todays_chat_jobs_all_failed():
    today = datetime.now(timezone.utc)
    yesterday = today - timedelta(days=1)
    with durable_temporary_directory() as tmp:
        db = str(Path(tmp) / "jobs.db")
        _chat_job(db, status=JobStatus.COMPLETE, when=yesterday, name="old-ok")
        _chat_job(db, status=JobStatus.FAILED, when=today, name="fail-a")
        _chat_job(db, status=JobStatus.FAILED, when=today, name="fail-b")
        code, message = PROBE.evaluate(
            {
                "ROBIE_PLAYGROUND": "1",
                "ROBIE_GEMINI_API_KEY_SECRET": (
                    "projects/example/secrets/gemini-api-key/versions/latest"
                ),
            },
            db_path=db,
            limit=5,
        )
    assert code == 1
    assert "all failed" in message
    assert "gemini-api-key" not in message
    assert "projects/example" not in message


def test_vertex_config_counts_as_a_provider():
    with durable_temporary_directory() as tmp:
        home = Path(tmp) / "hermes"
        home.mkdir()
        (home / "config.yaml").write_text(
            "model:\n  provider: vertex\n  default: google/gemini-2.5-flash\n",
            encoding="utf-8",
        )
        db = str(Path(tmp) / "jobs.db")
        JobStore(db)
        code, message = PROBE.evaluate(
            {"ROBIE_PLAYGROUND": "true"},
            db_path=db,
            hermes_home=str(home),
        )
    assert code == 0
    assert "configured" in message.casefold()


def test_env_file_key_counts_and_is_not_printed():
    secret = "sk-from-dotenv"
    with durable_temporary_directory() as tmp:
        home = Path(tmp) / "hermes"
        home.mkdir()
        (home / ".env").write_text(f"ANTHROPIC_API_KEY={secret}\n", encoding="utf-8")
        db = str(Path(tmp) / "jobs.db")
        JobStore(db)
        code, message = PROBE.evaluate(
            {"ROBIE_PLAYGROUND": "yes"},
            db_path=db,
            hermes_home=str(home),
        )
    assert code == 0
    assert secret not in message
