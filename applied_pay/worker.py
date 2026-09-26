"""Read-only Applied Pay batch pull and matcher. No QBO/EZLynx posting or email sends."""
from __future__ import annotations
import argparse
import base64
from datetime import date, datetime, timedelta
import email.utils
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from zoneinfo import ZoneInfo

from .email_parse import parse
from .match import Matcher
from .qbo_snapshot import build as qbo_build

MAILBOX = "accounting@streetsmart.insurance"
SENDER = "noreply_pay@mail.myappliedproducts.com"
SUBJECT = re.compile(r"^Batch Settlement Details for Primary Account Reconciled on (\d{1,2}/\d{1,2}/\d{4})$")
TRANSFER = re.compile(r"^\w+$")
MAX_ATTACHMENT = 5_000_000
MAX_BATCHES = 100

class PullError(RuntimeError):
    pass


def _decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _parts(payload):
    yield payload
    for child in payload.get("parts", []):
        yield from _parts(child)


def _body(payload):
    pieces = [p for p in _parts(payload) if p.get("mimeType") == "text/plain" and p.get("body", {}).get("data")]
    if len(pieces) != 1:
        raise PullError("expected exactly one plaintext body")
    return _decode(pieces[0]["body"]["data"]).decode("utf-8")


def _headers(payload):
    result = {}
    for item in payload.get("headers", []):
        result[item["name"].lower()] = item["value"]
    return result


def fetch_payouts(service, *, start: date, end: date):
    """Bounded Gmail read. Every candidate is validated; no silent partial result."""
    if end < start or (end - start).days > 31:
        raise ValueError("one pull may cover at most 31 days")
    query = f'from:{SENDER} subject:"Batch Settlement Details" after:{(start - timedelta(days=1)):%Y/%m/%d} before:{(end + timedelta(days=2)):%Y/%m/%d}'
    ids, token = [], None
    while True:
        args = {"userId":"me", "q":query, "maxResults":100}
        if token: args["pageToken"] = token
        page = service.users().messages().list(**args).execute()
        ids.extend(m["id"] for m in page.get("messages", []))
        if len(ids) > MAX_BATCHES: raise PullError("too many messages; narrow date range")
        token = page.get("nextPageToken")
        if not token: break
    payouts, evidence, refs = [], [], set()
    for mid in ids:
        msg = service.users().messages().get(userId="me", id=mid, format="full").execute()
        headers = _headers(msg.get("payload", {}))
        from_address = email.utils.parseaddr(headers.get("from", ""))[1].lower()
        subject = headers.get("subject", "")
        subject_match = SUBJECT.fullmatch(subject)
        if from_address != SENDER or not subject_match:
            raise PullError(f"candidate {mid}: unexpected sender or subject")
        month, day_num, year = map(int, subject_match[1].split("/"))
        day = date(year, month, day_num)
        if not start <= day <= end: continue
        parts = [p for p in _parts(msg["payload"]) if p.get("filename")]
        expected_name = f"Batch Settlement Details - {day.isoformat()}.xlsx"
        if len(parts) != 1 or parts[0]["filename"] != expected_name:
            raise PullError(f"candidate {mid}: missing, duplicate or wrong settlement attachment")
        body = parts[0]["body"]
        if body.get("attachmentId"):
            raw = service.users().messages().attachments().get(userId="me", messageId=mid, id=body["attachmentId"]).execute()
            data = _decode(raw["data"])
        else:
            data = _decode(body.get("data", ""))
        if not 0 < len(data) <= MAX_ATTACHMENT or data[:2] != b"PK":
            raise PullError(f"candidate {mid}: invalid attachment size/type")
        with tempfile.TemporaryDirectory(prefix="applied-xlsx-") as tmp:
            path = Path(tmp) / "source.xlsx"
            path.write_bytes(data)
            try:
                payout = parse(_body(msg["payload"]), headers["date"], str(path))
            except Exception as exc:
                raise PullError(f"candidate {mid}: settlement parse failed") from exc
        if payout["ref"] in refs or not TRANSFER.fullmatch(payout["ref"]):
            raise PullError(f"candidate {mid}: duplicate or invalid transfer reference")
        email_day = date.fromisoformat(payout["payout_date"])
        if not payout["lines_tie"] or not payout["lines"] or not day <= email_day <= day + timedelta(days=5):
            raise PullError(f"candidate {mid}: totals, lines or dates do not tie")
        refs.add(payout["ref"])
        payouts.append(payout)
        evidence.append({"message_id":mid,"attachment_id":body.get("attachmentId"),"attachment_sha256":hashlib.sha256(data).hexdigest(),"reconciled_date":day.isoformat(),"transfer_ref":payout["ref"],"line_count":len(payout["lines"]),"net":payout["net"]})
    return payouts, evidence


def run(service, *, start: date, end: date):
    payouts, evidence = fetch_payouts(service, start=start, end=end)
    # No silent empty daily success. A quiet day still needs a human/source check.
    if not payouts:
        return {"status":"needs_human", "reason":"no matching Applied settlement messages for requested period", "sources":[], "findings":[]}
    qbo = qbo_build((start - timedelta(days=30)).isoformat(), end.isoformat(), start.isoformat(), (end + timedelta(days=1)).isoformat())
    snapshot = {"cutoff":end.isoformat(), "payouts":payouts, "ledger":qbo["ledger"], "bank_deposits":qbo["bank_deposits"], "aliases":{}}
    findings = Matcher(snapshot).run()
    return {"status":"needs_human" if any(f.bucket not in ("already_posted",) for f in findings) else "review_complete",
            "source":"Applied email and XLSX; QBO live query, no independent bank-clearing feed or EZLynx note adapter",
            "sources":evidence,
            "qbo_counts":{"ledger":len(qbo["ledger"]),"deposits":len(qbo["bank_deposits"]),"skipped_jes":len(qbo["skipped_jes"])},
            "findings":[{"transfer_ref":f.payout_ref,"bucket":f.bucket,"reasons":f.reasons,"qbo_deposit_id":f.bank["id"] if f.bank and f.bank in qbo["bank_deposits"] else None,"proposal_only":bool(f.proposed_deposit)} for f in findings],
            "posts_made":0,"ezlynx_notes_written":0}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    today = datetime.now(ZoneInfo("America/New_York")).date()
    end = args.end or today
    start = args.start or end - timedelta(days=7)
    sa = os.environ.get("APPLIED_GMAIL_DELEGATED_SERVICE_ACCOUNT", "").strip()
    if not sa: parser.error("APPLIED_GMAIL_DELEGATED_SERVICE_ACCOUNT is required")
    from robie_job_engine.gmail_report_ingestion import build_readonly_delegated_service
    service = build_readonly_delegated_service(sa, MAILBOX)
    result = run(service, start=start, end=end)
    # Refuse symlinked or overly public destinations; deployment creates the directory.
    if not args.output_dir.is_dir() or args.output_dir.is_symlink() or args.output_dir.stat().st_mode & 0o077:
        parser.error("output-dir must be an existing private directory (mode 0700)")
    output = args.output_dir / f"applied-pay-{start.isoformat()}-{end.isoformat()}.json"
    fd, staged = tempfile.mkstemp(dir=args.output_dir, prefix=".applied-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd,"w") as stream:
            json.dump(result,stream,indent=2)
            stream.flush();os.fsync(stream.fileno())
        os.replace(staged,output)
    finally:
        if os.path.exists(staged): os.unlink(staged)
    buckets = {b:sum(f["bucket"] == b for f in result["findings"]) for b in {f["bucket"] for f in result["findings"]}}
    print(json.dumps({"status":result["status"],"report":str(output),"buckets":buckets}))
    # Explicit nonzero for needs-human so systemd surfaces it as an alert.
    return 2 if result["status"] == "needs_human" else 0

if __name__ == "__main__":
    raise SystemExit(main())
