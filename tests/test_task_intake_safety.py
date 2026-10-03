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
    url = cdp._activity_url("25486692")
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
    attrs = {"data-task-id": "63429523", "data-applicant-id": "25486692"}
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
        cdp._search_and_open_edit(page, "63429523", "25486692")
    assert page.rows.clicks == 0


def test_duplicate_descriptions_different_ids_select_exact_only():
    page = dom()
    # Selector intentionally has no description/text API; unrelated duplicate
    # descriptions cannot become candidates or authorize a page-wide Edit.
    assert cdp._search_and_open_edit(page, "63429523", "25486692") is page.panel
    assert page.rows.children[("button", "Edit this task")].clicks == 1


@pytest.mark.parametrize("where", ["url", "row", "panel"])
def test_foreign_applicant_refused(where):
    page = dom()
    if where == "url": page.url = cdp._activity_url("999")
    else: (page.rows if where == "row" else page.panel).attrs["data-applicant-id"] = "999"
    with pytest.raises(cdp.ReassignError):
        cdp._search_and_open_edit(page, "63429523", "25486692")
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
        cdp.PlaywrightTaskReassigner().reassign("63429523", "25486692", "Carlo Ferrara")
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
