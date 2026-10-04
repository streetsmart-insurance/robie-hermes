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


def test_changed_assignee_never_overwritten(monkeypatch):
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
    def accepted_then_failed(*args):
        original(*args)
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
    def waiting(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
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
        self.show_text = show_text

    def inject_note(self, note_id, text):
        """A note somebody else wrote in the same discussion."""
        self.notes.append(note_id)
        self.texts[note_id] = text
        self.latest = note_id

    def get_discussion_ids(self, applicant_id):
        return list(self.ids)

    def append_note(self, discussion_id, body):
        self.posts.append((discussion_id, body))
        self.latest = f"note-{len(self.posts)}"
        self.notes.append(self.latest)
        self.texts[self.latest] = body
        return {"note_id": self.latest}

    def get_discussion(self, discussion_id):
        return {"title": "Task Note", "mostRecentNoteId": self.latest,
                "noteCount": 5 + len(self.notes),
                "notes": [({"id": n, "body": self.texts[n]} if self.show_text and n in self.texts
                           else {"id": n}) for n in self.notes]}


class Owners:
    def __init__(self, assignee="Robie AI", unresolved=(), crash_after_save=None, fail_before_save=None):
        self.assignee = assignee
        self.unresolved = set(unresolved)
        self.crash_after_save = crash_after_save
        self.fail_before_save = (list(fail_before_save) if isinstance(fail_before_save, (list, tuple))
                                 else ([fail_before_save] if fail_before_save else []))
        self.calls = []

    def reassign(self, task_id, applicant_id, new_assignee, description="", expected_assignee="Robie AI"):
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
        def append_note(self, *a): self.posts.append(a); return {"note_id": "n"}
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
    assert first["status"] == JobStatus.AWAITING_HUMAN_INPUT.value and len(disc.posts) == 1
    store.resume(first["id"])
    second = _worker(disc, owners).process_job(store, store.get_job(first["id"]))
    assert second["status"] == JobStatus.VERIFYING.value, second.get("last_error")
    assert len(disc.posts) == 2 and owners.calls == ["Carlo Ferrara"]
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
        store, make_task(last_modified="2026-10-04T09:00:00", description=second_ask))
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
    task = make_task(assigned_producer="Mike Sosa", csr="Jazmin Molina")
    job = _work(store, _worker(disc, owners), task)
    assert job["status"] == JobStatus.VERIFYING.value, job.get("last_error")
    assert owners.calls == ["Mike Sosa"]
    assert _verify(store, job["id"], disc, owners)["status"] == JobStatus.COMPLETE.value


def test_robie_is_never_its_own_return_target(store):
    disc, owners = Discussions(), Owners()
    task = make_task(created_by="Robie AI", assigned_producer="Mike Sosa")
    job = _work(store, _worker(disc, owners), task)
    assert owners.calls == ["Mike Sosa"] and job["status"] == JobStatus.VERIFYING.value


def test_verifier_rejects_a_return_owner_that_skipped_precedence(store):
    disc, owners = Discussions(), Owners()
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
    def accepted_then_timeout(*args):
        original(*args)
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
    def accepted_then_timeout(*args):
        original(*args)
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
    def accepted_then_timeout(*args):
        original(*args)
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
    def accepted_then_timeout(*args):
        original(*args)
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
    again, created = ensure_task_job(store, make_task(last_modified="2026-10-05T09:00:00"))
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
                                                last_modified="2026-10-05T09:00:00"))
    assert again["payload"]["round"] == 1
    third = _worker(disc, owners).process_job(store, again)
    assert third["status"] == JobStatus.AWAITING_HUMAN_INPUT.value, "reused an answer from an earlier round"
    assert owners.calls == ["Mike Sosa"]
