"""Certificates Phase 2: gated filing of certificate notes to EZLynx discussions.

Reusable tool extracted from the 2026-09-21 Phase 2 live filing run
(``~/workspace/robie-ops/cert-phase2-filing.py``). It files caller-supplied
note bodies — it never drafts certificate language itself.

For each item:
  1. Resolve the destination discussion: an explicit ``discussion_id`` wins.
     Otherwise resolve by ``title_match`` (all substrings must appear in the
     discussion title, case-insensitive). Fail closed: zero or ambiguous
     matches mean nothing is written.
  2. Read the discussion before-state (title, noteCount, mostRecentNoteId,
     lastModified).
  3. Duplicate guard: when ``skip_if_modified_after`` is set and the
     discussion was modified at or after that instant, skip the item — a note
     may already have landed from an earlier run.
  4. Append the note by exact discussion ID. A discussion is never created
     and nothing is deleted.
  5. Read the after-state and verify: noteCount increased by exactly 1, the
     most-recent note id changed, and the title is unchanged.

Result statuses: ``filed`` (all read-back checks passed), ``filed_unverified``
(written but a check failed — investigate before retrying), ``skipped``
(duplicate guard or dry run chose not to write), ``pending`` (could not
resolve a destination — fail closed), ``error``.

Items file example (JSON list)::
    [
      {
        "key": "example-1",
        "account_id": "150750271",
        "discussion_id": "762585359",
        "note": "Client requests updated docs. GL WS558018 active thru 2026-10-04. CSR to issue."
      },
      {
        "key": "example-2",
        "account_id": "78540038",
        "title_match": ["bridgeport"],
        "note": "Client requests COI for Town of Bridgeport CT. GL 0251069 active thru 2027-02-11. CSR to issue."
      }
    ]

CLI::
    python -m robie_job_engine.certificate_filing --items items.json \\
        [--cutoff 2026-09-21T12:00:00+00:00] [--dry-run] [--results-out out.json]

Config comes from the standard ROBIE_ENV / Secret Manager path via
``load_ezlynx_api_config`` — no credentials in code or in the items file.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any

from .ezlynx_api import load_ezlynx_api_config
from .ezlynx_discussions import (
    DiscussionApiClient,
    DiscussionApiConfig,
    discussion_id_of,
    discussion_title_of,
    reject_phone_numbers,
)

DISCUSSION_BASE_URL = "https://app.ezlynx.com/DiscussionApi/"


def parse_dt(value: Any) -> datetime | None:
    """Parse an EZLynx timestamp into an aware datetime; None when unparseable."""
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def discussion_state(client: DiscussionApiClient, discussion_id: str) -> dict[str, Any]:
    """Read the fields needed for duplicate-guard and read-back verification."""
    rec = client.get_discussion(discussion_id)
    return {
        "discussion_id": str(discussion_id),
        "title": discussion_title_of(rec),
        "noteCount": rec.get("noteCount", rec.get("NoteCount")),
        "mostRecentNoteId": str(rec.get("mostRecentNoteId", rec.get("MostRecentNoteId", "")) or ""),
        "lastModified": str(rec.get("lastModified", rec.get("LastModified", "")) or ""),
    }


def resolve_discussion_id(
    client: DiscussionApiClient, item: dict[str, Any]
) -> tuple[str | None, str | None]:
    """Return (discussion_id, failure_reason). Fail closed on ambiguity."""
    explicit = str(item.get("discussion_id") or "").strip()
    if explicit:
        return explicit, None
    hints = [str(h).strip().lower() for h in (item.get("title_match") or []) if str(h).strip()]
    if not hints:
        return None, "no discussion_id and no title_match; refusing to guess"
    account_id = str(item.get("account_id") or "").strip()
    if not account_id:
        return None, "title_match given but account_id is missing"
    matches = []
    for row in client.get_discussions(account_id):
        title = discussion_title_of(row).lower()
        if all(h in title for h in hints):
            matches.append(row)
    if len(matches) != 1:
        return None, (
            "title match %r found %d discussions (need exactly 1); refusing to guess"
            % (hints, len(matches))
        )
    return discussion_id_of(matches[0]), None


def file_certificate_notes(
    client: DiscussionApiClient,
    items: list[dict[str, Any]],
    *,
    skip_if_modified_after: datetime | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """File each item's note; return one result dict per item.

    ``skip_if_modified_after``: aware datetime; items whose discussion was
    modified at/after it are skipped (duplicate guard). ``dry_run`` resolves
    destinations and reads before-state but writes nothing.
    """
    results: list[dict[str, Any]] = []
    for item in items:
        key = str(item.get("key") or "")
        account_id = str(item.get("account_id") or "")
        res: dict[str, Any] = {"key": key, "account_id": account_id}
        try:
            note = reject_phone_numbers(str(item.get("note") or "")).strip()
            if not note:
                res.update(status="pending", reason="note body is empty")
                results.append(res)
                continue

            discussion_id, failure = resolve_discussion_id(client, item)
            if failure:
                res.update(status="pending", reason=failure)
                results.append(res)
                continue
            res["discussion_id"] = discussion_id

            before = discussion_state(client, discussion_id)
            res["before"] = before

            if dry_run:
                res.update(status="skipped", reason="dry run: nothing written")
                results.append(res)
                continue

            if skip_if_modified_after is not None:
                last_modified = parse_dt(before["lastModified"])
                if last_modified is not None and last_modified >= skip_if_modified_after:
                    res.update(
                        status="skipped",
                        reason=(
                            "lastModified %s is at/after the guard %s; a note may "
                            "already have landed - skipping to avoid a duplicate"
                            % (
                                before["lastModified"],
                                skip_if_modified_after.isoformat(),
                            )
                        ),
                    )
                    results.append(res)
                    continue

            client.append_note(discussion_id, note)

            after = discussion_state(client, discussion_id)
            res["after"] = after
            res["note_id"] = after["mostRecentNoteId"]

            count_ok = (
                isinstance(before["noteCount"], int)
                and isinstance(after["noteCount"], int)
                and after["noteCount"] == before["noteCount"] + 1
            )
            recent_ok = bool(after["mostRecentNoteId"]) and (
                after["mostRecentNoteId"] != before["mostRecentNoteId"]
            )
            title_ok = bool(after["title"]) and after["title"] == before["title"]
            if count_ok and recent_ok and title_ok:
                res.update(status="filed", reason="note appended; read-back verified")
            else:
                res.update(
                    status="filed_unverified",
                    reason=(
                        "written but read-back checks failed: "
                        "count_ok=%s recent_ok=%s title_ok=%s"
                        % (count_ok, recent_ok, title_ok)
                    ),
                )
        except Exception as exc:  # never let one item kill the batch
            res.update(status="error", reason="%s: %s" % (type(exc).__name__, str(exc)[:300]))
        results.append(res)
    return results


def build_client() -> DiscussionApiClient:
    """Build an authenticated DiscussionApiClient from the standard config."""
    api_config = load_ezlynx_api_config()
    disc_config = DiscussionApiConfig(
        discussion_base_url=DISCUSSION_BASE_URL,
        token_endpoint=api_config.token_endpoint,
        client_id=api_config.client_id,
        client_secret=api_config.client_secret,
        username=api_config.username,
        integration_group_id=api_config.integration_group_id,
        scope="DiscussionApi openid",
    )
    client = DiscussionApiClient(disc_config)
    client.get_token()  # fail fast on bad credentials before touching anything
    return client


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="File certificate notes to EZLynx discussions with duplicate guard and read-back verification."
    )
    parser.add_argument("--items", required=True, help="JSON file: list of filing items")
    parser.add_argument(
        "--cutoff",
        default=None,
        help="ISO datetime; skip discussions modified at/after it (duplicate guard)",
    )
    parser.add_argument("--dry-run", action="store_true", help="resolve and read, write nothing")
    parser.add_argument("--results-out", default=None, help="write results JSON here")
    args = parser.parse_args(argv)

    with open(args.items, encoding="utf-8") as fh:
        items = json.load(fh)
    if not isinstance(items, list):
        print("--items must contain a JSON list", file=sys.stderr)
        return 2
    cutoff = parse_dt(args.cutoff) if args.cutoff else None
    if args.cutoff and cutoff is None:
        print("could not parse --cutoff %r" % args.cutoff, file=sys.stderr)
        return 2

    from datetime import datetime as _dt, timezone as _tz

    results = file_certificate_notes(
        build_client(),
        items,
        skip_if_modified_after=cutoff,
        dry_run=args.dry_run,
    )
    payload = {
        "ran_at_utc": _dt.now(_tz.utc).isoformat(),
        "dry_run": args.dry_run,
        "results": results,
    }
    text = json.dumps(payload, indent=1)
    if args.results_out:
        with open(args.results_out, "w", encoding="utf-8") as fh:
            fh.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
