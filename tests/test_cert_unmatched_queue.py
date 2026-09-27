"""Unit tests for robie_job_engine.cert_unmatched_queue + cert_unmatched_cli.

Covers: queue append/dedupe, report rendering (oldest first, evidence
visible), resolve with alias write-back, the vendor-sender guard (RMIS,
Highway, TrustLayer, myCOI, Certificial, OperFi, Assurant, internal),
--not-our-client, --no-alias, and fail-closed input validation.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from robie_job_engine.cert_unmatched_queue import (  # noqa: E402
    QueueError,
    enqueue_unmatched,
    load_entries,
    open_entries,
    render_json,
    render_markdown,
    resolve_entry,
    sender_is_vendor,
)


@pytest.fixture()
def queue_path(tmp_path):
    return str(tmp_path / "queue.jsonl")


@pytest.fixture()
def alias_path(tmp_path):
    return str(tmp_path / "aliases.json")


def _enqueue(queue_path, sender="mela@seciinc.com", gmail_id="g1",
             insured="Seci Construction Inc", **kw):
    args = dict(sender_email=sender, subject="Need COI for Town of Berlin",
                insured_name=insured,
                policy_numbers=["008985366C"],
                hold_reason="no applicant in the full-book report and no "
                            "policy anchor in EZLynx — holding for human",
                strategies_tried=["report_email", "report_name",
                                  "ezlynx_policy"],
                gmail_id=gmail_id,
                evidence=["sender domain matches client domain seciinc.com"],
                queue_path=queue_path)
    args.update(kw)
    return enqueue_unmatched(**args)


# ---------------------------------------------------------------------------
# Queue append
# ---------------------------------------------------------------------------

def test_enqueue_appends_well_formed_entry(queue_path):
    e = _enqueue(queue_path)
    assert e["entry_id"] == "uq-000001"
    assert e["status"] == "open"
    assert e["sender_email"] == "mela@seciinc.com"
    assert e["insured_name"] == "Seci Construction Inc"
    assert e["policy_numbers"] == ["008985366C"]
    assert "holding for human" in e["hold_reason"]
    assert e["strategies_tried"] == ["report_email", "report_name",
                                    "ezlynx_policy"]
    assert e["gmail_id"] == "g1"
    assert e["resolved_at"] is None

    on_disk = load_entries(queue_path)
    assert len(on_disk) == 1
    assert on_disk[0]["entry_id"] == "uq-000001"


def test_entry_ids_increment(queue_path):
    _enqueue(queue_path, gmail_id="g1")
    e2 = _enqueue(queue_path, gmail_id="g2")
    assert e2["entry_id"] == "uq-000002"


def test_enqueue_dedupes_open_entry_on_gmail_id(queue_path):
    e1 = _enqueue(queue_path, gmail_id="dup")
    e2 = _enqueue(queue_path, gmail_id="dup")
    assert e2["entry_id"] == e1["entry_id"]
    assert len(load_entries(queue_path)) == 1


def test_enqueue_after_resolve_creates_new_entry(queue_path):
    _enqueue(queue_path, gmail_id="dup")
    resolve_entry("uq-000001", not_our_client=True, resolved_by="Carlo",
                  queue_path=queue_path)
    e2 = _enqueue(queue_path, gmail_id="dup")
    assert e2["entry_id"] == "uq-000002"


def test_enqueue_requires_sender_and_reason(queue_path):
    with pytest.raises(ValueError):
        enqueue_unmatched(sender_email="", subject="x",
                          hold_reason="r", queue_path=queue_path)
    with pytest.raises(ValueError):
        enqueue_unmatched(sender_email="a@b.com", subject="x",
                          hold_reason="", queue_path=queue_path)


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

def test_report_oldest_first_with_evidence(queue_path):
    _enqueue(queue_path, gmail_id="g1", sender="first@example.com",
             insured="First Co")
    _enqueue(queue_path, gmail_id="g2", sender="second@example.com",
             insured="Second Co")
    md = render_markdown(open_entries(queue_path))
    assert md.index("uq-000001") < md.index("uq-000002")
    assert "first@example.com" in md
    assert "no applicant in the full-book report" in md
    assert "report_email" in md
    assert "seciinc.com" in md  # evidence column


def test_report_empty_queue(queue_path):
    md = render_markdown(open_entries(queue_path))
    assert "Queue is clear" in md


def test_render_json_machine_readable(queue_path):
    _enqueue(queue_path, gmail_id="g1")
    data = render_json(open_entries(queue_path))
    assert data["open_count"] == 1
    assert data["entries"][0]["entry_id"] == "uq-000001"
    assert "generated_at" in data


def test_resolved_entries_excluded_from_report(queue_path):
    _enqueue(queue_path, gmail_id="g1")
    resolve_entry("uq-000001", not_our_client=True, resolved_by="Carlo",
                  queue_path=queue_path)
    assert open_entries(queue_path) == []


# ---------------------------------------------------------------------------
# Vendor-sender guard
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sender", [
    "rmis@registrymonitoring.com",
    "rmishelp@truckstop.com",
    "insurance@certs.highway.com",
    "insurance1234567@certs.highway.com",
    "no-reply@highway.com",
    "requests@trustlayer.io",
    "support@mycoitracking.com",
    "sarah@certificial.ai",
    "insurance@operfi.com",
    "channelpartners@assurant.com",
    "noreply@vc.realpage.com",
    "myinsuranceinfo-confirmation.do-not-reply@myinsuranceinfo.com",
    "no-reply@plus1solutions.net",
    "eimy@streetsmart.insurance",  # agency internal
])
def test_vendor_senders_detected(sender):
    assert sender_is_vendor(sender)


@pytest.mark.parametrize("sender", [
    "mela@seciinc.com",
    "bobbyosaffordable@gmail.com",
    "closco@carlolosco.com",
])
def test_client_senders_not_vendors(sender):
    assert not sender_is_vendor(sender)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def _aliases(alias_path):
    with open(alias_path, encoding="utf-8") as fh:
        return json.load(fh)["aliases"]


def test_resolve_writes_strong_alias(queue_path, alias_path):
    _enqueue(queue_path)
    e = resolve_entry("uq-000001", applicant_id=78540038,
                      account_name="Seci Construction Inc",
                      resolved_by="Carlo",
                      queue_path=queue_path, alias_store_path=alias_path)
    assert e["status"] == "resolved"
    assert e["resolution"] == {"applicant_id": 78540038,
                              "account_name": "Seci Construction Inc"}
    assert e["alias_written"] is True
    assert e["resolved_by"] == "Carlo"
    assert e["resolved_at"]

    aliases = _aliases(alias_path)
    assert len(aliases) == 1
    assert aliases[0]["sender"] == "mela@seciinc.com"
    assert aliases[0]["applicant_id"] == 78540038
    assert aliases[0]["confidence"] == "strong"
    assert "uq-000001" in aliases[0]["evidence"]


def test_resolve_upserts_existing_alias(queue_path, alias_path):
    _enqueue(queue_path, gmail_id="g1")
    _enqueue(queue_path, gmail_id="g2")
    resolve_entry("uq-000001", applicant_id=111, resolved_by="Carlo",
                  queue_path=queue_path, alias_store_path=alias_path)
    resolve_entry("uq-000002", applicant_id=222, resolved_by="Carlo",
                  queue_path=queue_path, alias_store_path=alias_path)
    aliases = _aliases(alias_path)
    assert len(aliases) == 1  # same sender: updated, not duplicated
    assert aliases[0]["applicant_id"] == 222


@pytest.mark.parametrize("sender", [
    "rmis@registrymonitoring.com",
    "insurance@certs.highway.com",
    "requests@trustlayer.io",
    "sarah@certificial.ai",
    "insurance@operfi.com",
    "support@mycoitracking.com",
    "eimy@streetsmart.insurance",
])
def test_resolve_vendor_sender_writes_no_alias(queue_path, alias_path,
                                               sender):
    _enqueue(queue_path, sender=sender, gmail_id="g9")
    e = resolve_entry("uq-000001", applicant_id=147197937,
                      account_name="MM Heavy Hauls LLC",
                      resolved_by="Carlo",
                      queue_path=queue_path, alias_store_path=alias_path)
    assert e["status"] == "resolved"
    assert e["resolution"]["applicant_id"] == 147197937
    assert e["alias_written"] is False
    assert any("no fixed alias" in n for n in e["notes"])
    # store untouched (file may not even exist)
    assert not os.path.exists(alias_path)


def test_resolve_not_our_client_writes_no_alias(queue_path, alias_path):
    _enqueue(queue_path)
    e = resolve_entry("uq-000001", not_our_client=True, resolved_by="Carlo",
                      queue_path=queue_path, alias_store_path=alias_path)
    assert e["status"] == "resolved"
    assert e["resolution"] == {"not_our_client": True}
    assert e["alias_written"] is False
    assert not os.path.exists(alias_path)


def test_resolve_no_alias_override(queue_path, alias_path):
    # Broker/holder/lender sender: human judgment, no alias.
    _enqueue(queue_path, sender="mattie@berkowatts.com", gmail_id="g7")
    e = resolve_entry("uq-000001", applicant_id=999, no_alias=True,
                      resolved_by="Carlo",
                      queue_path=queue_path, alias_store_path=alias_path)
    assert e["alias_written"] is False
    assert not os.path.exists(alias_path)


def test_resolve_rejects_bad_applicant_id(queue_path, alias_path):
    _enqueue(queue_path)
    for bad in (0, -5, "abc", "12x"):
        with pytest.raises(QueueError):
            resolve_entry("uq-000001", applicant_id=bad,
                          resolved_by="Carlo",
                          queue_path=queue_path, alias_store_path=alias_path)
    # entry still open, nothing written
    assert open_entries(queue_path)[0]["entry_id"] == "uq-000001"
    assert not os.path.exists(alias_path)


def test_resolve_rejects_ambiguous_or_missing(queue_path, alias_path):
    _enqueue(queue_path)
    with pytest.raises(QueueError):
        resolve_entry("uq-000001", applicant_id=1, not_our_client=True,
                      resolved_by="Carlo", queue_path=queue_path,
                      alias_store_path=alias_path)
    with pytest.raises(QueueError):
        resolve_entry("uq-000001", resolved_by="Carlo",
                      queue_path=queue_path, alias_store_path=alias_path)
    with pytest.raises(QueueError):
        resolve_entry("uq-000001", applicant_id=1,
                      queue_path=queue_path, alias_store_path=alias_path)
    with pytest.raises(QueueError):
        resolve_entry("uq-999999", applicant_id=1, resolved_by="Carlo",
                      queue_path=queue_path, alias_store_path=alias_path)


def test_resolve_twice_rejected(queue_path, alias_path):
    _enqueue(queue_path)
    resolve_entry("uq-000001", not_our_client=True, resolved_by="Carlo",
                  queue_path=queue_path)
    with pytest.raises(QueueError):
        resolve_entry("uq-000001", not_our_client=True,
                      resolved_by="Carlo", queue_path=queue_path)


def test_alias_store_created_with_schema(queue_path, alias_path):
    _enqueue(queue_path)
    resolve_entry("uq-000001", applicant_id=1, resolved_by="Carlo",
                  queue_path=queue_path, alias_store_path=alias_path)
    with open(alias_path, encoding="utf-8") as fh:
        data = json.load(fh)
    assert set(("generated", "scope", "rule", "aliases")) <= set(data)
    assert "NEVER aliased" in data["rule"]


def test_alias_store_schema_mismatch_refuses_write(queue_path, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"aliases": "not-a-list"}', encoding="utf-8")
    _enqueue(queue_path)
    with pytest.raises(ValueError):
        resolve_entry("uq-000001", applicant_id=1, resolved_by="Carlo",
                      queue_path=queue_path, alias_store_path=str(bad))
    assert open_entries(queue_path)[0]["status"] == "open"


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------

def test_cli_report_and_resolve(tmp_path, capsys):
    from robie_job_engine.cert_unmatched_cli import main

    qp = str(tmp_path / "q.jsonl")
    ap = str(tmp_path / "a.json")
    _enqueue(qp, gmail_id="g1")
    assert main(["report", "--queue", qp]) == 0
    out = capsys.readouterr().out
    assert "uq-000001" in out

    assert main(["resolve", "uq-000001", "--applicant-id", "78540038",
                 "--by", "Carlo", "--queue", qp,
                 "--alias-store", ap]) == 0
    out = capsys.readouterr().out
    assert "alias_written: True" in out

    assert main(["report", "--queue", qp, "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["open_count"] == 0

    # CLI surfaces QueueError as exit 2, not a traceback
    assert main(["resolve", "uq-000001", "--applicant-id", "1",
                 "--by", "Carlo", "--queue", qp,
                 "--alias-store", ap]) == 2
