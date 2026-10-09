"""#745 safety tests: real durable JobStore; no live network or browser."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock

import pytest

from robie_job_engine import ezlynx_task_cdp as cdp
from robie_job_engine.ezlynx_task_jobs import ensure_task_job
from robie_job_engine.store import JobStore
from robie_job_engine.task_assignment_worker import (
    TaskAssignmentWorker, UnverifiedNoteError, _WorkerReassignPortAdapter,
)
from test_ezlynx_task_intake import make_task, FakeDiscussionClient


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Live network forbidden in task safety tests")
    monkeypatch.setattr("socket.socket.connect", blocked)
    monkeypatch.setattr("socket.create_connection", blocked)
    monkeypatch.setattr(cdp, "_browser_page", blocked)


@pytest.fixture
def context(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job, _ = ensure_task_job(store, make_task())
    return store, job


def post(client, store, job):
    return TaskAssignmentWorker(discussion_client=client)._post_note_verified(
        store, job, "849945654", "one intent")


def _live_state(**changes):
    state = {"assignee": "Robie AI", "description": "Please call the client about their quote.",
             "created_by": "Carlo Ferrara", "assigned_producer": "", "csr": "", "activity_labels": ""}
    state.update(changes)
    return state


@pytest.mark.parametrize("applicant", ["", " ", "PROSPECT", "prospect", "0"])
def test_invalid_applicant_not_sent(applicant):
    port = Mock()
    adapter = _WorkerReassignPortAdapter(port, make_task(applicant_id=applicant))
    result = adapter.reassign_task("63429523", "Carlo Ferrara", "completion note")
    assert result["ok"] is False and result["sent"] is False
    port.reassign.assert_not_called()
    assert adapter.read_task_assignee("63429523") is None
    port.read_assignee.assert_not_called()
    with pytest.raises(cdp.ReassignError):
        cdp.PlaywrightTaskReassigner().read_assignee("63429523", applicant)


def test_adapter_bound_original_identity_and_description():
    port = Mock()
    port.reassign.return_value = "Carlo Ferrara"
    port.read_assignee.return_value = "Carlo Ferrara"
    port.read_task_state.return_value = _live_state()
    task = make_task()
    adapter = _WorkerReassignPortAdapter(port, task)
    assert adapter.reassign_task(task.task_id, "Carlo Ferrara", "completion note")["ok"]
    port.reassign.assert_called_once_with(task.task_id, task.applicant_id,
        "Carlo Ferrara", description=task.description, expected_assignee="Robie AI")
    port.reset_mock()
    assert not adapter.reassign_task("999", "Carlo Ferrara")["ok"]
    port.reassign.assert_not_called()


def test_relay_200_without_destination_proof_is_not_delivered():
    port = Mock()
    port.reassign.return_value = {"status": 200, "accepted": True}
    port.read_assignee.return_value = "Robie AI"
    port.read_task_state.return_value = _live_state()
    result = _WorkerReassignPortAdapter(port, make_task()).reassign_task("63429523", "Carlo Ferrara")
    assert result["ok"] is False
    assert result["sent"] is None
    assert result["delivery_status"] == "unverified"


class Locator:
    def __init__(self, attrs=None, count=1, value="Robie AI"):
        self.attrs = attrs or {}
        self.n = count
        self.clicks = 0
        self.value = value
        self.children = {}
    def count(self): return self.n
    def wait_for(self, **kwargs): pass
    def get_attribute(self, key): return self.attrs.get(key)
    def click(self): self.clicks += 1
    def get_by_role(self, role, name, exact): return self.children[(role, name)]
    def get_by_label(self, name, exact): return self.children[("label", name)]
    def input_value(self): return self.value


class Page:
    url = cdp._activity_url("220250093")
    def __init__(self, rows, panel):
        self.rows = rows
        self.panel = panel
    def locator(self, selector):
        assert selector == '[data-task-id="63429523"]'
        return self.rows
    def get_by_role(self, role, name, exact):
        assert (role, name, exact) == ("dialog", "Edit Task", True)
        return self.panel


def dom():
    attrs = {"data-task-id": "63429523", "data-applicant-id": "220250093"}
    row, panel = Locator(attrs.copy()), Locator(attrs.copy())
    row.children[("button", "Edit this task")] = Locator()
    panel.children[("label", "Assign this task")] = Locator()
    panel.children[("button", "Cancel")] = Locator()
    panel.children[("button", "Save")] = Locator()
    return Page(row, panel)


@pytest.mark.parametrize("count", [0, 2])
def test_missing_ambiguous_task_refused(count):
    page = dom()
    page.rows.n = count
    with pytest.raises(cdp.ReassignError):
        cdp._search_and_open_edit(page, "63429523", "220250093")
    assert page.rows.clicks == 0


def test_duplicate_descriptions_different_ids_select_exact_only():
    page = dom()
    # Selector intentionally has no description/text API; unrelated duplicate
    # descriptions cannot become candidates or authorize a page-wide Edit.
    assert cdp._search_and_open_edit(page, "63429523", "220250093") is page.panel
    assert page.rows.children[("button", "Edit this task")].clicks == 1


@pytest.mark.parametrize("where", ["url", "row", "panel"])
def test_foreign_applicant_refused(where):
    page = dom()
    if where == "url": page.url = cdp._activity_url("999")
    else: (page.rows if where == "row" else page.panel).attrs["data-applicant-id"] = "999"
    with pytest.raises(cdp.ReassignError):
        cdp._search_and_open_edit(page, "63429523", "220250093")
    assert page.panel.children[("button", "Save")].clicks == 0


def test_changed_assignee_never_overwritten(monkeypatch, tmp_path):
    _contract_file(tmp_path, monkeypatch)
    page = dom()
    page.panel.children[("label", "Assign this task")].value = "Other Producer"
    @contextmanager
    def browser(): yield page
    monkeypatch.setattr(cdp, "_browser_page", browser)
    monkeypatch.setattr(cdp, "_goto_activity", lambda *args: None)
    monkeypatch.setenv(cdp.REASSIGN_GATE_ENV, "1")
    with pytest.raises(cdp.ReassignError, match="Assignee changed"):
        cdp.PlaywrightTaskReassigner().reassign("63429523", "220250093", "Carlo Ferrara")
    assert page.panel.children[("button", "Save")].clicks == 0


@pytest.mark.parametrize("failure", [TimeoutError, SystemExit])
def test_accepted_post_before_timeout_or_crash_restart_no_repost(context, failure):
    store, job = context
    client = FakeDiscussionClient()
    original = client.append_note
    def accepted_then_failed(*args, **kwargs):
        original(*args, **kwargs)
        raise failure("accepted before local receipt")
    client.append_note = accepted_then_failed
    with pytest.raises((UnverifiedNoteError, SystemExit)):
        post(client, store, job)
    client.append_note = original
    restarted = JobStore(store.path)
    with pytest.raises(UnverifiedNoteError): post(client, restarted, job)
    assert len(client.posts) == 1


def test_durable_receipt_reconciled_after_read_failure(context):
    store, job = context
    client = FakeDiscussionClient()
    read = client.get_discussion
    client.get_discussion = Mock(side_effect=TimeoutError())
    with pytest.raises(TimeoutError): post(client, store, job)
    client.get_discussion = read
    assert post(client, JobStore(store.path), job) == "note-123"
    assert len(client.posts) == 1


def test_concurrent_attempt_is_suppressed(context):
    store, job = context
    client = FakeDiscussionClient()
    entered, release = Event(), Event()
    original = client.append_note
    def waiting(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    client.append_note = waiting
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(post, client, store, job)
        assert entered.wait(5)
        with pytest.raises(UnverifiedNoteError): post(client, JobStore(store.path), job)
        release.set()
        assert future.result() == "note-123"
    assert len(client.posts) == 1


def test_pre_send_checkpoint_failure_sends_nothing(context, monkeypatch):
    store, job = context
    client = FakeDiscussionClient()
    monkeypatch.setattr(store, "transaction", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError): post(client, store, job)
    assert client.posts == []


def test_receipt_checkpoint_failure_restart_does_not_send_again(context, monkeypatch):
    store, job = context
    client = FakeDiscussionClient()
    monkeypatch.setattr(store, "checkpoint", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError): post(client, store, job)
    with pytest.raises(UnverifiedNoteError): post(client, JobStore(store.path), job)
    assert len(client.posts) == 1


def test_changed_intent_cannot_bypass_reservation(context):
    store, job = context
    client = FakeDiscussionClient()
    assert post(client, store, job) == "note-123"
    with pytest.raises(UnverifiedNoteError):
        TaskAssignmentWorker(discussion_client=client)._post_note_verified(store, job, "849945654", "different text")
    assert len(client.posts) == 1


# ---------------------------------------------------------------------------
# Task-flow reliability review: wrong-client writes, duplicate effects,
# resume/recovery, false completion. Real durable JobStore, synthetic
# destinations. These prove source behavior only; they are NOT live proof.
# ---------------------------------------------------------------------------
from datetime import datetime, timedelta, timezone  # noqa: E402

from robie_job_engine import ezlynx_task_intake as intake  # noqa: E402
from robie_job_engine.ezlynx_task_inbox import IngestedReport  # noqa: E402
from robie_job_engine.ezlynx_task_intake_health import check_intake  # noqa: E402
from robie_job_engine.ezlynx_task_jobs import _find_by_idempotency_key, task_idempotency_key  # noqa: E402
from robie_job_engine.models import JobStatus  # noqa: E402
from robie_job_engine.task_assignment_worker import TaskIntakeVerifier, is_test_task  # noqa: E402


class Discussions:
    """Sequential note ids; the applicant owns exactly `ids`."""

    def __init__(self, ids=("849945654",), show_text=True):
        self.ids = list(ids)
        self.posts = []
        self.latest = "note-000"
        self.notes = []
        self.texts = {}
        self.created = {}
        self.show_text = show_text
        self.id_lookups = []

    def inject_note(self, note_id, text, created="unset"):
        """A note somebody else wrote in the same discussion (created: ISO text, None = no time)."""
        self.notes.append(note_id)
        self.texts[note_id] = text
        if created == "unset":
            created = datetime.now(timezone.utc).isoformat()
        if created is not None:
            self.created[note_id] = created
        self.latest = note_id

    def get_discussion_ids(self, applicant_id):
        self.id_lookups.append(applicant_id)
        return list(self.ids)

    def append_note(self, discussion_id, body, applicant_id=None, **kwargs):
        del applicant_id, kwargs
        self.posts.append((discussion_id, body))
        self.latest = f"note-{len(self.posts)}"
        self.notes.append(self.latest)
        self.texts[self.latest] = body
        self.created[self.latest] = datetime.now(timezone.utc).isoformat()
        return {"note_id": self.latest}

    def get_discussion(self, discussion_id):
        return {"title": "Task Note", "mostRecentNoteId": self.latest,
                "noteCount": 5 + len(self.notes),
                "notes": [self._row(n) for n in self.notes]}

    def _row(self, n):
        row = {"id": n}
        if self.show_text and n in self.texts:
            row["body"] = self.texts[n]
        if n in self.created:
            row["createdDate"] = self.created[n]
        return row


class Owners:
    def __init__(self, assignee="Robie AI", unresolved=(), crash_after_save=None, fail_before_save=None):
        self.assignee = assignee
        self.unresolved = set(unresolved)
        self.crash_after_save = crash_after_save
        self.fail_before_save = (list(fail_before_save) if isinstance(fail_before_save, (list, tuple))
                                 else ([fail_before_save] if fail_before_save else []))
        self.calls = []
        self.attempts = []
        self.live_description = None  # None: the live request equals what the caller passes
        self.live_fields = {}         # live values that differ from make_task()'s defaults
        self.hide_fields = ()         # fields the live read cannot return
        self.state_error = None

    def read_task_state(self, task_id, applicant_id, description=""):
        if self.state_error is not None:
            raise self.state_error
        state = {"assignee": self.assignee,
                 "description": self.live_description if self.live_description is not None else description,
                 "created_by": "Carlo Ferrara", "assigned_producer": "", "csr": "", "activity_labels": ""}
        state.update(self.live_fields)
        for name in self.hide_fields:
            state.pop(name, None)
        return state

    def reassign(self, task_id, applicant_id, new_assignee, description="", expected_assignee="Robie AI"):
        self.attempts.append(new_assignee)
        if new_assignee in self.unresolved:
            raise cdp.AssigneeUnresolvedError("Missing or ambiguous assignee option; no edit")
        if self.fail_before_save:
            raise self.fail_before_save.pop(0)
        if self.assignee.casefold() != expected_assignee.casefold():
            raise cdp.ReassignError("Assignee changed since intake; reassignment not sent")
        self.calls.append(new_assignee)
        self.assignee = new_assignee
        if self.crash_after_save:
            exc, self.crash_after_save = self.crash_after_save, None
            raise exc
        return new_assignee

    def read_assignee(self, task_id, applicant_id, description=""):
        return self.assignee


def _worker(disc, owners=None, gate=True):
    return TaskAssignmentWorker(discussion_client=disc, task_reassigner=owners,
                                reassign_enabled=gate and owners is not None)


def _work(store, worker, task):
    job, _ = ensure_task_job(store, task)
    return worker.process_job(store, job)


def _verify(store, job_id, disc, owners, verifier_store=None):
    verifier = TaskIntakeVerifier(discussion_client=disc, task_reassigner=owners,
                                  store=verifier_store)
    action = store.get_checkpoint(job_id, "action") or {}
    intake._build_engine(store, verifier)._verify(store.get_job(job_id), action)
    return store.get_job(job_id)


def _lapse(store, job_id):
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    with store.transaction() as conn:
        conn.execute("UPDATE jobs SET lease_expires_at=? WHERE id=?", (past, job_id))


def _later_ms(seconds=5):
    return str(int((datetime.now(timezone.utc) + timedelta(seconds=seconds)).timestamp() * 1000))


def _age(store, job_id, hours):
    stamp = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with store.transaction() as conn:
        conn.execute("UPDATE jobs SET updated_at=? WHERE id=?", (stamp, job_id))


@pytest.fixture
def store(tmp_path):
    return JobStore(tmp_path / "jobs.db")


# ---- wrong-client writes ---------------------------------------------------

def test_discussion_not_on_applicant_blocks_every_write(store):
    disc, owners = Discussions(ids=["111"]), Owners()
    result = _work(store, _worker(disc, owners), make_task())
    assert result["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    assert disc.posts == [] and owners.calls == []


def test_applicant_outside_write_allowlist_blocks_every_write(store):
    disc, owners = Discussions(), Owners()
    result = _work(store, _worker(disc, owners), make_task(applicant_id="25486692"))
    assert result["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    assert disc.posts == [] and owners.calls == []


def test_unprovable_discussion_ownership_blocks_every_write(store):
    class NoLookup:
        posts = []
        def append_note(self, *a, **k): self.posts.append(a); return {"note_id": "n"}
        def get_discussion(self, *a): return {}
    client, owners = NoLookup(), Owners()
    result = _work(store, _worker(client, owners), make_task())
    assert result["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    assert client.posts == [] and owners.calls == []


def test_verifier_rejects_discussion_not_on_applicant(store):
    disc, owners = Discussions(), Owners()
    job = _work(store, _worker(disc, owners), make_task())
    assert job["status"] == JobStatus.VERIFYING.value
    disc.ids = ["111"]
    assert _verify(store, job["id"], disc, owners)["status"] != JobStatus.COMPLETE.value


def test_reassigner_refuses_non_allowlisted_applicant_before_browser(monkeypatch):
    monkeypatch.setenv("EZLYNX_TASK_REASSIGN_ENABLED", "1")
    with pytest.raises(Exception) as caught:
        cdp.PlaywrightTaskReassigner().reassign("63429523", "25486692", "Carlo Ferrara")
    assert not isinstance(caught.value, AssertionError), "browser was opened"
    assert "allowlist" in str(caught.value)


# ---- duplicate effects, resume, reopen -----------------------------------

def test_resume_after_question_posts_new_step_note_and_reassigns_once(store):
    disc, owners = Discussions(), Owners()
    first = _work(store, _worker(disc, None), make_task())
    assert first["status"] == JobStatus.AWAITING_HUMAN_INPUT.value and disc.posts == []
    store.resume(first["id"])
    second = _worker(disc, owners).process_job(store, store.get_job(first["id"]))
    assert second["status"] == JobStatus.VERIFYING.value, second.get("last_error")
    assert len(disc.posts) == 1 and owners.calls == ["Carlo Ferrara"]
    assert _verify(store, first["id"], disc, owners)["status"] == JobStatus.COMPLETE.value


@pytest.mark.parametrize("second_ask", ["Please call the client about their quote.",
                                        "New ask: also email the client"])
def test_second_request_after_handback_gets_its_own_note(store, second_ask):
    disc, owners = Discussions(), Owners()
    worker = _worker(disc, owners)
    job = _work(store, worker, make_task())
    assert _verify(store, job["id"], disc, owners)["status"] == JobStatus.COMPLETE.value
    owners.assignee = "Robie AI"  # staff hands it back to Robie with a new ask
    job2, created = ensure_task_job(
        store, make_task(last_modified="2026-10-04T09:00:00", description=second_ask),
        report_received_at=_later_ms(), confirm_returned=lambda t: True)
    assert not created and job2["status"] == JobStatus.PENDING.value
    after = worker.process_job(store, job2)
    assert after["status"] == JobStatus.VERIFYING.value, after.get("last_error")
    assert len(disc.posts) == 2 and owners.calls == ["Carlo Ferrara", "Carlo Ferrara"]
    assert _verify(store, job["id"], disc, owners)["status"] == JobStatus.COMPLETE.value


def test_test_task_note_not_reposted_when_the_request_changes_but_the_note_is_the_same(store):
    disc, owners = Discussions(), Owners()
    worker = _worker(disc, owners)
    task = make_task(description="Test task for Roby - please ignore")
    job = _work(store, worker, task)
    assert _verify(store, job["id"], disc, owners)["status"] == JobStatus.COMPLETE.value
    job2, _ = ensure_task_job(store, make_task(description="Test task for Robie - please ignore again",
                                               last_modified="2026-10-04T09:00:00"))
    assert job2["status"] == JobStatus.PENDING.value  # the request changed, so it is reopened
    after = worker.process_job(store, job2)
    assert after["status"] == JobStatus.VERIFYING.value, after.get("last_error")
    assert len(disc.posts) == 1 and owners.calls == []


def test_save_landed_but_receipt_lost_is_reconciled_not_repeated(store):
    disc, owners = Discussions(), Owners(crash_after_save=TimeoutError("lost after save"))
    result = _work(store, _worker(disc, owners), make_task())
    assert result["status"] == JobStatus.VERIFYING.value, result.get("last_error")
    assert owners.calls == ["Carlo Ferrara"] and len(disc.posts) == 1


def test_process_death_after_save_then_restart_does_not_save_twice(store):
    disc, owners = Discussions(), Owners(crash_after_save=SystemExit("killed after save"))
    job, _ = ensure_task_job(store, make_task())
    with pytest.raises(SystemExit):
        _worker(disc, owners).process_job(store, job)
    assert store.get_job(job["id"])["status"] == JobStatus.RUNNING.value
    restarted = JobStore(store.path)
    far_future = datetime.now(timezone.utc) + timedelta(hours=3)
    assert intake.recover_stale_running(restarted, now=far_future) == [job["id"]]
    result = _worker(disc, owners).process_job(restarted, restarted.get_job(job["id"]))
    assert result["status"] == JobStatus.VERIFYING.value, result.get("last_error")
    assert owners.calls == ["Carlo Ferrara"]


def test_unknown_save_is_never_replayed_just_because_the_task_is_still_robies(store):
    disc, owners = Discussions(), Owners(fail_before_save=TimeoutError("page died"))
    first = _work(store, _worker(disc, owners), make_task())
    assert first["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    assert owners.calls == [] and disc.posts == []
    store.resume(first["id"])
    second = _worker(disc, owners).process_job(store, store.get_job(first["id"]))
    assert second["status"] == JobStatus.AWAITING_HUMAN_INPUT.value, "a plain resume replayed the Save"
    assert owners.calls == []
    # Only an explicit human authorization allows one more Save.
    assert intake.resume_task("63429523", db_path=store.path, allow_retry_save=True) == 0
    third = _worker(disc, owners).process_job(store, store.get_job(first["id"]))
    assert third["status"] == JobStatus.VERIFYING.value, third.get("last_error")
    assert owners.calls == ["Carlo Ferrara"]


def test_a_retry_authorization_is_used_once(store):
    disc = Discussions()
    owners = Owners(fail_before_save=[TimeoutError("first"), TimeoutError("second")])
    first = _work(store, _worker(disc, owners), make_task())
    assert first["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    assert intake.resume_task("63429523", db_path=store.path, allow_retry_save=True) == 0
    second = _worker(disc, owners).process_job(store, store.get_job(first["id"]))
    assert second["status"] == JobStatus.AWAITING_HUMAN_INPUT.value  # attempt 2 also failed
    store.resume(first["id"])
    third = _worker(disc, owners).process_job(store, store.get_job(first["id"]))
    assert third["status"] == JobStatus.AWAITING_HUMAN_INPUT.value, "the old authorization was reused"
    assert owners.calls == []


def test_retry_authorization_needs_an_unknown_save_to_retry(store):
    disc, owners = Discussions(), Owners()
    first = _work(store, _worker(disc, None), make_task())
    assert first["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    assert intake.resume_task("63429523", db_path=store.path, allow_retry_save=True) == 1


def test_unknown_save_with_owner_changed_by_someone_else_pauses_no_save(store):
    disc, owners = Discussions(), Owners(assignee="Someone Else", fail_before_save=TimeoutError("x"))
    job, _ = ensure_task_job(store, make_task())
    store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
    store.checkpoint(job["id"], "task-reassign-intent:0", {
        "task_id": "63429523", "applicant_id": "220250093", "target": "Carlo Ferrara",
        "expected_assignee": "Robie AI", "state": "attempting", "skipped": []})
    store.transition(job["id"], JobStatus.PENDING, expected={JobStatus.RUNNING})
    result = _worker(disc, owners).process_job(store, store.get_job(job["id"]))
    assert result["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    assert owners.calls == [] and disc.posts == []


def test_unresolvable_first_owner_falls_through_in_order(store):
    disc, owners = Discussions(), Owners(unresolved={"Carlo Ferrara"})
    owners.live_fields = {"assigned_producer": "Mike Sosa", "csr": "Jazmin Molina"}
    task = make_task(assigned_producer="Mike Sosa", csr="Jazmin Molina")
    job = _work(store, _worker(disc, owners), task)
    assert job["status"] == JobStatus.VERIFYING.value, job.get("last_error")
    assert owners.calls == ["Mike Sosa"]
    assert _verify(store, job["id"], disc, owners)["status"] == JobStatus.COMPLETE.value


def test_robie_is_never_its_own_return_target(store):
    disc, owners = Discussions(), Owners()
    owners.live_fields = {"created_by": "Robie AI", "assigned_producer": "Mike Sosa"}
    task = make_task(created_by="Robie AI", assigned_producer="Mike Sosa")
    job = _work(store, _worker(disc, owners), task)
    assert owners.calls == ["Mike Sosa"] and job["status"] == JobStatus.VERIFYING.value


def test_verifier_rejects_a_return_owner_that_skipped_precedence(store):
    disc, owners = Discussions(), Owners()
    owners.live_fields = {"assigned_producer": "Mike Sosa", "csr": "Jazmin Molina"}
    task = make_task(assigned_producer="Mike Sosa", csr="Jazmin Molina")
    job = _work(store, _worker(disc, owners), task)
    action = store.get_checkpoint(job["id"], "action")
    action["reassigned"] = {"to": "Jazmin Molina", "verified_assignee": "Jazmin Molina"}
    store.checkpoint(job["id"], "action", action)
    owners.assignee = "Jazmin Molina"
    assert _verify(store, job["id"], disc, owners)["status"] != JobStatus.COMPLETE.value


def test_resume_with_answer_uses_the_chosen_owner(store):
    disc, owners = Discussions(), Owners()
    task = make_task(created_by="", assigned_producer="", csr="")
    first = _work(store, _worker(disc, owners), task)
    assert first["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    assert intake.resume_task(task.task_id, db_path=store.path, assign_to="Mike Sosa") == 0
    second = _worker(disc, owners).process_job(store, store.get_job(first["id"]))
    assert second["status"] == JobStatus.VERIFYING.value, second.get("last_error")
    assert owners.calls == ["Mike Sosa"]
    done = _verify(store, first["id"], disc, owners, verifier_store=store)
    assert done["status"] == JobStatus.COMPLETE.value


def test_uncertain_note_pauses_for_a_human_instead_of_failing_and_never_reposts(store):
    disc, owners = Discussions(), Owners()
    original = disc.append_note
    def accepted_then_timeout(*args, **kwargs):
        original(*args, **kwargs)
        raise TimeoutError("accepted before receipt")
    disc.append_note = accepted_then_timeout
    first = _work(store, _worker(disc, owners), make_task())
    assert first["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    store.resume(first["id"])
    again = _worker(disc, owners).process_job(store, store.get_job(first["id"]))
    assert again["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    assert len(disc.posts) == 1


def test_human_can_adopt_the_confirmed_note_id_and_work_continues(store):
    disc, owners = Discussions(), Owners()
    original = disc.append_note
    def accepted_then_timeout(*args, **kwargs):
        original(*args, **kwargs)
        raise TimeoutError("accepted before receipt")
    disc.append_note = accepted_then_timeout
    first = _work(store, _worker(disc, owners), make_task())
    assert first["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    assert intake.resume_task("63429523", db_path=store.path, note_id="note-1") == 0
    after = _worker(disc, owners).process_job(store, store.get_job(first["id"]))
    assert after["status"] == JobStatus.VERIFYING.value, after.get("last_error")
    assert len(disc.posts) == 1


def test_legacy_uncertain_note_intent_still_blocks_a_repost(store):
    disc, owners = Discussions(), Owners()
    job, _ = ensure_task_job(store, make_task())
    store.checkpoint(job["id"], "task-note-intent", {
        "task_id": "63429523", "applicant_id": "220250093", "discussion_id": "849945654",
        "body_sha256": "0" * 64, "state": "uncertain", "note_id": ""})
    result = _worker(disc, owners).process_job(store, store.get_job(job["id"]))
    assert result["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    assert disc.posts == []


# ---- recovery and visibility ------------------------------------------------

def test_stale_running_job_is_recovered_and_a_fresh_one_is_not(store):
    old, _ = ensure_task_job(store, make_task(task_id="1001"))
    fresh, _ = ensure_task_job(store, make_task(task_id="1002"))
    for job in (old, fresh):
        store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
    _age(store, old["id"], 3)
    assert intake.recover_stale_running(store) == [old["id"]]
    assert store.get_job(old["id"])["status"] == JobStatus.PENDING.value
    assert store.get_job(fresh["id"])["status"] == JobStatus.RUNNING.value


def _wire_intake(monkeypatch, report, disc, owners, gate):
    # Main wires Bland into the intake; these tests are about task handoff, so keep calls out.
    # Production intake leaves unlabeled tasks untouched. These tests opt in.
    monkeypatch.setattr(intake, "_include_unlabeled_tasks", lambda: True)
    monkeypatch.setattr("robie_job_engine.bland_prod_wiring.build_call_dependencies",
                        lambda: (None, None, None, True))
    monkeypatch.setattr("robie_job_engine.report_email_source.build_default_gmail_service", lambda: object())
    monkeypatch.setattr(intake, "fetch_latest_task_report", lambda service: report)
    monkeypatch.setattr(intake, "_build_discussion_client", lambda: disc)
    monkeypatch.setattr(intake, "PlaywrightTaskReassigner", lambda: owners)
    monkeypatch.setattr(intake, "reassign_enabled", lambda: gate["on"])


def _now_ms(minutes_ago=0):
    return str(int((datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).timestamp() * 1000))


def _report(task, *, message_id="m1", minutes_ago=0):
    tasks = () if task is None else (task,)
    return IngestedReport(message_id=message_id, filename="Robie_AI_-_Task_Check-In_1.csv",
                          digest=message_id, received_at=_now_ms(minutes_ago), tasks=tasks)


def test_resumed_job_is_worked_even_when_the_delivery_was_already_processed(tmp_path, monkeypatch):
    db = str(tmp_path / "jobs.db")
    task, disc, owners, gate = make_task(), Discussions(), Owners(), {"on": False}
    _wire_intake(monkeypatch, _report(task), disc, owners, gate)
    assert intake.run_intake(db_path=db) == 0
    job = _find_by_idempotency_key(JobStore(db), task_idempotency_key(task.task_id))
    assert job["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    gate["on"] = True
    assert intake.resume_task(task.task_id, db_path=db) == 0
    assert intake.run_intake(db_path=db) == 0  # same delivery, already processed
    final = JobStore(db).get_job(job["id"])
    assert final["status"] == JobStatus.COMPLETE.value, final.get("last_error")
    assert owners.calls == ["Carlo Ferrara"]


def test_a_task_that_cannot_get_a_job_is_recorded_as_partial_not_ok(tmp_path, monkeypatch):
    db = str(tmp_path / "jobs.db")
    task, disc, owners = make_task(), Discussions(), Owners()
    _wire_intake(monkeypatch, _report(task), disc, owners, {"on": False})
    monkeypatch.setattr(intake, "ensure_task_job", Mock(side_effect=OSError("disk full")))
    assert intake.run_intake(db_path=db) == 2
    with JobStore(db).connect() as conn:
        status = conn.execute("SELECT status FROM ezlynx_task_intake_runs").fetchone()[0]
    assert status == "partial"


def test_health_flags_waiting_verifying_and_queued_jobs_and_names_the_owner(store, monkeypatch):
    import robie_job_engine.ezlynx_task_intake_health as health
    from test_ezlynx_task_intake import _record_run
    _record_run(store, message_id="m1", status="ok", minutes_ago=5)
    waiting, _ = ensure_task_job(store, make_task(task_id="2001"))
    store.transition(waiting["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
    store.transition(waiting["id"], JobStatus.AWAITING_HUMAN_INPUT, expected={JobStatus.RUNNING},
                     error="needs an owner", resume_status=JobStatus.PENDING, release_lease=True)
    fresh_wait, _ = ensure_task_job(store, make_task(task_id="2002"))
    store.transition(fresh_wait["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
    store.transition(fresh_wait["id"], JobStatus.AWAITING_HUMAN_INPUT, expected={JobStatus.RUNNING},
                     error="needs an owner", resume_status=JobStatus.PENDING, release_lease=True)
    verifying, _ = ensure_task_job(store, make_task(task_id="2003"))
    store.transition(verifying["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
    store.transition(verifying["id"], JobStatus.VERIFYING, expected={JobStatus.RUNNING}, release_lease=True)
    queued, _ = ensure_task_job(store, make_task(task_id="2004"))
    for job in (waiting, verifying, queued):
        _age(store, job["id"], 6)
    monkeypatch.setattr(health, "default_db_path", lambda: store.path)
    monkeypatch.setenv("TASK_INTAKE_OWNER", "Carlo Ferrara")
    problems = check_intake()
    text = "\n".join(problems)
    assert "2001" in text and "waiting on a person" in text and "Carlo Ferrara" in text
    assert "2003" in text and "2004" in text
    assert "2002" not in text


# ---- false completion --------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Customer has a problem with the latest quote, please fix", False),
    ("Please contest the latest charge for Robert", False),
    ("Test task for Roby - please ignore", True),
    ("This is a test for Robie", True),
])
def test_is_test_task_requires_whole_words(text, expected):
    assert is_test_task(make_task(description=text)) is expected


def test_real_request_mentioning_test_lookalike_words_is_handed_back_not_acknowledged(store):
    disc, owners = Discussions(), Owners()
    task = make_task(description="Customer has a problem with the latest quote, please fix")
    job = _work(store, _worker(disc, owners), task)
    assert owners.calls == ["Carlo Ferrara"] and job["status"] == JobStatus.VERIFYING.value


def test_a_reassigner_that_reports_the_wrong_person_never_completes(store):
    class Liar(Owners):
        def reassign(self, task_id, applicant_id, new_assignee, **kwargs):
            super().reassign(task_id, applicant_id, new_assignee, **kwargs)
            return "Someone Else"
    disc, owners = Discussions(), Liar()
    result = _work(store, _worker(disc, owners), make_task())
    assert result["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    assert disc.posts == []


def test_verifier_rejects_a_read_back_that_is_not_the_intended_owner(store):
    disc, owners = Discussions(), Owners()
    job = _work(store, _worker(disc, owners), make_task())
    action = store.get_checkpoint(job["id"], "action")
    action["reassigned"]["verified_assignee"] = "Mike Sosa"
    store.checkpoint(job["id"], "action", action)
    owners.assignee = "Mike Sosa"
    assert _verify(store, job["id"], disc, owners)["status"] != JobStatus.COMPLETE.value


# ---------------------------------------------------------------------------
# Clara's six review findings on #774 (lease fencing, adopted notes, rounds).
# ---------------------------------------------------------------------------

# ---- 2. leases and fencing ---------------------------------------------------------

def test_a_live_lease_is_never_recovered_but_an_expired_one_is(store):
    job, _ = ensure_task_job(store, make_task())
    assert store.claim(job["id"], "worker-a", lease_seconds=900)
    store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
    _age(store, job["id"], 3)  # old, but its owner is still renewing the lease
    assert intake.recover_stale_running(store) == []
    assert store.get_job(job["id"])["status"] == JobStatus.RUNNING.value
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    with store.transaction() as conn:
        conn.execute("UPDATE jobs SET lease_expires_at=? WHERE id=?", (past, job["id"]))
    assert intake.recover_stale_running(store) == [job["id"]]
    assert store.get_job(job["id"])["lease_owner"] in (None, "")


def test_a_second_worker_cannot_start_while_the_first_holds_the_lease(store):
    disc, owners = Discussions(), Owners()
    job, _ = ensure_task_job(store, make_task())
    assert store.claim(job["id"], "worker-a", lease_seconds=900)
    result = _worker(disc, owners).process_job(store, store.get_job(job["id"]))
    assert result["status"] == JobStatus.PENDING.value
    assert disc.posts == [] and owners.calls == []


def test_transition_is_fenced_by_the_lease_owner(store):
    job, _ = ensure_task_job(store, make_task())
    store.claim(job["id"], "worker-a", lease_seconds=900)
    store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
    with pytest.raises(RuntimeError):
        store.transition(job["id"], JobStatus.VERIFYING, expected={JobStatus.RUNNING}, lease_owner="worker-b")
    assert store.get_job(job["id"])["status"] == JobStatus.RUNNING.value
    store.transition(job["id"], JobStatus.VERIFYING, expected={JobStatus.RUNNING}, lease_owner="worker-a")


def test_a_zombie_worker_cannot_write_after_a_replacement_takes_over(store):
    disc = Discussions()
    state = {}

    class MidSave(Owners):
        def reassign(self, task_id, applicant_id, new_assignee, **kwargs):
            # While the original worker is inside its Save, its lease lapses, the
            # job is recovered, and a replacement worker runs.
            job_id = state["job_id"]
            past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
            with store.transaction() as conn:
                conn.execute("UPDATE jobs SET lease_expires_at=? WHERE id=?", (past, job_id))
            state["recovered"] = intake.recover_stale_running(store)
            state["replacement"] = _worker(disc, owners).process_job(store, store.get_job(job_id))
            return super().reassign(task_id, applicant_id, new_assignee, **kwargs)

    owners = MidSave()
    job, _ = ensure_task_job(store, make_task())
    state["job_id"] = job["id"]
    original = _worker(disc, owners).process_job(store, job)
    assert state["recovered"] == [job["id"]]
    assert state["replacement"]["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    assert owners.calls == ["Carlo Ferrara"]          # the original's Save is the only one
    assert disc.posts == []                           # the zombie wrote nothing afterwards
    assert original["status"] == JobStatus.AWAITING_HUMAN_INPUT.value  # and did not clobber the replacement
    assert store.get_checkpoint(job["id"], "action") is None


# ---- 3. adopted notes bind to the exact note ---------------------------------------

def _uncertain_note_job(store, disc, owners):
    original = disc.append_note
    def accepted_then_timeout(*args, **kwargs):
        original(*args, **kwargs)
        raise TimeoutError("accepted before receipt")
    disc.append_note = accepted_then_timeout
    first = _work(store, _worker(disc, owners), make_task())
    assert first["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    disc.append_note = original
    return first


def test_an_adopted_note_id_must_be_the_note_Robie_meant_to_write(store):
    disc, owners = Discussions(), Owners()
    first = _uncertain_note_job(store, disc, owners)
    disc.inject_note("note-77", "Please call the client back about the invoice.")
    assert intake.resume_task("63429523", db_path=store.path, note_id="note-77") == 0
    after = _worker(disc, owners).process_job(store, store.get_job(first["id"]))
    assert after["status"] == JobStatus.AWAITING_HUMAN_INPUT.value, "adopted somebody else's note"
    assert len(disc.posts) == 1


def test_an_adopted_note_with_the_exact_content_is_confirmed(store):
    disc, owners = Discussions(), Owners()
    first = _uncertain_note_job(store, disc, owners)
    assert intake.resume_task("63429523", db_path=store.path, note_id="note-1") == 0
    after = _worker(disc, owners).process_job(store, store.get_job(first["id"]))
    assert after["status"] == JobStatus.VERIFYING.value, after.get("last_error")
    assert len(disc.posts) == 1
    note = store.get_checkpoint(first["id"], "action")["note"]
    assert note["adopted"] is True and note["purpose"] == "handoff" and note["round"] == 0
    assert _verify(store, first["id"], disc, owners)["status"] == JobStatus.COMPLETE.value


def test_an_adopted_note_is_refused_when_its_text_cannot_be_read(store):
    disc, owners = Discussions(show_text=False), Owners()
    first = _uncertain_note_job(store, disc, owners)
    assert intake.resume_task("63429523", db_path=store.path, note_id="note-1") == 0
    after = _worker(disc, owners).process_job(store, store.get_job(first["id"]))
    assert after["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    assert len(disc.posts) == 1


def test_adoption_only_touches_the_current_round(store):
    disc, owners = Discussions(), Owners()
    job, _ = ensure_task_job(store, make_task())
    store.checkpoint(job["id"], "task-note-intent:0:handoff", {
        "task_id": "63429523", "applicant_id": "220250093", "discussion_id": "849945654",
        "body_sha256": "0" * 64, "state": "uncertain", "note_id": ""})
    refreshed = dict(store.get_job(job["id"])["payload"], round=1)
    store.update_payload(job["id"], refreshed)
    assert intake.resume_task("63429523", db_path=store.path, note_id="note-1") == 1


# ---- 4. pending jobs recover after the task leaves the report -------------------------------

def test_a_pending_job_is_recovered_after_the_reassignment_removed_it_from_the_report(tmp_path, monkeypatch):
    db = str(tmp_path / "jobs.db")
    task, disc, owners, gate = make_task(), Discussions(), Owners(), {"on": True}
    holder = {"report": _report(task, message_id="m1")}
    _wire_intake(monkeypatch, None, disc, owners, gate)
    monkeypatch.setattr(intake, "fetch_latest_task_report", lambda service: holder["report"])
    original = disc.append_note
    def accepted_then_timeout(*args, **kwargs):
        original(*args, **kwargs)
        raise TimeoutError("accepted before receipt")
    disc.append_note = accepted_then_timeout
    assert intake.run_intake(db_path=db) == 0
    job = _find_by_idempotency_key(JobStore(db), task_idempotency_key(task.task_id))
    assert job["status"] == JobStatus.AWAITING_HUMAN_INPUT.value and owners.calls == ["Carlo Ferrara"]
    disc.append_note = original
    assert intake.resume_task(task.task_id, db_path=db, note_id="note-1") == 0
    holder["report"] = _report(None, message_id="m2")  # Robie no longer owns the task
    assert intake.run_intake(db_path=db) == 0
    final = JobStore(db).get_job(job["id"])
    assert final["status"] == JobStatus.COMPLETE.value, final.get("last_error")
    assert len(disc.posts) == 1


def test_a_pending_job_with_no_effects_is_still_left_alone_when_not_in_the_report(tmp_path, monkeypatch):
    db = str(tmp_path / "jobs.db")
    task, disc, owners = make_task(), Discussions(), Owners()
    store = JobStore(db)
    ensure_task_job(store, task)
    _wire_intake(monkeypatch, _report(None, message_id="m9"), disc, owners, {"on": True})
    assert intake.run_intake(db_path=db) == 0
    assert disc.posts == [] and owners.calls == []


# ---- 5. stale reports and Robie's own edits are not new requests ------------------------------

def test_a_stale_report_is_refused_and_flagged(tmp_path, monkeypatch):
    db = str(tmp_path / "jobs.db")
    task, disc, owners = make_task(), Discussions(), Owners()
    _wire_intake(monkeypatch, _report(task, minutes_ago=240), disc, owners, {"on": True})
    assert intake.run_intake(db_path=db) == 2
    assert disc.posts == [] and owners.calls == []
    with JobStore(db).connect() as conn:
        status = conn.execute("SELECT status FROM ezlynx_task_intake_runs").fetchone()[0]
    assert status == "stale"


def test_robies_own_last_modified_change_is_not_a_new_request(store):
    disc, owners = Discussions(), Owners()
    worker = _worker(disc, owners)
    task = make_task(description="Test task for Roby - please ignore")
    job = _work(store, worker, task)
    assert _verify(store, job["id"], disc, owners)["status"] == JobStatus.COMPLETE.value
    again, created = ensure_task_job(store, make_task(description=task.description,
                                                      last_modified="2026-10-04T09:00:00"))
    assert not created and again["status"] == JobStatus.COMPLETE.value
    assert again["payload"]["last_modified"] == "2026-10-04T09:00:00"
    assert len(disc.posts) == 1


def test_an_older_snapshot_of_a_task_is_ignored(store):
    disc, owners = Discussions(), Owners()
    job = _work(store, _worker(disc, owners), make_task(last_modified="2026-10-04T09:00:00"))
    assert _verify(store, job["id"], disc, owners)["status"] == JobStatus.COMPLETE.value
    same, created = ensure_task_job(store, make_task(last_modified="2026-10-03T08:00:00",
                                                     description="Something else entirely"))
    assert not created and same["status"] == JobStatus.COMPLETE.value
    assert same["payload"]["last_modified"] == "2026-10-04T09:00:00"


def test_a_handed_back_task_that_returns_unchanged_is_a_new_round(store):
    disc, owners = Discussions(), Owners()
    worker = _worker(disc, owners)
    job = _work(store, worker, make_task())
    assert _verify(store, job["id"], disc, owners)["status"] == JobStatus.COMPLETE.value
    owners.assignee = "Robie AI"
    again, created = ensure_task_job(store, make_task(last_modified="2026-10-05T09:00:00"),
                                     report_received_at=_later_ms(), confirm_returned=lambda t: True)
    assert not created and again["status"] == JobStatus.PENDING.value
    assert again["payload"]["round"] == 1


# ---- 6. human answers are scoped to their own round ---------------------------------------------

def test_a_human_answer_is_scoped_to_its_own_round(store):
    disc, owners = Discussions(), Owners()
    task = make_task(created_by="", assigned_producer="", csr="")
    first = _work(store, _worker(disc, owners), task)
    assert first["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    assert intake.resume_task(task.task_id, db_path=store.path, assign_to="Mike Sosa") == 0
    second = _worker(disc, owners).process_job(store, store.get_job(first["id"]))
    assert second["status"] == JobStatus.VERIFYING.value, second.get("last_error")
    assert _verify(store, first["id"], disc, owners, verifier_store=store)["status"] == JobStatus.COMPLETE.value
    owners.assignee = "Robie AI"
    again, _ = ensure_task_job(store, make_task(created_by="", assigned_producer="", csr="",
                                                last_modified="2026-10-05T09:00:00"),
                               report_received_at=_later_ms(), confirm_returned=lambda t: True)
    assert again["payload"]["round"] == 1
    third = _worker(disc, owners).process_job(store, again)
    assert third["status"] == JobStatus.AWAITING_HUMAN_INPUT.value, "reused an answer from an earlier round"
    assert owners.calls == ["Mike Sosa"]


# ---------------------------------------------------------------------------
# Clara's second review of #774 (7405c20): fenced records, changed targets,
# evidence of a return, consistent freshness, and old identical notes.
# ---------------------------------------------------------------------------

# ---- 1. effect-record writes are fenced, not just transitions -------------------------------

def test_a_late_worker_cannot_overwrite_the_replacements_attempt_record(store):
    disc, state = Discussions(), {}

    class Hung(Owners):
        def reassign(self, task_id, applicant_id, new_assignee, **kwargs):
            self.attempts.append(new_assignee)
            if len(self.attempts) == 1:
                # The original worker hangs inside attempt 1. Its lease lapses, the job is
                # recovered, a replacement pauses (unknown result), a human grants ONE retry,
                # and the replacement's retry (attempt 2) also ends unknown.
                job_id = state["job_id"]
                _lapse(store, job_id)
                intake.recover_stale_running(store)
                state["b1"] = _worker(disc, self).process_job(store, store.get_job(job_id))["status"]
                assert intake.resume_task("63429523", db_path=store.path, allow_retry_save=True) == 0
                state["b2"] = _worker(disc, self).process_job(store, store.get_job(job_id))["status"]
                raise TimeoutError("the original finally gives up")  # a late, stale outcome record
            raise TimeoutError("attempt 2 outcome unknown")

    owners = Hung()
    job, _ = ensure_task_job(store, make_task())
    state["job_id"] = job["id"]
    original = _worker(disc, owners).process_job(store, job)
    assert state["b1"] == JobStatus.AWAITING_HUMAN_INPUT.value
    assert state["b2"] == JobStatus.AWAITING_HUMAN_INPUT.value
    record = store.get_checkpoint(job["id"], "task-reassign-intent:0")
    assert record["attempt"] == 2, "a late worker rewrote the replacement's attempt record"
    assert original["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    store.resume(job["id"])  # the retry grant was used up by attempt 2
    third = _worker(disc, owners).process_job(store, store.get_job(job["id"]))
    assert third["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    assert len(owners.attempts) == 2, "a used retry grant became valid again"


def test_a_late_worker_cannot_write_a_note_record_after_losing_its_lease(store):
    disc, state = Discussions(), {}
    original_append = disc.append_note

    def append_then_lose_lease(discussion_id, body, applicant_id=None, **kwargs):
        del applicant_id, kwargs
        original_append(discussion_id, body)
        _lapse(store, state["job_id"])
        intake.recover_stale_running(store)
        raise TimeoutError("accepted, then the worker lost its lease")

    disc.append_note = append_then_lose_lease
    job, _ = ensure_task_job(store, make_task(description="Test task for Roby - please ignore"))
    state["job_id"] = job["id"]
    result = _worker(disc, Owners()).process_job(store, job)
    assert result["status"] == JobStatus.PENDING.value  # recovered, and the late worker changed nothing
    intent = store.get_checkpoint(job["id"], "task-note-intent:0:test-ack")
    assert intent["state"] == "uncertain" and intent["note_id"] == ""


# ---- 2. an unresolved Save blocks another Save even for a different person -----------------------

def test_an_unresolved_save_blocks_a_save_to_a_different_person(store):
    disc, owners = Discussions(), Owners(fail_before_save=TimeoutError("page died"))
    first = _work(store, _worker(disc, owners), make_task())
    assert first["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    assert intake.resume_task("63429523", db_path=store.path, assign_to="Mike Sosa") == 0
    after = _worker(disc, owners).process_job(store, store.get_job(first["id"]))
    assert after["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    assert owners.attempts == ["Carlo Ferrara"], "a changed target bypassed the unknown-outcome check"


def test_a_retry_grant_names_the_person_it_applies_to(store):
    disc, owners = Discussions(), Owners(fail_before_save=TimeoutError("page died"))
    first = _work(store, _worker(disc, owners), make_task())
    assert first["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    assert intake.resume_task("63429523", db_path=store.path, assign_to="Mike Sosa",
                              allow_retry_save=True) == 0
    after = _worker(disc, owners).process_job(store, store.get_job(first["id"]))
    assert after["status"] == JobStatus.VERIFYING.value, after.get("last_error")
    assert owners.calls == ["Mike Sosa"]


def test_a_save_that_landed_on_someone_else_is_not_followed_by_another_save(store):
    disc, owners = Discussions(), Owners(crash_after_save=SystemExit("killed after save"))
    job, _ = ensure_task_job(store, make_task())
    with pytest.raises(SystemExit):
        _worker(disc, owners).process_job(store, job)
    _lapse(store, job["id"])
    assert intake.recover_stale_running(store) == [job["id"]]
    store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
    store.transition(job["id"], JobStatus.AWAITING_HUMAN_INPUT, expected={JobStatus.RUNNING},
                     error="x", resume_status=JobStatus.PENDING, release_lease=True)
    assert intake.resume_task("63429523", db_path=store.path, assign_to="Mike Sosa") == 0
    after = _worker(disc, owners).process_job(store, store.get_job(job["id"]))
    assert after["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    assert owners.attempts == ["Carlo Ferrara"], "a second Save was attempted after the first landed"


# ---- 3. a new round needs evidence the task actually returned to Robie ---------------------------

def _handed_back_job(store, disc, owners):
    job = _work(store, _worker(disc, owners), make_task())
    assert _verify(store, job["id"], disc, owners)["status"] == JobStatus.COMPLETE.value
    return job


def test_a_delayed_report_cannot_open_a_new_round(store):
    disc, owners = Discussions(), Owners()
    job = _handed_back_job(store, disc, owners)
    applied = datetime.fromisoformat(store.get_checkpoint(job["id"], "task-reassign-intent:0")["applied_at"])
    delayed_ms = str(int((applied - timedelta(minutes=10)).timestamp() * 1000))
    again, created = ensure_task_job(store, make_task(last_modified="2026-10-05T09:00:00"),
                                     report_received_at=delayed_ms, confirm_returned=lambda t: True)
    assert not created and again["status"] == JobStatus.COMPLETE.value
    assert int(again["payload"].get("round") or 0) == 0


def test_a_new_round_needs_a_live_read_that_shows_robie(store):
    disc, owners = Discussions(), Owners()
    _handed_back_job(store, disc, owners)
    again, _ = ensure_task_job(store, make_task(last_modified="2026-10-05T09:00:00"),
                               report_received_at=_later_ms(), confirm_returned=lambda t: False)
    assert again["status"] == JobStatus.COMPLETE.value and int(again["payload"].get("round") or 0) == 0


def test_without_a_return_check_no_new_round_is_ever_opened(store):
    disc, owners = Discussions(), Owners()
    _handed_back_job(store, disc, owners)
    again, _ = ensure_task_job(store, make_task(last_modified="2026-10-05T09:00:00"))
    assert again["status"] == JobStatus.COMPLETE.value and int(again["payload"].get("round") or 0) == 0


def test_intake_reads_the_task_live_before_opening_a_round(tmp_path, monkeypatch):
    db = str(tmp_path / "jobs.db")
    task, disc, owners = make_task(), Discussions(), Owners()
    holder = {"report": _report(task, message_id="m1")}
    _wire_intake(monkeypatch, None, disc, owners, {"on": True})
    monkeypatch.setattr(intake, "fetch_latest_task_report", lambda service: holder["report"])
    assert intake.run_intake(db_path=db) == 0
    job = _find_by_idempotency_key(JobStore(db), task_idempotency_key(task.task_id))
    assert job["status"] == JobStatus.COMPLETE.value and owners.assignee == "Carlo Ferrara"
    # A lagging report claims the task is Robie's again, but it is still with Carlo.
    later = make_task(last_modified="2026-10-05T09:00:00")
    holder["report"] = _report(later, message_id="m2")
    assert intake.run_intake(db_path=db) == 0
    assert JobStore(db).get_job(job["id"])["status"] == JobStatus.COMPLETE.value
    assert len(disc.posts) == 1
    # Now it really is back with Robie.
    owners.assignee = "Robie AI"
    holder["report"] = _report(later, message_id="m3")
    assert intake.run_intake(db_path=db) == 0
    final = JobStore(db).get_job(job["id"])
    assert final["status"] == JobStatus.COMPLETE.value and int(final["payload"]["round"]) == 1
    assert len(disc.posts) == 2


# ---- 4. freshness applies to new work everywhere; owed recovery is separate -------------------------

def _record_processed(db, message_id):
    store = JobStore(db)
    intake._ensure_intake_table(store)
    intake._record_run(store, message_id=message_id, digest="d", filename="f.csv",
                       task_count=1, jobs_created=1, status="ok")


def test_an_already_processed_stale_report_does_not_start_new_work(tmp_path, monkeypatch):
    db = str(tmp_path / "jobs.db")
    task, disc, owners = make_task(), Discussions(), Owners()
    ensure_task_job(JobStore(db), task)  # PENDING, no effect taken yet
    _record_processed(db, "m1")
    _wire_intake(monkeypatch, _report(task, message_id="m1", minutes_ago=240), disc, owners, {"on": True})
    assert intake.run_intake(db_path=db) == 2
    job = _find_by_idempotency_key(JobStore(db), task_idempotency_key(task.task_id))
    assert job["status"] == JobStatus.PENDING.value
    assert disc.posts == [] and owners.calls == []


def test_owed_recovery_still_completes_when_the_latest_report_is_stale(tmp_path, monkeypatch):
    db = str(tmp_path / "jobs.db")
    task, disc, owners = make_task(), Discussions(), Owners()
    holder = {"report": _report(task, message_id="m1")}
    _wire_intake(monkeypatch, None, disc, owners, {"on": True})
    monkeypatch.setattr(intake, "fetch_latest_task_report", lambda service: holder["report"])
    original = disc.append_note
    def accepted_then_timeout(*args, **kwargs):
        original(*args, **kwargs)
        raise TimeoutError("accepted before receipt")
    disc.append_note = accepted_then_timeout
    assert intake.run_intake(db_path=db) == 0
    job = _find_by_idempotency_key(JobStore(db), task_idempotency_key(task.task_id))
    disc.append_note = original
    assert intake.resume_task(task.task_id, db_path=db, note_id="note-1") == 0
    holder["report"] = _report(None, message_id="m2", minutes_ago=240)
    assert intake.run_intake(db_path=db) == 2  # new work is refused...
    final = JobStore(db).get_job(job["id"])
    assert final["status"] == JobStatus.COMPLETE.value, final.get("last_error")  # ...owed recovery is not
    assert len(disc.posts) == 1


def test_owed_recovery_never_builds_call_dependencies(tmp_path, monkeypatch):
    db = str(tmp_path / "jobs.db")
    store = JobStore(db)
    disc, owners = Discussions(), Owners()
    job = _work(store, _worker(disc, owners), make_task())
    assert job["status"] == JobStatus.VERIFYING.value
    monkeypatch.setattr(intake, "_build_discussion_client", lambda: disc)
    monkeypatch.setattr(intake, "PlaywrightTaskReassigner", lambda: owners)
    monkeypatch.setattr(intake, "reassign_enabled", lambda: True)
    def boom():
        raise AssertionError("call dependencies were built during task recovery")
    monkeypatch.setattr("robie_job_engine.bland_prod_wiring.build_call_dependencies", boom)
    assert intake._recover_owed(store) == 1
    assert store.get_job(job["id"])["status"] == JobStatus.COMPLETE.value


# ---- 5. an old identical note is not proof of a new request ---------------------------------------------

def test_an_old_identical_note_is_not_adopted_as_the_note_for_this_request(store):
    disc, owners = Discussions(), Owners()
    first = _uncertain_note_job(store, disc, owners)
    disc.inject_note("note-77", disc.posts[0][1],
                     created=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat())
    assert intake.resume_task("63429523", db_path=store.path, note_id="note-77") == 0
    after = _worker(disc, owners).process_job(store, store.get_job(first["id"]))
    assert after["status"] == JobStatus.AWAITING_HUMAN_INPUT.value, "adopted an old identical note"
    assert len(disc.posts) == 1


@pytest.mark.parametrize("created", [None, "2026-10-04T10:00:00"])
def test_a_note_without_a_trustworthy_creation_time_is_not_adopted(store, created):
    disc, owners = Discussions(), Owners()
    first = _uncertain_note_job(store, disc, owners)
    disc.inject_note("note-78", disc.posts[0][1], created=created)  # none, or a naive time
    assert intake.resume_task("63429523", db_path=store.path, note_id="note-78") == 0
    after = _worker(disc, owners).process_job(store, store.get_job(first["id"]))
    assert after["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    assert len(disc.posts) == 1


def test_a_note_from_an_earlier_round_cannot_be_adopted_for_a_later_one(store):
    disc, owners = Discussions(), Owners()
    worker = _worker(disc, owners)
    job = _work(store, worker, make_task())
    assert _verify(store, job["id"], disc, owners)["status"] == JobStatus.COMPLETE.value
    owners.assignee = "Robie AI"
    again, _ = ensure_task_job(store, make_task(last_modified="2026-10-05T09:00:00"),
                               report_received_at=_later_ms(), confirm_returned=lambda t: True)
    assert int(again["payload"]["round"]) == 1
    original = disc.append_note
    def accepted_then_timeout(*args, **kwargs):
        original(*args, **kwargs)
        raise TimeoutError("accepted before receipt")
    disc.append_note = accepted_then_timeout
    second = worker.process_job(store, again)
    assert second["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    disc.append_note = original
    assert intake.resume_task("63429523", db_path=store.path, note_id="note-1") in (0, 1)  # round 0's note
    after = _worker(disc, owners).process_job(store, store.get_job(job["id"]))
    assert after["status"] == JobStatus.AWAITING_HUMAN_INPUT.value, "reused a note from an earlier round"
    assert intake.resume_task("63429523", db_path=store.path, note_id="note-2") == 0  # this round's own note
    done = _worker(disc, owners).process_job(store, store.get_job(job["id"]))
    assert done["status"] == JobStatus.VERIFYING.value, done.get("last_error")


# ---- Test-only task restriction: unrelated tasks stop before lookups or jobs ---------------------

def test_unrelated_tasks_are_stopped_before_any_lookup_or_job(tmp_path, monkeypatch):
    db = str(tmp_path / "jobs.db")
    allowed = make_task()
    other = make_task(task_id="70000001", applicant_id="25486692", discussion_id="900000001")
    disc, owners = Discussions(), Owners()
    report = IngestedReport(message_id="m1", filename="Robie_AI_-_Task_Check-In_1.csv", digest="d",
                            received_at=_now_ms(), tasks=(other, allowed))
    _wire_intake(monkeypatch, report, disc, owners, {"on": True})
    monkeypatch.setenv("ROBIE_TASK_INTAKE_ALLOWED_TASK_IDS", allowed.task_id)
    assert intake.run_intake(db_path=db) == 0
    store = JobStore(db)
    assert _find_by_idempotency_key(store, task_idempotency_key(other.task_id)) is None
    assert _find_by_idempotency_key(store, task_idempotency_key(allowed.task_id)) is not None
    assert "25486692" not in disc.id_lookups, "an unrelated client was looked up"


@pytest.mark.parametrize("value", [None, "", "  ,  "])
def test_a_test_environment_refuses_to_run_without_a_task_restriction(tmp_path, monkeypatch, value):
    db = str(tmp_path / "jobs.db")
    task, disc, owners = make_task(), Discussions(), Owners()
    _wire_intake(monkeypatch, _report(task), disc, owners, {"on": True})
    monkeypatch.setenv("ROBIE_ENV", "TEST")
    if value is None:
        monkeypatch.delenv("ROBIE_TASK_INTAKE_ALLOWED_TASK_IDS", raising=False)
    else:
        monkeypatch.setenv("ROBIE_TASK_INTAKE_ALLOWED_TASK_IDS", value)
    assert intake.run_intake(db_path=db) == 2
    assert _find_by_idempotency_key(JobStore(db), task_idempotency_key(task.task_id)) is None
    assert disc.id_lookups == [] and disc.posts == []


def test_the_task_restriction_also_limits_owed_recovery(tmp_path, monkeypatch):
    db = str(tmp_path / "jobs.db")
    store = JobStore(db)
    disc, owners = Discussions(), Owners()
    job = _work(store, _worker(disc, owners), make_task())
    assert job["status"] == JobStatus.VERIFYING.value
    monkeypatch.setattr(intake, "_build_discussion_client", lambda: disc)
    monkeypatch.setattr(intake, "PlaywrightTaskReassigner", lambda: owners)
    monkeypatch.setattr(intake, "reassign_enabled", lambda: True)
    monkeypatch.setenv("ROBIE_TASK_INTAKE_ALLOWED_TASK_IDS", "70000001")  # not this task
    assert intake._recover_owed(store) == 0
    assert store.get_job(job["id"])["status"] == JobStatus.VERIFYING.value


# ---------------------------------------------------------------------------
# Clara's third review of #774 (7c3e4ea).
# ---------------------------------------------------------------------------

# ---- 1. verify the CURRENT request before opening a new round ------------------------------------

def test_an_old_snapshot_cannot_open_a_round_after_a_human_returns_the_task_with_new_instructions(tmp_path, monkeypatch):
    db = str(tmp_path / "jobs.db")
    old = make_task(description="Please send me the declarations page.")
    disc, owners = Discussions(), Owners()
    holder = {"report": _report(old, message_id="m1")}
    _wire_intake(monkeypatch, None, disc, owners, {"on": True})
    monkeypatch.setattr(intake, "fetch_latest_task_report", lambda service: holder["report"])
    assert intake.run_intake(db_path=db) == 0
    job = _find_by_idempotency_key(JobStore(db), task_idempotency_key(old.task_id))
    assert job["status"] == JobStatus.COMPLETE.value and len(disc.posts) == 1
    # A human returns the task to Robie with CHANGED instructions...
    owners.assignee = "Robie AI"
    owners.live_description = "Please send me the ID card instead."
    # ...but the report that arrives next is an OLD snapshot: Robie's old wording, a later
    # Last Modified, received after the handback.
    holder["report"] = _report(make_task(description=old.description, last_modified="2026-10-05T09:00:00"),
                               message_id="m2")
    assert intake.run_intake(db_path=db) == 0
    same = JobStore(db).get_job(job["id"])
    assert same["status"] == JobStatus.COMPLETE.value and int(same["payload"].get("round") or 0) == 0
    assert len(disc.posts) == 1, "worked an old snapshot's instructions"
    # A report carrying the real current instructions does open the round, with THOSE instructions.
    holder["report"] = _report(make_task(description=owners.live_description,
                                         last_modified="2026-10-05T10:00:00"), message_id="m3")
    assert intake.run_intake(db_path=db) == 0
    final = JobStore(db).get_job(job["id"])
    assert final["status"] == JobStatus.COMPLETE.value and int(final["payload"]["round"]) == 1
    assert final["payload"]["description"] == owners.live_description
    assert len(disc.posts) == 2


def test_an_unreadable_live_request_never_opens_a_round(tmp_path, monkeypatch):
    db = str(tmp_path / "jobs.db")
    task, disc, owners = make_task(), Discussions(), Owners()
    holder = {"report": _report(task, message_id="m1")}
    _wire_intake(monkeypatch, None, disc, owners, {"on": True})
    monkeypatch.setattr(intake, "fetch_latest_task_report", lambda service: holder["report"])
    assert intake.run_intake(db_path=db) == 0
    job = _find_by_idempotency_key(JobStore(db), task_idempotency_key(task.task_id))
    owners.assignee = "Robie AI"
    owners.state_error = RuntimeError("the task description field is not on the page")
    holder["report"] = _report(make_task(last_modified="2026-10-05T09:00:00"), message_id="m2")
    assert intake.run_intake(db_path=db) == 0
    assert JobStore(db).get_job(job["id"])["status"] == JobStatus.COMPLETE.value
    assert len(disc.posts) == 1


class _Field:
    def __init__(self, value, count=1):
        self.value, self.n = value, count
    def count(self): return self.n
    def input_value(self): return self.value
    def text_content(self): return self.value


class _Panel:
    def __init__(self, fields): self.fields = fields
    def get_by_label(self, name, exact): return self.fields.get(name, _Field("", count=0))


# ---- 2. the Test task restriction covers stale recovery and the manual resume command --------------

def test_stale_recovery_skips_jobs_the_restriction_excludes(store, monkeypatch):
    kept, _ = ensure_task_job(store, make_task(task_id="1001"))
    excluded, _ = ensure_task_job(store, make_task(task_id="1002"))
    for job in (kept, excluded):
        store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
        _age(store, job["id"], 3)
    monkeypatch.setenv("ROBIE_TASK_INTAKE_ALLOWED_TASK_IDS", "1001")
    assert intake.recover_stale_running(store) == [kept["id"]]
    assert store.get_job(excluded["id"])["status"] == JobStatus.RUNNING.value


def test_stale_recovery_in_a_test_environment_without_a_restriction_does_nothing(store, monkeypatch):
    job, _ = ensure_task_job(store, make_task())
    store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
    _age(store, job["id"], 3)
    monkeypatch.setenv("ROBIE_ENV", "TEST")
    monkeypatch.delenv("ROBIE_TASK_INTAKE_ALLOWED_TASK_IDS", raising=False)
    assert intake.recover_stale_running(store) == []


@pytest.mark.parametrize("kwargs", [
    {}, {"assign_to": "Mike Sosa"}, {"note_id": "note-1"}, {"allow_retry_save": True},
])
def test_manual_resume_refuses_excluded_jobs_and_changes_nothing(store, monkeypatch, kwargs):
    disc, owners = Discussions(), Owners(fail_before_save=TimeoutError("page died"))
    first = _work(store, _worker(disc, owners), make_task())
    assert first["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    before = {kind: store.get_checkpoint(first["id"], kind) for kind in
              ("task-reassign-intent:0", "human-answer:0", "task-note-intent:0:handoff")}
    monkeypatch.setenv("ROBIE_TASK_INTAKE_ALLOWED_TASK_IDS", "70000001")
    assert intake.resume_task("63429523", db_path=store.path, **kwargs) == 1
    assert store.get_job(first["id"])["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    after = {kind: store.get_checkpoint(first["id"], kind) for kind in before}
    assert after == before, "an excluded job's permissions were changed"


def test_manual_resume_in_a_test_environment_requires_the_restriction(store, monkeypatch):
    disc, owners = Discussions(), Owners()
    first = _work(store, _worker(disc, None), make_task())
    monkeypatch.setenv("ROBIE_ENV", "TEST")
    monkeypatch.delenv("ROBIE_TASK_INTAKE_ALLOWED_TASK_IDS", raising=False)
    assert intake.resume_task("63429523", db_path=store.path) == 1
    assert store.get_job(first["id"])["status"] == JobStatus.AWAITING_HUMAN_INPUT.value


def test_manual_resume_still_works_for_an_allowed_job(store, monkeypatch):
    disc, owners = Discussions(), Owners()
    first = _work(store, _worker(disc, None), make_task())
    monkeypatch.setenv("ROBIE_TASK_INTAKE_ALLOWED_TASK_IDS", "63429523")
    assert intake.resume_task("63429523", db_path=store.path) == 0
    assert store.get_job(first["id"])["status"] == JobStatus.PENDING.value


# ---- 3. a wrong (nonexistent) adopted note ID must not stick -------------------------------------------

def test_a_nonexistent_adopted_note_id_can_be_corrected(store):
    disc, owners = Discussions(), Owners()
    first = _uncertain_note_job(store, disc, owners)
    assert intake.resume_task("63429523", db_path=store.path, note_id="note-999") == 0
    wrong = _worker(disc, owners).process_job(store, store.get_job(first["id"]))
    assert wrong["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    assert intake.resume_task("63429523", db_path=store.path, note_id="note-1") == 0, \
        "the wrong ID stayed stuck on the record"
    right = _worker(disc, owners).process_job(store, store.get_job(first["id"]))
    assert right["status"] == JobStatus.VERIFYING.value, right.get("last_error")
    assert len(disc.posts) == 1


# ---- live calls stay separately gated: only OWED recovery force-disables them ------------------------------

def test_a_normal_pass_keeps_main_call_wiring_and_only_owed_recovery_disables_it(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "jobs.db")
    monkeypatch.setattr(intake, "_build_discussion_client", lambda: Discussions())
    monkeypatch.setattr(intake, "PlaywrightTaskReassigner", lambda: Owners())
    monkeypatch.setattr(intake, "reassign_enabled", lambda: True)
    monkeypatch.setattr("robie_job_engine.bland_prod_wiring.build_call_dependencies",
                        lambda: ("PHONE", "BLAND", "TRANSFER", True))
    normal, _ = intake._build_worker_and_engine(store)
    assert (normal.phone_lookup, normal.bland_client, normal.call_dry_run) == ("PHONE", "BLAND", True)
    owed, _ = intake._build_worker_and_engine(store, allow_calls=False)
    assert owed.phone_lookup is None and owed.bland_client is None


# ---------------------------------------------------------------------------
# Clara's fourth review (cb6d949): the current-request proof must cover every
# consequential field, not just the assignee and description.
# ---------------------------------------------------------------------------

def test_an_older_report_naming_the_previous_producer_cannot_hand_the_task_back_to_them(tmp_path, monkeypatch):
    db = str(tmp_path / "jobs.db")
    original = make_task(created_by="", assigned_producer="Mike Sosa", csr="")
    disc, owners = Discussions(), Owners()
    owners.live_fields = {"created_by": "", "assigned_producer": "Mike Sosa", "csr": ""}
    holder = {"report": _report(original, message_id="m1")}
    _wire_intake(monkeypatch, None, disc, owners, {"on": True})
    monkeypatch.setattr(intake, "fetch_latest_task_report", lambda service: holder["report"])
    assert intake.run_intake(db_path=db) == 0
    job = _find_by_idempotency_key(JobStore(db), task_idempotency_key(original.task_id))
    assert job["status"] == JobStatus.COMPLETE.value and owners.calls == ["Mike Sosa"]
    # A human returns the task with the SAME description but Producer A (Mike) changed to B (Jazmin).
    owners.assignee = "Robie AI"
    owners.live_fields = {"created_by": "", "assigned_producer": "Jazmin Molina", "csr": ""}
    # An older report arrives afterward, still naming A, received after the handback.
    holder["report"] = _report(make_task(created_by="", assigned_producer="Mike Sosa", csr="",
                                         last_modified="2026-10-05T09:00:00"), message_id="m2")
    assert intake.run_intake(db_path=db) == 0
    same = JobStore(db).get_job(job["id"])
    assert same["status"] == JobStatus.COMPLETE.value and int(same["payload"].get("round") or 0) == 0
    assert owners.calls == ["Mike Sosa"], "handed the task back to the former producer"
    assert len(disc.posts) == 1
    # A report that names the real current producer opens the round, and the task goes to B.
    holder["report"] = _report(make_task(created_by="", assigned_producer="Jazmin Molina", csr="",
                                         last_modified="2026-10-05T10:00:00"), message_id="m3")
    assert intake.run_intake(db_path=db) == 0
    final = JobStore(db).get_job(job["id"])
    assert final["status"] == JobStatus.COMPLETE.value and int(final["payload"]["round"]) == 1
    assert owners.calls == ["Mike Sosa", "Jazmin Molina"]


def _confirm(monkeypatch, owners, task):
    monkeypatch.setattr(intake, "PlaywrightTaskReassigner", lambda: owners)
    return intake._confirm_returned(task)


def test_the_return_check_passes_only_when_every_consequential_field_matches(monkeypatch):
    task = make_task(created_by="Carlo Ferrara", assigned_producer="Mike Sosa", csr="Jazmin Molina",
                     description="Send the dec page", )
    owners = Owners(assignee="Robie AI")
    owners.live_fields = {"assigned_producer": "Mike Sosa", "csr": "Jazmin Molina"}
    assert _confirm(monkeypatch, owners, task) is True


@pytest.mark.parametrize("field,live", [
    ("created_by", "Someone Else"), ("assigned_producer", "Jazmin Molina"),
    ("csr", "Jazmin Molina"), ("activity_labels", "Robie Call"),
])
def test_a_live_routing_field_that_differs_from_the_report_row_is_a_stale_snapshot(monkeypatch, field, live):
    task = make_task(created_by="Carlo Ferrara", assigned_producer="Mike Sosa", csr="", description="Send the dec page")
    owners = Owners(assignee="Robie AI")
    owners.live_fields = {"assigned_producer": "Mike Sosa", field: live}
    assert _confirm(monkeypatch, owners, task) is False


@pytest.mark.parametrize("field", ["created_by", "assigned_producer", "csr", "activity_labels", "description"])
def test_a_consequential_field_the_live_read_cannot_return_is_unproven(monkeypatch, field):
    task = make_task(created_by="Carlo Ferrara", assigned_producer="Mike Sosa", csr="", description="Send the dec page")
    owners = Owners(assignee="Robie AI")
    owners.live_fields = {"assigned_producer": "Mike Sosa"}
    owners.hide_fields = (field,)
    assert _confirm(monkeypatch, owners, task) is False


def test_blank_routing_fields_on_both_sides_still_match(monkeypatch):
    task = make_task(created_by="", assigned_producer="", csr="", description="Send the dec page")
    owners = Owners(assignee="Robie AI")
    owners.live_fields = {"created_by": ""}
    assert _confirm(monkeypatch, owners, task) is True


class _Scope:
    """A page or dialog whose labelled fields are known; anything else has no match."""
    def __init__(self, fields): self.fields = fields
    def get_by_label(self, name, exact): return self.fields.get(name, _Field("", count=0))


# ---------------------------------------------------------------------------
# Clara's fifth review (8066e87): fresh routing proof before EVERY Save, an
# ENFORCED field contract (no guessed selectors), and no blank-on-error reads.
# ---------------------------------------------------------------------------
import json  # noqa: E402

DEFAULT_LIVE = {"assignee": "Robie AI", "description": "Please call the client about their quote.",
                "created_by": "Carlo Ferrara", "assigned_producer": "", "csr": "", "activity_labels": ""}


def _verified(label, *, scope="dialog", read="input_value", column="Note"):
    return {"scope": scope, "label": label, "read": read, "meaning": f"observed meaning of {label}",
            "report_column": column,
            "verified": {"environment": "TEST", "applicant_id": "220250093", "observed_on": "2026-10-06",
                         "observed_by": "inspector", "evidence": "inspection-2026-10-06.json"}}


def _contract_file(tmp_path, monkeypatch, overrides=None, drop=()):
    fields = {
        "description": _verified("Instructions (observed)"),
        "created_by": _verified("Created by (observed)", read="text_content", column="Task Created By"),
        "assigned_producer": _verified("Producer (observed)", scope="page", read="text_content", column="Assigned Producer"),
        "csr": _verified("CSR (observed)", scope="page", read="text_content", column="CSR"),
        "activity_labels": _verified("Labels (observed)", read="text_content", column="Activity Labels"),
    }
    fields.update(overrides or {})
    for name in drop:
        fields.pop(name, None)
    path = tmp_path / "contract.json"
    path.write_text(json.dumps({"schema_version": 1, "status": "ESTABLISHED", "fields": fields}))
    monkeypatch.setenv(cdp.CONTRACT_PATH_ENV, str(path))
    return path


# ---- A. fresh routing proof immediately before every Save ----------------------------------------

def test_the_first_round_save_needs_a_fresh_live_routing_proof(store):
    disc, owners = Discussions(), Owners()
    owners.live_fields = {"created_by": "Someone Else"}  # the report row names Carlo; live says otherwise
    result = _work(store, _worker(disc, owners), make_task())
    assert result["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    assert owners.attempts == [] and disc.posts == []
    assert store.get_checkpoint(result["id"], "task-reassign-intent:0") is None, \
        "a refused proof must not look like an unknown Save"


def test_an_unreadable_routing_field_blocks_the_save(store):
    disc, owners = Discussions(), Owners()
    owners.state_error = cdp.ReassignError("assigned_producer could not be read")
    result = _work(store, _worker(disc, owners), make_task())
    assert result["status"] == JobStatus.AWAITING_HUMAN_INPUT.value and owners.attempts == []


@pytest.mark.parametrize("field", ["created_by", "assigned_producer", "csr", "activity_labels", "description"])
def test_a_routing_field_the_live_read_omits_blocks_the_save(store, field):
    disc, owners = Discussions(), Owners()
    owners.hide_fields = (field,)
    result = _work(store, _worker(disc, owners), make_task())
    assert result["status"] == JobStatus.AWAITING_HUMAN_INPUT.value and owners.attempts == []


def test_a_live_owner_other_than_robie_blocks_the_save(store):
    disc, owners = Discussions(), Owners()
    owners.live_fields = {"assignee": "Someone Else"}
    result = _work(store, _worker(disc, owners), make_task())
    assert result["status"] == JobStatus.AWAITING_HUMAN_INPUT.value and owners.attempts == []


def test_a_human_chosen_return_owner_does_not_depend_on_report_routing_fields(store):
    disc, owners = Discussions(), Owners()
    task = make_task(created_by="", assigned_producer="", csr="")
    first = _work(store, _worker(disc, owners), task)
    assert first["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    assert intake.resume_task(task.task_id, db_path=store.path, assign_to="Mike Sosa") == 0
    owners.state_error = cdp.ReassignError("routing fields are unreadable")  # irrelevant to a human's choice
    second = _worker(disc, owners).process_job(store, store.get_job(first["id"]))
    assert second["status"] == JobStatus.VERIFYING.value, second.get("last_error")
    assert owners.calls == ["Mike Sosa"]


def test_the_proof_runs_again_before_an_authorized_retry(store):
    disc, owners = Discussions(), Owners(fail_before_save=TimeoutError("page died"))
    first = _work(store, _worker(disc, owners), make_task())
    assert first["status"] == JobStatus.AWAITING_HUMAN_INPUT.value and len(owners.attempts) == 1
    assert intake.resume_task("63429523", db_path=store.path, allow_retry_save=True) == 0
    owners.live_fields = {"created_by": "Someone Else"}  # routing changed since the first attempt
    again = _worker(disc, owners).process_job(store, store.get_job(first["id"]))
    assert again["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    assert len(owners.attempts) == 1, "a retry Save went out on stale routing"


def test_the_call_route_save_also_needs_the_live_proof():
    port = Mock()
    port.reassign.return_value = "Carlo Ferrara"
    port.read_assignee.return_value = "Carlo Ferrara"
    task = make_task()
    port.read_task_state.return_value = {**DEFAULT_LIVE, "created_by": "Someone Else"}
    result = _WorkerReassignPortAdapter(port, task).reassign_task(task.task_id, "Carlo Ferrara", "n")
    assert result["ok"] is False and result["sent"] is False
    port.reassign.assert_not_called()
    port.read_task_state.return_value = dict(DEFAULT_LIVE)
    ok = _WorkerReassignPortAdapter(port, task).reassign_task(task.task_id, "Carlo Ferrara", "n")
    assert ok["ok"] is True
    port.reassign.assert_called_once()


# ---- B. the field contract is ENFORCED, and nothing guesses a selector ------------------------------

def test_no_guessed_labels_remain_in_the_code():
    for name in ("TASK_DESCRIPTION_LABELS", "CONSEQUENTIAL_FIELD_LABELS", "unverified_dom_contract"):
        assert not hasattr(cdp, name), f"{name} is a guessed selector or descriptive-only metadata"


def test_the_shipped_field_contract_declares_nothing_verified(monkeypatch):
    monkeypatch.delenv(cdp.CONTRACT_PATH_ENV, raising=False)
    status = cdp.field_contract_status()
    assert status["established"] is False
    assert set(status["missing"]) == set(cdp.REQUIRED_FIELDS)


def test_reading_task_state_is_refused_before_any_browser_use_until_the_contract_is_established(monkeypatch):
    monkeypatch.delenv(cdp.CONTRACT_PATH_ENV, raising=False)
    with pytest.raises(cdp.FieldContractNotEstablished):
        cdp.PlaywrightTaskReassigner().read_task_state("63429523", "220250093")  # a browser use would AssertionError


def test_reassignment_stays_disabled_until_the_contract_is_established(store, monkeypatch):
    monkeypatch.delenv(cdp.CONTRACT_PATH_ENV, raising=False)
    monkeypatch.setenv("EZLYNX_TASK_REASSIGN_ENABLED", "1")
    disc = Discussions()
    result = _work(store, _worker(disc, cdp.PlaywrightTaskReassigner()), make_task())
    assert result["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    assert disc.posts == []


def test_a_contract_missing_a_required_field_is_not_established(tmp_path, monkeypatch):
    _contract_file(tmp_path, monkeypatch, drop=("csr",))
    status = cdp.field_contract_status()
    assert status["established"] is False and status["missing"] == ["csr"]


@pytest.mark.parametrize("broken", [
    {"label": ""}, {"read": "innerText"}, {"scope": "frame"}, {"verified": {}},
    {"verified": {"environment": "TEST", "observed_on": "2026-10-06", "observed_by": "", "evidence": "x"}},
])
def test_a_malformed_or_unobserved_entry_is_rejected(tmp_path, monkeypatch, broken):
    entry = {**_verified("Producer (observed)", scope="page", read="text_content"), **broken}
    _contract_file(tmp_path, monkeypatch, overrides={"assigned_producer": entry})
    assert "assigned_producer" in cdp.field_contract_status()["missing"]


def test_only_declared_selectors_are_ever_used(tmp_path, monkeypatch):
    _contract_file(tmp_path, monkeypatch)
    asked = []

    class Scope:
        def __init__(self, fields): self.fields = fields
        def get_by_label(self, name, exact):
            asked.append(name)
            return self.fields.get(name, _Field("", count=0))

    panel = Scope({"Instructions (observed)": _Field("Send the dec page"),
                   "Created by (observed)": _Field("Carlo Ferrara"), "Labels (observed)": _Field("")})
    page = Scope({"Producer (observed)": _Field("Mike Sosa"), "CSR (observed)": _Field("")})
    state = cdp._read_consequential_fields(panel, page)
    assert state == {"description": "Send the dec page", "created_by": "Carlo Ferrara",
                     "assigned_producer": "Mike Sosa", "csr": "", "activity_labels": ""}
    assert set(asked) == {"Instructions (observed)", "Created by (observed)", "Labels (observed)",
                          "Producer (observed)", "CSR (observed)"}


# ---- C. an unreadable field is an error, never a blank --------------------------------------------------

class _Raising:
    def __init__(self, count=1): self.n = count
    def count(self): return self.n
    def input_value(self): raise RuntimeError("element detached")
    def text_content(self): raise RuntimeError("element detached")


class _NoneText:
    def count(self): return 1
    def text_content(self): return None


def _scopes(producer):
    panel = type("S", (), {"get_by_label": lambda self, name, exact: {
        "Instructions (observed)": _Field("Send the dec page"), "Created by (observed)": _Field("Carlo"),
        "Labels (observed)": _Field("")}.get(name, _Field("", count=0))})()
    page = type("S", (), {"get_by_label": lambda self, name, exact: {
        "Producer (observed)": producer, "CSR (observed)": _Field("")}.get(name, _Field("", count=0))})()
    return panel, page


def test_a_read_that_raises_is_not_a_blank_field(tmp_path, monkeypatch):
    _contract_file(tmp_path, monkeypatch)
    with pytest.raises(cdp.ReassignError, match="assigned_producer"):
        cdp._read_consequential_fields(*_scopes(_Raising()))


def test_text_content_of_none_is_unreadable_not_blank(tmp_path, monkeypatch):
    _contract_file(tmp_path, monkeypatch)
    with pytest.raises(cdp.ReassignError, match="assigned_producer"):
        cdp._read_consequential_fields(*_scopes(_NoneText()))


def test_a_successful_empty_read_is_a_real_blank(tmp_path, monkeypatch):
    _contract_file(tmp_path, monkeypatch)
    state = cdp._read_consequential_fields(*_scopes(_Field("")))
    assert state["assigned_producer"] == ""


def test_an_unreadable_producer_means_the_return_is_unproven(monkeypatch):
    owners = Owners(assignee="Robie AI")
    owners.state_error = cdp.ReassignError("assigned_producer could not be read")
    monkeypatch.setattr(intake, "PlaywrightTaskReassigner", lambda: owners)
    assert intake._confirm_returned(make_task(created_by="Carlo Ferrara")) is False


# ---- D. the read-only Test inspection ----------------------------------------------------------------------
class _LazyInspector:
    """Imported on first use so a missing module fails each inspector test, not the whole file."""
    def __getattr__(self, name):
        import importlib
        return getattr(importlib.import_module("robie_job_engine.task_field_inspector"), name)


inspector = _LazyInspector()


class _InspectPanel:
    def __init__(self, log): self.log = log
    def evaluate(self, script):
        self.log.append("panel.evaluate")
        return [{"tag": "input", "role": "textbox", "name": "Instructions (observed)", "value": "Send the dec page",
                 "editable": True, "visible": True}]
    def click(self, *a, **k): self.log.append("PANEL CLICK")


class _InspectPage:
    def __init__(self, log): self.log = log
    def evaluate(self, script):
        self.log.append("page.evaluate")
        return [{"tag": "div", "role": "", "name": "Producer", "value": "Mike Sosa", "editable": False, "visible": True}]
    def click(self, *a, **k): self.log.append("PAGE CLICK")


def _inspect_env(monkeypatch, task_id="63429523", env="TEST"):
    monkeypatch.setenv("ROBIE_ENV", env)
    monkeypatch.setenv("ROBIE_TASK_INTAKE_ALLOWED_TASK_IDS", task_id)


def test_the_inspector_refuses_outside_test_other_applicants_and_unlisted_tasks(monkeypatch, tmp_path):
    out = tmp_path / "observation.json"
    _inspect_env(monkeypatch, env="PRODUCTION")
    with pytest.raises(inspector.InspectionRefused):
        inspector.run_dom_inspection(task_id="63429523", applicant_id="220250093", output_path=out)
    _inspect_env(monkeypatch)
    with pytest.raises(inspector.InspectionRefused):
        inspector.run_dom_inspection(task_id="63429523", applicant_id="25486692", output_path=out)
    with pytest.raises(inspector.InspectionRefused):
        inspector.run_dom_inspection(task_id="70000001", applicant_id="220250093", output_path=out)
    assert not out.exists()


def test_the_inspector_only_reads_and_never_saves(monkeypatch, tmp_path):
    _inspect_env(monkeypatch)
    log = []
    from contextlib import contextmanager

    @contextmanager
    def fake_page():
        yield _InspectPage(log)

    monkeypatch.setattr(cdp, "_browser_page", fake_page)
    monkeypatch.setattr(cdp, "_goto_activity", lambda page, applicant_id: log.append("goto"))
    monkeypatch.setattr(cdp, "_search_and_open_edit", lambda page, t, a: (log.append("open"), _InspectPanel(log))[1])
    monkeypatch.setattr(cdp, "_cancel_dialog", lambda panel: log.append("cancel"))
    out = tmp_path / "observation.json"
    record = inspector.run_dom_inspection(task_id="63429523", applicant_id="220250093", output_path=out)
    assert "CLICK" not in " ".join(log) and log[-1] == "cancel"
    saved = json.loads(out.read_text())
    assert saved["environment"] == "TEST" and saved["task_id"] == "63429523"
    assert saved["dialog_elements"][0]["name"] == "Instructions (observed)"
    assert saved["page_elements"][0]["name"] == "Producer"
    assert saved["meaning_review"]["status"] == "PENDING HUMAN REVIEW"
    assert all(value is None for value in saved["meaning_review"]["fields"].values())
    assert record["meaning_review"]["status"] == "PENDING HUMAN REVIEW"
    import importlib
    import inspect as _inspect
    source = _inspect.getsource(importlib.import_module("robie_job_engine.task_field_inspector"))
    assert "_click_save" not in source and ".reassign(" not in source and "append_note" not in source


def test_the_api_inspection_reads_only_and_summarizes_the_note_shape(monkeypatch, tmp_path):
    _inspect_env(monkeypatch)
    class Client:
        def get_discussion(self, discussion_id):
            return {"id": discussion_id, "lastModified": "2026-10-06T10:00:00Z",
                    "notes": [{"id": "1", "type": "TaskCreationNote", "body": "Send the dec page",
                               "createdDate": "2026-10-06T09:00:00Z", "task": {"assignedUserId": 5}}]}
        def append_note(self, *a, **k): raise AssertionError("the inspection wrote a note")
    out = tmp_path / "api.json"
    record = inspector.run_api_inspection(client=Client(), task_id="63429523", applicant_id="220250093",
                                          discussion_id="849945654", output_path=out)
    assert "type" in record["note_keys"] and "createdDate" in record["note_keys"]
    assert "task.assignedUserId" in record["note_keys"]
    assert record["meaning_review"]["status"] == "PENDING HUMAN REVIEW"


def test_the_save_method_itself_refuses_until_the_contract_is_established(monkeypatch):
    monkeypatch.delenv(cdp.CONTRACT_PATH_ENV, raising=False)
    monkeypatch.setenv(cdp.REASSIGN_GATE_ENV, "1")
    with pytest.raises(cdp.FieldContractNotEstablished):  # a browser use would AssertionError
        cdp.PlaywrightTaskReassigner().reassign("63429523", "220250093", "Carlo Ferrara")


# ---------------------------------------------------------------------------
# A task that names its discussion posts there even when it has no title.
# The no-note-id rule is unchanged: a POST that returns no note id is never
# reposted and never confirmed from text.


def test_pinned_untitled_discussion_receives_and_confirms_the_note(context):
    store, job = context
    client = FakeDiscussionClient(title="")
    assert post(client, store, job) == "note-123"
    assert client.posts == [("849945654", "one intent")]


def test_post_without_a_note_id_is_held_and_never_reposted_even_untitled(context):
    store, job = context
    client = FakeDiscussionClient(title="", return_note_id=False)
    with pytest.raises(UnverifiedNoteError, match="No durable destination note ID"):
        post(client, store, job)
    with pytest.raises(UnverifiedNoteError):
        post(client, JobStore(store.path), job)
    assert len(client.posts) == 1
