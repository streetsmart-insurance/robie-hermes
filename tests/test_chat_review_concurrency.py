"""Synthetic races at the note reservation and shared session boundaries."""
import json
import multiprocessing
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from types import SimpleNamespace
from unittest.mock import Mock

from robie_job_engine import discussion_note_ledger as ledger
from robie_job_engine.chat_job_controls import consume_note_repost_allowance
from robie_job_engine.chat_turn_control import _release_adapter_job
from robie_job_engine.ezlynx_discussions import file_note_to_existing_discussion
from robie_job_engine.store import JobStore


def _record_worker(path, label):
    # Enlarge the read/replace window; without the interprocess lock concurrent
    # updates overwrite each other even though each rename is atomic.
    original = ledger._read_file
    def slow_read(path):
        result = original(path)
        time.sleep(0.02)
        return result
    ledger._read_file = slow_read
    for index in range(5):
        ledger.begin_unconfirmed_note("26356199", "synthetic-discussion",
                                     note_text=f"{label} {index}", ledger_path=path)


def test_interprocess_ledger_updates_preserve_every_uncertain_note(tmp_path):
    path = tmp_path / "notes.json"
    ctx = multiprocessing.get_context("spawn")
    workers = [ctx.Process(target=_record_worker, args=(path, label)) for label in ("A", "B")]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(15)
        assert not worker.is_alive()
        assert worker.exitcode == 0
    notes = json.loads(path.read_text())["notes"]
    assert len(notes) == 10
    assert all(row["confirmation"] == ledger.SENT_UNCONFIRMED for row in notes)


def test_concurrent_uncertain_post_is_reserved_before_other_caller(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBIE_ENV", "TEST")
    monkeypatch.setattr("robie_job_engine.ezlynx_write_scope.ALLOWED_EZLYNX_WRITE_APPLICANT_IDS",
                        frozenset({"26356199"}))
    started, finish = Event(), Event()
    class Client:
        posts = 0
        def get_discussions(self, applicant):
            return [{"discussionId": "synthetic-discussion", "title": "Synthetic", "applicantId": applicant}]
        def get_discussion(self, discussion):
            return {"title": "Synthetic", "noteCount": 0, "mostRecentNoteId": "old"}
        def append_note(self, *args, **kwargs):
            self.posts += 1
            started.set()
            assert finish.wait(5)
            raise TimeoutError("synthetic uncertain server acceptance")
    client = Client()
    def send():
        try:
            return file_note_to_existing_discussion(client, "26356199", "Synthetic concurrent note",
                title_hint="Synthetic", ledger_path=tmp_path / "notes.json")
        except TimeoutError:
            return {"status": "timeout"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(send)
        assert started.wait(5)
        second = pool.submit(send)
        finish.set()
        assert first.result()["status"] == "timeout"
        assert second.result()["status"] == "already_posted"
    assert client.posts == 1


def test_one_repost_approval_has_one_concurrent_consumer(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = store.create_job("hermes.google_chat_task", {"text": "Synthetic note"})
    store.checkpoint(job["id"], "note_repost_confirmed", {"text": "yes"})
    barrier = Barrier(2)
    def consume():
        barrier.wait()
        return consume_note_repost_allowance(store, job["id"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: consume(), range(2)))
    assert sorted(results) == [False, True]


def test_finishing_turn_does_not_release_other_turn_shared_session():
    lease, agent = Mock(), object()
    key = "agent:main:google_chat:dm:spaces/synthetic"
    adapter = SimpleNamespace(
        _gateway_turns={
            ("spaces/synthetic", "thread-a"): {"job_id": "A"},
            ("spaces/synthetic", "thread-b"): {
                "job_id": "B", "task": SimpleNamespace(done=lambda: False)},
        },
        _active_chat_job={"spaces/synthetic": "B"},
        gateway_runner=SimpleNamespace(_active_session_leases={key: lease}, _running_agents={key: agent}),
    )
    _release_adapter_job(adapter, "A", cancel_turn=True)
    assert ("spaces/synthetic", "thread-b") in adapter._gateway_turns
    lease.release.assert_not_called()
    assert adapter.gateway_runner._active_session_leases[key] is lease
    assert adapter.gateway_runner._running_agents[key] is agent
