"""Multi-page Ascend lists through the real AscendApiClient HTTP path.

Only urlopen is mocked. The fake vendor serves the page-number contract
proven live on /users (``meta.next`` = next page number, null on the last
page) and honors ``page`` and ``page_size`` from the query string.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from robie_job_engine import ascend_sync
from robie_job_engine.ascend_sync import AscendApiClient

ROOT = Path(__file__).resolve().parents[1]


class _Resp:
    def __init__(self, body):
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeAscend:
    """Serves N rows per path in pages; records every requested URL."""

    def __init__(self, rows_by_path, *, ignore_page=False, cursor=False):
        self.rows = rows_by_path
        self.ignore_page = ignore_page
        self.cursor = cursor
        self.urls = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.urls.append(url)
        parsed = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        rows = self.rows[parsed.path]
        size = int(q.get("page_size", 50))
        if self.cursor:
            start = 0
            if "starting_after" in q:
                start = next(i for i, r in enumerate(rows) if r["id"] == q["starting_after"]) + 1
            chunk = rows[start:start + size]
            more = start + size < len(rows)
            return _Resp({"data": chunk, "has_more": more,
                          "next_cursor": chunk[-1]["id"] if more else None})
        page = 1 if self.ignore_page else int(q.get("page", 1))
        chunk = rows[(page - 1) * size: page * size]
        last = page * size >= len(rows)
        return _Resp({"data": chunk, "meta": {"next": None if last else page + 1,
                                               "total_count": len(rows)}})


def rows(n, prefix="p"):
    return [{"id": f"{prefix}{i}", "payout_type": "commission", "status": "paid"} for i in range(n)]


@pytest.fixture
def client():
    # A stand-in accessor: the key is given, so no secret is ever read.
    return AscendApiClient(api_key="test-key", origin="https://sandbox.api.useascend.com",
                           secret_accessor=object())


def test_fetch_payouts_reads_all_three_pages(client, monkeypatch):
    fake = FakeAscend({"/v1/payouts": rows(125)})
    monkeypatch.setattr(ascend_sync.request, "urlopen", fake)
    got = client.fetch_payouts(page_size=50)
    assert [r["id"] for r in got] == [f"p{i}" for i in range(125)]
    pages = [parse_qs(urlparse(u).query).get("page", ["1"])[0] for u in fake.urls]
    assert pages == ["1", "2", "3"]


def test_every_list_used_by_delivery_crosses_pages(client, monkeypatch):
    fake = FakeAscend({
        "/v1/cancelation_returns": rows(7, "c"),
        "/v1/programs": rows(7, "g"),
        "/v1/payouts": rows(7, "p"),
    })
    monkeypatch.setattr(ascend_sync.request, "urlopen", fake)
    assert len(client.fetch_cancelation_returns(page_size=3)) == 7
    assert len(client.fetch_programs(page_size=3)) == 7
    assert len(client.fetch_payouts(page_size=3)) == 7
    assert len(fake.urls) == 9  # 3 pages each


def test_vendor_that_ignores_page_fails_loudly_not_short(client, monkeypatch):
    # If the vendor kept returning page 1, the old code would loop or stop
    # early. Now the repeated ids raise.
    fake = FakeAscend({"/v1/payouts": rows(5)}, ignore_page=True)
    monkeypatch.setattr(ascend_sync.request, "urlopen", fake)
    with pytest.raises(ValueError, match="duplicate_record_across_pages"):
        client.fetch_payouts(page_size=2)


def test_cursor_contract_also_crosses_pages(client, monkeypatch):
    fake = FakeAscend({"/v1/payouts": rows(5)}, cursor=True)
    monkeypatch.setattr(ascend_sync.request, "urlopen", fake)
    assert len(client.fetch_payouts(page_size=2)) == 5


def _probe():
    spec = importlib.util.spec_from_file_location("probe", ROOT / "scripts" / "ascend_pagination_probe.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_probe_reports_multi_page_proven_with_small_page_size(client, monkeypatch, capsys):
    fake = FakeAscend({p: rows(5, p[4]) for p in _probe().ENDPOINTS})
    monkeypatch.setattr(ascend_sync.request, "urlopen", fake)
    code = _probe().main(["--page-size", "2"], api=client)
    out = capsys.readouterr().out
    report = json.loads(out[: out.index("PAGINATION PROOF")])
    assert code == 0
    assert all(e["multi_page_proven"] for e in report["endpoints"].values())
    assert all(e["pages"] == 3 and e["vendor_total_matches"] for e in report["endpoints"].values())
    assert "MULTI-PAGE PROVEN on:" in out
    assert "test-key" not in out


def test_probe_says_not_proven_when_everything_fits_on_one_page(client, monkeypatch, capsys):
    fake = FakeAscend({p: rows(3, p[4]) for p in _probe().ENDPOINTS})
    monkeypatch.setattr(ascend_sync.request, "urlopen", fake)
    code = _probe().main([], api=client)
    out = capsys.readouterr().out
    assert code == 0
    assert "MULTI-PAGE NOT PROVEN" in out and "--page-size 1" in out
