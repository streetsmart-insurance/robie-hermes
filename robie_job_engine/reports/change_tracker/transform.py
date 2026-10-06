"""Parse, validate, and fold the 4659 CSV export into the weekly tab matrix.

No browser, no network: pure data transforms. All Sheet-specific quirks live
here so the same code path serves --dry-run, the smoke test, and the live run.

Entry points:
- ``schema_fingerprint(columns)`` / ``assert_schema_fingerprint(csv_text)``
- ``build_week_matrix(csv_text, *, total_requests, total_open, config)``
  -> (matrix, stats)
- ``week_tab_name(for_date, config)``
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .config import ChangeTrackerConfig, DEFAULT_CONFIG, DEST_HEADERS, RAW_TO_DEST


class ChangeTrackerError(RuntimeError):
    """Base fail-closed error for the change-tracker pipeline."""


class SchemaMismatchError(ChangeTrackerError):
    """4659 export columns renamed/reordered/added/dropped (P11)."""


class CountMismatchError(ChangeTrackerError):
    """Total Open Change Requests tile != downloaded row count (P10)."""


class UnmappedProducerError(ChangeTrackerError):
    """A producer is not on any fixed team (spec: stop and ask, don't guess)."""


class DuplicateRequestError(ChangeTrackerError):
    """A Policy Change Request ID appears twice with conflicting content (P3)."""


class RefuseOverwriteError(ChangeTrackerError):
    """The week tab already holds data and --force was not given (X3)."""


class MissingSpreadsheetError(ChangeTrackerError):
    """Canonical spreadsheet id is unset (X1 pending)."""


def schema_fingerprint(columns: list[str]) -> str:
    """Stable fingerprint of the export's ordered column names (P11)."""
    body = "\n".join(str(c).strip() for c in columns)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def expected_fingerprint(config: ChangeTrackerConfig = DEFAULT_CONFIG) -> str:
    return schema_fingerprint(list(config.expected_columns))


def assert_schema_fingerprint(
    columns: list[str], config: ChangeTrackerConfig = DEFAULT_CONFIG
) -> None:
    """Fail closed unless the export schema matches the pinned fingerprint exactly.

    Catches silent EZLynx renames/reorders/adds/drops (P11) instead of mapping
    the wrong columns into the sheet.
    """
    columns = [str(c).strip() for c in columns]
    expected = list(config.expected_columns)
    if columns != expected:
        missing = [c for c in expected if c not in columns]
        extra = [c for c in columns if c not in expected]
        raise SchemaMismatchError(
            "4659 export schema changed: fail closed.\n"
            f"  fingerprint expected={expected_fingerprint(config)[:16]}... "
            f"actual={schema_fingerprint(columns)[:16]}...\n"
            f"  missing columns={missing}\n"
            f"  unexpected columns={extra}\n"
            f"  order expected={expected}\n"
            f"  order actual={columns}"
        )


def _col_index(letter: str) -> int:
    return ord(letter.upper()) - ord("A")


@dataclass
class WeekStats:
    rows_raw: int
    rows_after_dedupe: int
    rows_open: int
    duplicates_dropped: int
    dropped_not_open: int
    teams: dict[str, int]
    unmapped_producers: list[str]
    stale_rows: int


def _first_name(full: str) -> str:
    return (full or "").strip().split()[0] if (full or "").strip() else ""


def _team_of(producer: str, config: ChangeTrackerConfig) -> tuple[str, int]:
    name = (producer or "").strip()
    for rank, team in enumerate(config.team_order, start=1):
        if name in config.team_roster.get(team, ()):
            return team, rank
    raise UnmappedProducerError(
        f"producer {name!r} is not on any fixed team "
        f"(known: { {t: list(m) for t, m in config.team_roster.items()} }); "
        "refusing to guess — confirm the team with the process owner (P6)"
    )


def _parse_days_open(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        raise ChangeTrackerError("Days Open is blank on a data row; refusing to guess")
    try:
        return int(float(text))
    except ValueError:
        raise ChangeTrackerError(f"Days Open is not numeric: {value!r}")


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def parse_export_rows(
    csv_text: str, config: ChangeTrackerConfig = DEFAULT_CONFIG
) -> list[dict[str, str]]:
    """Parse the 4659 CSV into raw dicts keyed by raw column name.

    Validates the schema fingerprint first (P11). Policy Number and Policy
    Change Request ID are kept as STRINGS — never coerced to float — to avoid
    the scientific-notation precision bug (12+ digit numbers).
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    columns = [str(c or "").strip() for c in (reader.fieldnames or [])]
    assert_schema_fingerprint(columns, config)
    rows: list[dict[str, str]] = []
    for line_no, raw in enumerate(reader, start=2):
        if raw is None:
            continue
        row = {k: (v.strip() if isinstance(v, str) else v) for k, v in raw.items()}
        if not any(str(v or "").strip() for v in row.values()):
            continue  # skip fully blank lines
        request_id = _norm(row.get("Policy Change Request ID"))
        if not request_id:
            raise ChangeTrackerError(
                f"row {line_no}: missing Policy Change Request ID; "
                "refusing to build an unkeyed row"
            )
        rows.append(row)
    return rows


def dedupe_rows(rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], int]:
    """Dedupe on Policy Change Request ID (P3: one row per change request).

    Identical duplicates are dropped and counted; conflicting duplicates fail
    closed rather than guessing which one is right.
    """
    seen: dict[str, dict[str, str]] = {}
    dropped = 0
    for row in rows:
        key = _norm(row["Policy Change Request ID"])
        if key not in seen:
            seen[key] = row
            continue
        if seen[key] == row:
            dropped += 1
            continue
        raise DuplicateRequestError(
            f"Policy Change Request ID {key!r} appears twice with different "
            "content; refusing to guess which is authoritative (P3)"
        )
    return list(seen.values()), dropped


def build_week_matrix(
    csv_text: str,
    *,
    total_requests: int | str,
    total_open: int | str,
    config: ChangeTrackerConfig = DEFAULT_CONFIG,
) -> tuple[list[list[str]], WeekStats]:
    """Fold the 4659 export into the weekly tab matrix (header + data rows).

    ``total_requests`` / ``total_open`` are the two summary-tile values read
    from the 4659 page; the downloaded row count (after dedupe, BEFORE the
    P1 not-open filter — the tile counts all rows) MUST equal
    ``total_open`` (P10) or the whole build fails closed.

    Returns (matrix, stats). Matrix row 0 is DEST_HEADERS; each team group is
    preceded by its header row ("Personal - Jazmin, Daniela" in column B);
    rows within a team sort by Days Open descending.
    """
    try:
        total_open_n = int(str(total_open).strip().replace(",", ""))
    except ValueError:
        raise ChangeTrackerError(f"Total Open Change Requests tile is not numeric: {total_open!r}")
    try:
        total_requests_n = int(str(total_requests).strip().replace(",", ""))
    except ValueError:
        raise ChangeTrackerError(f"Total Policy Change Requests tile is not numeric: {total_requests!r}")

    rows = parse_export_rows(csv_text, config)
    rows, dupes = dedupe_rows(rows)

    # P10: count cross-check against the tile BEFORE any sheet write and
    # BEFORE the P1 not-open filter — the tile counts ALL rows in the
    # export, so "Done" rows still count toward it.
    if len(rows) != total_open_n:
        raise CountMismatchError(
            f"fail closed: Total Open Change Requests tile={total_open_n} but "
            f"the download holds {len(rows)} rows "
            f"({len(rows) + dupes} before dedupe). Ship nothing partial (X3); "
            "retry the download or flag to the process owner (P10)"
        )

    # P1 (LOCKED): open statuses pass through; "Done" rows are filtered out
    # and counted in dropped_not_open; ANY other status value fails closed
    # (never guessed).
    dropped_not_open = 0
    if config.open_request_statuses is not None:
        open_set = {s.casefold() for s in config.open_request_statuses}
        closed = config.closed_request_status.casefold()
        kept: list[dict[str, str]] = []
        for row in rows:
            status = _norm(row.get("Request Status"))
            if status.casefold() in open_set:
                kept.append(row)
            elif status.casefold() == closed:
                dropped_not_open += 1
            else:
                raise ChangeTrackerError(
                    f"unknown Request Status {status!r} (expected one of "
                    f"{sorted(config.open_request_statuses)} or {config.closed_request_status!r}); "
                    "refusing to guess — fail closed and confirm with the "
                    "process owner (P1)"
                )
        rows = kept

    # Assign teams; any unmapped producer fails the whole build (spec: stop,
    # don't guess). Collect ALL unmapped names so one run reports them all.
    grouped: dict[str, list[tuple[dict[str, str], int]]] = {}
    unmapped: list[str] = []
    for row in rows:
        producer = _norm(row.get("Assigned Producer"))
        try:
            team, rank = _team_of(producer, config)
        except UnmappedProducerError:
            if producer not in unmapped:
                unmapped.append(producer)
            continue
        grouped.setdefault(team, []).append((row, _parse_days_open(row.get("Days Open"))))
    if unmapped:
        raise UnmappedProducerError(
            f"producers not on any fixed team: {unmapped}; refusing to guess "
            "— confirm the team with the process owner (P6)"
        )

    stats_teams: dict[str, int] = {}
    stale_rows = 0
    matrix: list[list[str]] = [list(DEST_HEADERS)]
    dest_index = {letter: idx for letter, idx in
                  ((letter, _col_index(letter)) for letter in "ABCDEFGHIJKLMNOPQ")}

    for team in config.team_order:
        team_rows = grouped.get(team, [])
        stats_teams[team] = len(team_rows)
        if not team_rows:
            continue
        # Sort by Days Open descending within the team.
        team_rows.sort(key=lambda pair: pair[1], reverse=True)
        # Team header row: "Personal - Jazmin, Daniela" in column B.
        first_names = [_first_name(m) for m in config.team_roster[team]]
        header = [""] * len(DEST_HEADERS)
        header[dest_index["B"]] = f"{team} - {', '.join(first_names)}"
        matrix.append(header)
        threshold = config.stale_threshold_for(team)
        for row, days_open in team_rows:
            out = [""] * len(DEST_HEADERS)
            for raw_col, letter in RAW_TO_DEST.items():
                out[dest_index[letter]] = _norm(row.get(raw_col))
            # P/Q: summary-tile values repeated per row (the only source).
            out[dest_index["P"]] = str(total_requests_n)
            out[dest_index["Q"]] = str(total_open_n)
            matrix.append(out)
            if days_open > threshold:
                stale_rows += 1

    stats = WeekStats(
        rows_raw=len(rows) + dupes + dropped_not_open,
        rows_after_dedupe=len(rows) + dropped_not_open,
        rows_open=len(rows),
        duplicates_dropped=dupes,
        dropped_not_open=dropped_not_open,
        teams=stats_teams,
        unmapped_producers=[],
        stale_rows=stale_rows,
    )
    return matrix, stats


def week_tab_name(
    for_date: date | None = None, config: ChangeTrackerConfig = DEFAULT_CONFIG
) -> str:
    """Weekly tab name per TAB_WEEK_RULE (P12 LOCKED): Monday–Sunday of the
    week just closed, regardless of run day or holidays. week_ending = the
    most recent Sunday on or before the run date; the tab spans
    week_ending − 6d through week_ending, formatted like "9/28-10/4".
    """
    tz = ZoneInfo(config.timezone)
    today = for_date or datetime.now(tz).date()
    # Most recent Sunday on or before today. A Sunday run now labels the
    # week ending TODAY (the week just closed).
    days_since_sunday = (today.weekday() + 1) % 7
    week_ending = today - timedelta(days=days_since_sunday)
    week_start = week_ending - timedelta(days=6)
    return f"{week_start.month}/{week_start.day}-{week_ending.month}/{week_ending.day}"
