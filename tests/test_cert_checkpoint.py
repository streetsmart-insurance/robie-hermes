"""Tests for the durable certificates dedupe checkpoint (SQLite).

Offline: tmp_path only, no mailbox, no network.
"""

import json
import sqlite3
import sys
import time

import pytest

sys.path.insert(0, "robie_job_engine")

from cert_checkpoint import SqliteDedupeStore
from cert_intake import CertEmail, is_duplicate, mark_processed


def _email(gmail_id="abc123"):
    return CertEmail(
        gmail_id=gmail_id,
        thread_id="thread-1",
        rfc_message_id="<req-1@example.com>",
        from_header="bob@example.com",
        subject="COI please",
        date="Sat, 26 Sep 2026 08:00:00 -0400",
        body_text="Need a certificate for Lawn Buddies LLC",
        attachments=[],
    )


def test_seen_mark_roundtrip(tmp_path):
    store = SqliteDedupeStore(tmp_path / "ckpt.db")
    assert store.seen("gmail:x") is False
    store.mark("gmail:x")
    assert store.seen("gmail:x") is True
    store.close()


def test_meta_survives_as_json(tmp_path):
    store = SqliteDedupeStore(tmp_path / "ckpt.db")
    store.mark("gmail:x", {"applicant_id": 220250093, "action": "filed"})
    assert store.get_meta("gmail:x") == {"applicant_id": 220250093, "action": "filed"}
    assert store.get_meta("gmail:missing") is None
    store.close()


def test_persistence_across_reopen(tmp_path):
    path = tmp_path / "ckpt.db"
    store = SqliteDedupeStore(path)
    store.mark("gmail:x", {"n": 1})
    store.close()
    reopened = SqliteDedupeStore(path)
    assert reopened.seen("gmail:x") is True
    assert reopened.get_meta("gmail:x") == {"n": 1}
    reopened.close()


def test_remark_keeps_first_seen_refreshes_last_seen(tmp_path):
    store = SqliteDedupeStore(tmp_path / "ckpt.db")
    store.mark("gmail:x")
    first = sqlite3.connect(tmp_path / "ckpt.db").execute(
        "SELECT first_seen_at, last_seen_at FROM dedupe_keys WHERE key='gmail:x'"
    ).fetchone()
    time.sleep(1.1)
    store.mark("gmail:x", {"retry": True})
    second = sqlite3.connect(tmp_path / "ckpt.db").execute(
        "SELECT first_seen_at, last_seen_at FROM dedupe_keys WHERE key='gmail:x'"
    ).fetchone()
    assert second[0] == first[0]  # first_seen_at untouched
    assert second[1] > first[1]  # last_seen_at refreshed
    store.close()


def test_drop_in_with_cert_intake_helpers(tmp_path):
    """SqliteDedupeStore works with is_duplicate / mark_processed unchanged."""
    store = SqliteDedupeStore(tmp_path / "ckpt.db")
    email = _email()
    dup, _ = is_duplicate(email, store)
    assert dup is False
    mark_processed(email, store, {"applicant_id": 220250093})
    dup, reason = is_duplicate(email, store)
    assert dup is True
    assert "already processed" in reason
    store.close()
    # A fresh process reading the same file still sees it as duplicate.
    store2 = SqliteDedupeStore(tmp_path / "ckpt.db")
    dup, _ = is_duplicate(email, store2)
    assert dup is True
    store2.close()


def test_stats(tmp_path):
    store = SqliteDedupeStore(tmp_path / "ckpt.db")
    store.mark("gmail:a")
    store.mark("gmail:b")
    s = store.stats()
    assert s["keys"] == 2
    assert s["oldest_first_seen_at"]
    store.close()


def test_wal_files_created(tmp_path):
    store = SqliteDedupeStore(tmp_path / "ckpt.db")
    store.mark("gmail:x")
    # WAL mode: -wal / -shm sidecars exist while the connection is open.
    names = {p.name for p in tmp_path.iterdir()}
    assert "ckpt.db-wal" in names or "ckpt.db-shm" in names
    store.close()
