"""DocumentApi search reads every page, and callers fail closed on a partial list.

Regression: search_applicant_documents returned only the first page (30
rows on Production), so read-back, the CLI same-name check, and the
filer's earlier-upload check could miss a document on a large client.
"""

from __future__ import annotations

import json
from argparse import Namespace
from urllib.parse import parse_qs, urlparse

import pytest

from robie_job_engine.ezlynx_api import EzlynxApiClient, EzlynxApiConfig, document_search_is_complete
from robie_job_engine.ezlynx_api_only_writes import EzlynxNoteDocReadbackError, confirm_uploaded_document_id


def _config() -> EzlynxApiConfig:
    return EzlynxApiConfig(
        token_endpoint="https://app.uatezlynx.com/auth/connect/token",
        document_base_url="https://app.uatezlynx.com/DocumentApi/",
        client_id="cid", client_secret="csecret", username="api_user",
        integration_group_id="183", scope="DocumentApi openid",
    )


class _Resp:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode()
        self.headers = {"Content-Type": "application/json"}
        self.status = 200

    def read(self) -> bytes:
        return self._body

    def getcode(self) -> int:
        return 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _book(total: int, page_size: int = 30, first_index: int = 1, *, echo: bool = True, ignore_paging: bool = False):
    """Fake EZLynx: newest first, ``total`` documents, one page per request."""

    ids = list(range(900000 + total, 900000, -1))
    calls: list[str] = []

    def urlopen(url, *, data, headers, timeout):
        if "connect/token" in url:
            return _Resp({"access_token": "t", "expires_in": 3600})
        calls.append(url)
        query = parse_qs(urlparse(url).query)
        index = first_index if ignore_paging or "pageIndex" not in query else int(query["pageIndex"][0])
        start = (index - first_index) * page_size
        rows = [{"id": i, "name": f"doc {i}"} for i in ids[start:start + page_size]]
        return _Resp({"pageIndex": index if echo else 0, "pageSize": page_size, "totalSize": total, "results": rows})

    return EzlynxApiClient(_config(), urlopen=urlopen), calls, ids


@pytest.mark.parametrize("first_index", [0, 1])
def test_reads_every_page_whatever_the_first_index(first_index):
    client, calls, ids = _book(95, first_index=first_index)
    payload = client.search_applicant_documents("220250093")
    assert [r["id"] for r in payload["results"]] == ids
    assert payload["complete"] is True and payload["pages_read"] == 4
    assert "pageIndex" not in calls[0]  # the first call is the proven one, unchanged


def test_single_page_client_makes_one_request():
    client, calls, _ = _book(12)
    payload = client.search_applicant_documents("220250093")
    assert len(calls) == 1 and payload["complete"] is True and len(payload["results"]) == 12


def test_paging_ignored_by_server_is_reported_incomplete():
    client, calls, _ = _book(95, ignore_paging=True)
    payload = client.search_applicant_documents("220250093")
    assert len(payload["results"]) == 30 and payload["complete"] is False
    assert len(calls) == 2


def test_page_without_matching_echo_is_not_trusted():
    client, _, _ = _book(95, echo=False)
    payload = client.search_applicant_documents("220250093")
    assert len(payload["results"]) == 30 and document_search_is_complete(payload) is False


def test_readback_finds_a_document_on_a_later_page():
    client, _, ids = _book(95)
    oldest = str(ids[-1])
    assert confirm_uploaded_document_id(client, "220250093", oldest)["read_back"] is True


def test_readback_on_incomplete_list_says_unverified_not_missing():
    client, _, _ = _book(95, ignore_paging=True)
    with pytest.raises(EzlynxNoteDocReadbackError, match="UNVERIFIED.*incomplete.*Do not upload again"):
        confirm_uploaded_document_id(client, "220250093", "123")


def test_payload_without_paging_fields_counts_as_complete():
    assert document_search_is_complete({"results": []}) is True
    assert document_search_is_complete([{"id": 1}]) is True
    assert document_search_is_complete({"results": [], "complete": False}) is False


# ------------------------------------------------------------- callers
class _Port:
    def __init__(self, rows, complete, total):
        self.listing = {"rows": rows, "complete": complete, "total": total}

    def document_listing(self, applicant):
        return self.listing


def test_cli_docs_list_reports_completeness():
    from robie_job_engine.ezlynx_api_cli import cmd_docs_list

    svc = Namespace(port=_Port([{"id": "1", "name": "a"}], False, 95))
    out = cmd_docs_list(svc, Namespace(applicant="220250093", name_contains="", limit=50))
    assert out.result["complete"] is False and out.result["total_on_file"] == 95


def test_filer_will_not_reupload_when_list_is_incomplete():
    from robie_job_engine import robie_filer as rf

    class Api:
        def search_applicant_documents(self, applicant):
            return {"results": [{"id": 1, "name": "other"}], "totalSize": 95, "complete": False}

    with pytest.raises(rf.FilerError, match="cannot be checked; not uploading again"):
        rf.Ezlynx(Api()).find_document("220250093", "dec.pdf")


def test_filer_adopts_a_match_found_on_the_merged_list():
    from robie_job_engine import robie_filer as rf

    class Api:
        def search_applicant_documents(self, applicant):
            return {"results": [{"id": 777, "name": "dec.pdf"}], "totalSize": 95, "complete": False}

    assert rf.Ezlynx(Api()).find_document("220250093", "dec.pdf") == "777"
