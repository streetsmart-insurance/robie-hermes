import copy
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from robie_job_engine.plaid_bank_reader import (
    AccountScope, Credentials, PaginationChanged, PlaidHold, SandboxTransport,
    collect_transaction_delta, hosted_link_request, load_sandbox_credentials,
)

NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)
SCOPE = AccountScope("synthetic-entity", "synthetic-item", "synthetic-bank", ("a",))


def page(cursor="end", more=False, **updates):
    result = dict(added=[], modified=[], removed=[], next_cursor=cursor,
                  has_more=more, transactions_update_status="HISTORICAL_UPDATE_COMPLETE")
    result.update(updates)
    return result


def row(txid="t", account="a", pending=False):
    return dict(transaction_id=txid, account_id=account, pending=pending,
                amount=-10.25, original_description="SYNTHETIC deposit")


class Source:
    def __init__(self, pages=None):
        self.pages = pages or [page()]
        self.calls = []
        self.item = dict(item_id=SCOPE.item_id, institution_id=SCOPE.institution_id, error=None)
        self.status = {"transactions": {"last_successful_update": NOW.isoformat()}}
        self.accounts = [dict(account_id="a", type="depository", balances={"current": 5})]

    def __call__(self, path, fields):
        self.calls.append((path, copy.deepcopy(fields)))
        if path == "/item/get":
            return dict(item=self.item, status=self.status)
        if path == "/accounts/get":
            return dict(item=self.item, accounts=self.accounts)
        p = self.pages.pop(0)
        if isinstance(p, Exception):
            raise p
        return p


def collect(source, **kwargs):
    return collect_transaction_delta(source, SCOPE, cursor="start", now=NOW, **kwargs)


def test_complete_delta_preserves_pending_removals_raw_description_and_sign():
    src = Source([page("middle", True, added=[row(pending=True)]),
                  page(modified=[row("modified")], removed=[row("removed")])])
    result = collect(src)
    assert result.added[0]["pending"] is True
    assert result.added[0]["amount"] == -10.25
    assert result.added[0]["original_description"] == "SYNTHETIC deposit"
    assert result.removed[0]["transaction_id"] == "removed"
    assert result.next_cursor == "end" and len(result.source_pages) == 2
    assert not result.bank_clearing_proven and not result.durable_commit_proven
    assert "SYNTHETIC deposit" not in repr(result) and "end" not in repr(result)
    assert src.calls[-1][1]["cursor"] == "middle"


def test_mutation_discards_partial_delta_and_restarts_original_cursor_once():
    src = Source([page("partial", True, added=[row("discard")]),
                  PaginationChanged("provider token must not escape"),
                  page(added=[row("retained")])])
    result = collect(src)
    assert [r["transaction_id"] for r in result.added] == ["retained"]
    assert [fields["cursor"] for path, fields in src.calls if path.endswith("sync")] == ["start", "partial", "start"]


def test_repeated_mutation_stops():
    with pytest.raises(PlaidHold, match="source keeps changing"):
        collect(Source([PaginationChanged("secret"), PaginationChanged("secret")]))


@pytest.mark.parametrize("updates", [
    {"has_more": "false"}, {"next_cursor": ""}, {"added": None},
    {"transactions_update_status": "NOT_READY"},
    {"transactions_update_status": "INITIAL_UPDATE_COMPLETE"},
    {"added": [row("duplicate"), row("duplicate")]},
    {"added": [row(account="foreign")]},
    {"added": [dict(transaction_id="t", account_id="a")]},
])
def test_partial_malformed_or_unscoped_pages_never_return_cursor(updates):
    with pytest.raises(PlaidHold):
        collect(Source([page(**updates)]))


def test_cross_page_and_cross_group_duplicates_hold():
    with pytest.raises(PlaidHold):
        collect(Source([page("next", True, added=[row()]), page(removed=[row()])]))


def test_cursor_loop_and_page_cap_hold():
    with pytest.raises(PlaidHold, match="pagination loop"):
        collect(Source([page("start", True)]))
    with pytest.raises(PlaidHold, match="limit exceeded"):
        collect(Source([page("next", True)]), max_pages=1)


@pytest.mark.parametrize("field", ["item_id", "institution_id", "error"])
def test_wrong_item_bank_or_expired_auth_holds_before_transactions(field):
    src = Source(); src.item[field] = "wrong"
    with pytest.raises(PlaidHold):
        collect(src)
    assert all(path != "/transactions/sync" for path, _ in src.calls)


@pytest.mark.parametrize("accounts", [[], [{"account_id": "foreign", "type": "credit"}],
    [{"account_id": "a", "type": "credit"}, {"account_id": "a", "type": "credit"}],
    [{"account_id": "a", "type": "investment"}],
])
def test_account_scope_and_type_changes_hold(accounts):
    src = Source(); src.accounts = accounts
    with pytest.raises(PlaidHold):
        collect(src)


@pytest.mark.parametrize("updated", [None, "bad", "2026-01-02T00:00:00",
    (NOW - timedelta(days=2)).isoformat(), (NOW + timedelta(seconds=1)).isoformat()])
def test_missing_stale_future_naive_source_update_holds(updated):
    src = Source(); src.status["transactions"]["last_successful_update"] = updated
    with pytest.raises(PlaidHold):
        collect(src)


def test_newer_failed_update_and_initial_empty_pull_hold():
    src = Source(); src.status["transactions"]["last_failed_update"] = NOW.isoformat()
    with pytest.raises(PlaidHold):
        collect(src)
    assert collect(Source()).added == ()  # historical completion independently supplied


def test_provider_exception_text_does_not_escape():
    with pytest.raises(PlaidHold) as e:
        collect(Source([RuntimeError("private-token-description")]))
    assert "private-token" not in str(e.value)
    assert e.value.__suppress_context__


def test_expired_consent_and_provider_hold_are_sanitized():
    src = Source(); src.item["consent_expiration_time"] = NOW.isoformat()
    with pytest.raises(PlaidHold, match="consent expired"):
        collect(src)
    with pytest.raises(PlaidHold) as e:
        collect(Source([PlaidHold("provider-private-description")]))
    assert "provider-private" not in str(e.value)


def test_sandbox_http_allowlist_destination_and_no_redirect():
    from robie_job_engine.plaid_bank_reader import _NoRedirect
    t = SandboxTransport(Credentials("fake-client", "fake-secret", "access-sandbox-fake"))
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size): return b'{"request_id":"synthetic"}'
    with patch.object(t._opener, "open", return_value=Response()) as network:
        assert t("/item/get", {}) == {"request_id": "synthetic"}
        req = network.call_args.args[0]
        assert req.full_url == "https://sandbox.plaid.com/item/get"
        assert req.get_method() == "POST" and network.call_args.kwargs["timeout"] == 20
        assert json.loads(req.data)["access_token"] == "access-sandbox-fake"
    assert _NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid") is None


@pytest.mark.parametrize("cursor", ["now", None, "x" * 257])
def test_cursor_bounds(cursor):
    with pytest.raises(PlaidHold):
        collect_transaction_delta(Source(), SCOPE, cursor=cursor, now=NOW)


def test_credentials_are_pinned_test_only_and_redacted():
    class Accessor:
        def access(self, ref):
            return json.dumps(dict(environment="sandbox", client_id="synthetic-client",
                                   secret="synthetic-secret", access_token="access-sandbox-fake"))
    ref = "projects/synthetic-test/secrets/plaid-reader-test/versions/1"
    with patch.dict("os.environ", {"GOOGLE_APPLICATION_CREDENTIALS": ""}):
        c = load_sandbox_credentials(ref, Accessor())
    assert c.access_token == "access-sandbox-fake" and "fake" not in repr(c)
    for bad in (ref.replace("/1", "/latest"), ref.replace("-test/", "-prod/")):
        with pytest.raises(PlaidHold):
            load_sandbox_credentials(bad, Accessor())
    with patch.dict("os.environ", {"GOOGLE_APPLICATION_CREDENTIALS": "/tmp/synthetic-key.json"}):
        with pytest.raises(PlaidHold):
            load_sandbox_credentials(ref, Accessor())


def test_sandbox_transport_refuses_mutations_and_live_tokens_without_network():
    t = SandboxTransport(Credentials("client", "secret", "access-production-fake"))
    with patch.object(t._opener, "open") as network:
        for path, fields in (("/item/remove", {}), ("/link/token/create", {}),
                             ("/transactions/sync", {}), ("/item/get", {"secret": "override"})):
            with pytest.raises(PlaidHold):
                t(path, fields)
        network.assert_not_called()


def test_link_preparation_is_read_scope_only_no_delivery_or_effect():
    request = hosted_link_request(opaque_user_id="synthetic-user", client_name="Synthetic Books")
    assert request["products"] == ["transactions"]
    assert request["hosted_link"] == {} and request["transactions"]["days_requested"] == 730
    with pytest.raises(PlaidHold):
        hosted_link_request(opaque_user_id="", client_name="test")
