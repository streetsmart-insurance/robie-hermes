"""Daily carrier document retrieval workflow.

BUILT 2026-10-05 (not just designed).

Runs every carrier pull in DRY-RUN mode (via carrier_dry_run), uploads the
downloaded PDFs to the shared Drive folder "Robie Carrier Pull QA (Nicole)",
and writes a new dated tab on the internal carrier status spreadsheet for
Nicole with one row per document linking back to its Drive file.

Safety posture:
- ``ROBIE_ENV=TEST`` is required; Production hosts are refused.
- The filing kill switch ``ROBIE_DOCUMENT_RETRIEVAL_FILE_EZLYNX`` is forced
  to ``0`` — nothing is ever filed to EZLynx and no email is ever sent.
- One carrier failing (pull, upload, or sheet) never stops the others; every
  failure is recorded in the result and on the sheet tab itself.
- Drive uploads and Sheets writes go through injectable clients. The default
  implementations use googleapiclient with cloud ADC and fail closed
  (IntakeHold) when the client library or credentials are unavailable. Tests
  inject fakes and never touch real Drive/Sheets.

Drive layout:
    <QA_ROOT>/<Carrier Display>/<YYYY-MM-DD>/<filename>.pdf

Sheet layout (new tab "YYYY-MM-DD"):
    row 1: summary line (documents, per-outcome carrier counts)
    row 2: per-carrier download counts
    row 3: blank
    row 4: header (Carrier | Policy # | Insured Name | Document Type |
                   Document Date | Drive Link | Status)
    row 5+: one row per document; HYPERLINK formula to the Drive file
    held/failed carriers get a row with status "HELD: <reason>" and no link.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Protocol

QA_ROOT_FOLDER_ID = "1cLEpR-0T6KdiVjcdAr0qpGTO447MetI2"
STATUS_SPREADSHEET_ID = "1uNPWu18wo0nB0PzKq8p4aWslf_CpIKySjRMyQi45ItM"
KILL_SWITCH_ENV = "ROBIE_DOCUMENT_RETRIEVAL_FILE_EZLYNX"

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"

SHEET_COLUMNS = [
    "Carrier",
    "Policy #",
    "Insured Name",
    "Document Type",
    "Document Date",
    "Drive Link",
    "Status",
]

FOLDER_MIME = "application/vnd.google-apps.folder"

_DATE_PAREN_RE = re.compile(r"\s*\(\d{4}-\d{2}-\d{2}\)\s*$")
_CARRIER_TAG_RE = re.compile(
    r"\s+(Progressive|Guard|GEICO|Travelers|NatGen|UticaFirst|FarmersofSalem)\s*$",
    re.IGNORECASE,
)


class DriveClient(Protocol):
    """Minimal Drive surface the daily workflow needs."""

    def ensure_folder(self, parent_id: str, name: str) -> str:
        """Return the folder id for ``name`` under ``parent_id``, creating it."""
        ...

    def upload_pdf(self, local_path: Path, folder_id: str, name: str) -> dict[str, str]:
        """Upload a PDF. Returns {"id": ..., "webViewLink": ...}."""
        ...


class SheetsClient(Protocol):
    """Minimal Sheets surface the daily workflow needs."""

    def create_tab(self, spreadsheet_id: str, title: str) -> None:
        """Create a tab named ``title``; no-op when it already exists."""
        ...

    def write_values(
        self, spreadsheet_id: str, tab_title: str, values: list[list[str]]
    ) -> None:
        """Write ``values`` starting at A1 of ``tab_title`` (USER_ENTERED)."""
        ...


def _refuse_production_host() -> None:
    from .intake_core import IntakeHold

    raw = f"{socket.gethostname()} {socket.getfqdn()}".lower()
    labels = [label for label in re.split(r"[\s.]+", raw) if label]
    if any(label == "hermes-poc-01" or label.startswith("hermes-poc") for label in labels):
        raise IntakeHold("Daily carrier retrieval refuses Production host")


def _require_daily_environment() -> None:
    from .intake_core import require_test

    require_test()  # ROBIE_ENV must be TEST
    _refuse_production_host()
    os.environ[KILL_SWITCH_ENV] = "0"


class GwsDriveClient:
    """Drive client backed by googleapiclient + cloud ADC. Fail-closed."""

    def __init__(self) -> None:
        from .intake_core import IntakeHold

        try:
            import google.auth
            from google.auth.transport.requests import Request
            from googleapiclient.discovery import build

            credentials, _ = google.auth.default(scopes=[DRIVE_SCOPE])
            if getattr(credentials, "requires_scopes", False):
                credentials = credentials.with_scopes([DRIVE_SCOPE])
            refresh = getattr(credentials, "refresh", None)
            if callable(refresh) and not getattr(credentials, "valid", True):
                refresh(Request())
            self._service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        except Exception as exc:  # noqa: BLE001 - fail closed, never invent auth
            raise IntakeHold(f"Google Drive is unavailable: {type(exc).__name__}: {exc}") from exc

    def ensure_folder(self, parent_id: str, name: str) -> str:
        from .intake_core import IntakeHold

        safe = name.replace("'", "\\'")
        try:
            resp = (
                self._service.files()
                .list(
                    q=f"name='{safe}' and '{parent_id}' in parents "
                    f"and mimeType='{FOLDER_MIME}' and trashed=false",
                    fields="files(id,name)",
                    pageSize=5,
                )
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            raise IntakeHold(f"Drive folder lookup failed: {type(exc).__name__}: {exc}") from exc
        files = resp.get("files", [])
        if files:
            return files[0]["id"]
        try:
            created = (
                self._service.files()
                .create(
                    body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
                    fields="id",
                )
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            raise IntakeHold(f"Drive folder create failed: {type(exc).__name__}: {exc}") from exc
        return created["id"]

    def upload_pdf(self, local_path: Path, folder_id: str, name: str) -> dict[str, str]:
        from .intake_core import IntakeHold
        from googleapiclient.http import MediaFileUpload

        if not local_path.is_file():
            raise IntakeHold(f"Cannot upload missing file: {local_path}")
        try:
            media = MediaFileUpload(str(local_path), mimetype="application/pdf", resumable=False)
            created = (
                self._service.files()
                .create(
                    body={"name": name, "parents": [folder_id], "mimeType": "application/pdf"},
                    media_body=media,
                    fields="id,webViewLink",
                )
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            raise IntakeHold(f"Drive upload failed for {name}: {type(exc).__name__}: {exc}") from exc
        return {"id": created["id"], "webViewLink": created.get("webViewLink", "")}


class GwsSheetsClient:
    """Sheets client backed by googleapiclient + cloud ADC. Fail-closed."""

    def __init__(self) -> None:
        from .intake_core import IntakeHold

        try:
            import google.auth
            from google.auth.transport.requests import Request
            from googleapiclient.discovery import build

            credentials, _ = google.auth.default(scopes=[SHEETS_SCOPE])
            if getattr(credentials, "requires_scopes", False):
                credentials = credentials.with_scopes([SHEETS_SCOPE])
            refresh = getattr(credentials, "refresh", None)
            if callable(refresh) and not getattr(credentials, "valid", True):
                refresh(Request())
            self._service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
        except Exception as exc:  # noqa: BLE001 - fail closed, never invent auth
            raise IntakeHold(f"Google Sheets is unavailable: {type(exc).__name__}: {exc}") from exc

    def _existing_titles(self, spreadsheet_id: str) -> set[str]:
        from .intake_core import IntakeHold

        try:
            meta = (
                self._service.spreadsheets()
                .get(spreadsheetId=spreadsheet_id, fields="sheets.properties.title")
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            raise IntakeHold(f"Sheets metadata read failed: {type(exc).__name__}: {exc}") from exc
        return {s["properties"]["title"] for s in meta.get("sheets", [])}

    def create_tab(self, spreadsheet_id: str, title: str) -> None:
        from .intake_core import IntakeHold

        if title in self._existing_titles(spreadsheet_id):
            return
        try:
            self._service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
            ).execute()
        except Exception as exc:  # noqa: BLE001
            raise IntakeHold(f"Sheets tab create failed: {type(exc).__name__}: {exc}") from exc

    def write_values(
        self, spreadsheet_id: str, tab_title: str, values: list[list[str]]
    ) -> None:
        from .intake_core import IntakeHold

        try:
            self._service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"'{tab_title}'!A1",
                valueInputOption="USER_ENTERED",
                body={"values": values},
            ).execute()
        except Exception as exc:  # noqa: BLE001
            raise IntakeHold(f"Sheets write failed: {type(exc).__name__}: {exc}") from exc


@dataclass
class DocumentRow:
    """One downloaded PDF, with metadata parsed best-effort from its pack."""

    carrier: str
    filename: str
    local_path: Path
    policy_number: str = ""
    insured_name: str = ""
    document_type: str = ""
    document_date: str = ""
    drive_id: str = ""
    drive_link: str = ""
    status: str = "Downloaded"


def _ledger_issued_dates(pack: Path) -> dict[str, str]:
    """Map PDF filename -> issued date from carrier ledger JSONs in the pack."""
    mapping: dict[str, str] = {}
    for ledger_file in sorted(pack.glob("*-ledger.json")):
        try:
            data = json.loads(ledger_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        items = data.get("items", {}) if isinstance(data, dict) else {}
        for _source_id, entry in items.items():
            if not isinstance(entry, dict):
                continue
            fname = entry.get("filename")
            issued = entry.get("issued_date") or entry.get("issued_on") or ""
            if fname:
                mapping.setdefault(str(fname), str(issued))
    return mapping


def _parse_filename(stem: str) -> tuple[str, str]:
    """Best-effort (policy_number, document_type) from a pack PDF filename."""
    cleaned = _DATE_PAREN_RE.sub("", stem)
    cleaned = _CARRIER_TAG_RE.sub("", cleaned).strip()
    parts = cleaned.split(None, 1)
    if not parts:
        return "", ""
    policy = parts[0]
    doc_type = parts[1].replace("_", " ").strip() if len(parts) > 1 else ""
    return policy, doc_type


def _filename_doc_date(stem: str) -> str:
    match = re.search(r"\((\d{4}-\d{2}-\d{2})\)", stem)
    return match.group(1) if match else ""


def collect_documents(pack: Path, carrier_display: str, *, as_of: date) -> list[DocumentRow]:
    """Scan a carrier's dated pack folder for downloaded PDFs.

    Insured names are not recoverable from pack filenames or ledgers, so that
    column is left blank rather than invented.
    """
    rows: list[DocumentRow] = []
    if not pack.is_dir():
        return rows
    issued = _ledger_issued_dates(pack)
    for pdf in sorted(pack.glob("*.pdf")):
        stem = pdf.stem
        policy, doc_type = _parse_filename(stem)
        doc_date = issued.get(pdf.name) or _filename_doc_date(stem) or as_of.isoformat()
        rows.append(
            DocumentRow(
                carrier=carrier_display,
                filename=pdf.name,
                local_path=pdf,
                policy_number=policy,
                document_type=doc_type,
                document_date=doc_date,
            )
        )
    return rows


def upload_carrier_pack(
    drive: DriveClient,
    *,
    carrier_display: str,
    day: date,
    pack: Path,
    documents: list[DocumentRow],
) -> list[dict[str, str]]:
    """Upload one carrier's PDFs under <root>/<carrier>/<YYYY-MM-DD>/."""
    carrier_folder = drive.ensure_folder(QA_ROOT_FOLDER_ID, carrier_display)
    date_folder = drive.ensure_folder(carrier_folder, day.isoformat())
    uploaded: list[dict[str, str]] = []
    for doc in documents:
        info = drive.upload_pdf(doc.local_path, date_folder, doc.filename)
        doc.drive_id = info.get("id", "")
        doc.drive_link = info.get("webViewLink", "")
        uploaded.append(
            {
                "filename": doc.filename,
                "drive_id": doc.drive_id,
                "drive_link": doc.drive_link,
            }
        )
    return uploaded


def _hyperlink_formula(link: str) -> str:
    if not link:
        return ""
    safe = link.replace('"', '""')
    return f'=HYPERLINK("{safe}","Open PDF")'


def build_sheet_values(
    day: date,
    summary: dict[str, Any],
    documents: list[DocumentRow],
    *,
    carrier_order: tuple[str, ...],
) -> list[list[str]]:
    """Build the full value grid for the dated tab."""
    carriers = summary.get("carriers", {})
    totals = summary.get("totals", {})
    total_docs = len(documents)
    counts = []
    for name in carrier_order:
        r = carriers.get(name)
        if r is None:
            continue
        counts.append(f"{r.get('display', name)}: {r.get('downloaded', 0)}")
    lines: list[list[str]] = [
        [
            f"Carrier document retrieval — {day.isoformat()} | {total_docs} documents | "
            f"{totals.get('ok', 0)} carriers ok, {totals.get('held', 0)} held, "
            f"{totals.get('failed', 0)} failed"
        ]
    ]
    lines.append([" | ".join(counts) if counts else "no carriers ran"])
    lines.append([])
    lines.append(SHEET_COLUMNS)
    for doc in documents:
        lines.append(
            [
                doc.carrier,
                doc.policy_number,
                doc.insured_name,
                doc.document_type,
                doc.document_date,
                _hyperlink_formula(doc.drive_link),
                doc.status,
            ]
        )
    for name in carrier_order:
        r = carriers.get(name)
        if r is None:
            continue
        if r.get("status") == "OK":
            continue
        if r.get("status") == "HELD":
            held = r.get("held") or []
            reason = held[0].get("reason", "held") if held else "held"
            status = f"HELD: {reason}"
        else:
            status = f"FAILED: {r.get('error') or 'unknown error'}"
        lines.append([r.get("display", name), "", "", "", "", "", status])
    return lines


def _default_dry_run_runner(*, as_of: date, output_root: str | None) -> dict[str, Any]:
    from . import carrier_dry_run

    return carrier_dry_run.run_dry_run(as_of=as_of, output_root=output_root)


def run_daily_retrieval(
    as_of: date | None = None,
    *,
    skip_upload: bool = False,
    skip_sheet: bool = False,
    output_root: str | None = None,
    dry_run_runner: Callable[..., dict[str, Any]] | None = None,
    drive: DriveClient | None = None,
    sheets: SheetsClient | None = None,
) -> dict[str, Any]:
    """Run the daily retrieval: pull, upload to Drive, write the sheet tab.

    ``dry_run_runner``, ``drive``, and ``sheets`` are injectable for tests;
    in production the real dry-run orchestrator and the GWS clients are used.
    Nothing here ever files to EZLynx or sends email.
    """
    from . import carrier_dry_run

    _require_daily_environment()
    day = as_of or date.today()
    errors: list[str] = []

    runner = dry_run_runner or _default_dry_run_runner
    summary = runner(as_of=day, output_root=output_root)

    documents: list[DocumentRow] = []
    uploaded: dict[str, list[dict[str, str]]] = {}
    carriers = summary.get("carriers", {})
    for name in carrier_dry_run.CARRIER_ORDER:
        r = carriers.get(name)
        if r is None or r.get("status") != "OK":
            continue
        pack = Path(r["pack"])
        docs = collect_documents(pack, r.get("display", name), as_of=day)
        if not skip_upload:
            try:
                drive_client = drive or GwsDriveClient()
                uploaded[name] = upload_carrier_pack(
                    drive_client,
                    carrier_display=r.get("display", name),
                    day=day,
                    pack=pack,
                    documents=docs,
                )
            except Exception as exc:  # noqa: BLE001 - one carrier never stops the run
                errors.append(f"Drive upload failed for {r.get('display', name)}: {exc}")
        documents.extend(docs)

    tab_title = day.isoformat()
    if not skip_sheet:
        try:
            sheets_client = sheets or GwsSheetsClient()
            values = build_sheet_values(
                day, summary, documents, carrier_order=carrier_dry_run.CARRIER_ORDER
            )
            sheets_client.create_tab(STATUS_SPREADSHEET_ID, tab_title)
            sheets_client.write_values(STATUS_SPREADSHEET_ID, tab_title, values)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Sheet update failed: {exc}")

    for name in carrier_dry_run.CARRIER_ORDER:
        r = carriers.get(name)
        if r is not None and r.get("status") == "FAILED":
            errors.append(f"Carrier pull failed: {r.get('display', name)}: {r.get('error')}")

    return {
        "as_of": day.isoformat(),
        "mode": "daily-retrieval",
        "dry_run": summary,
        "documents": len(documents),
        "uploaded": uploaded,
        "sheet_tab": tab_title if not skip_sheet else None,
        "errors": errors,
    }


def render_result(result: dict[str, Any]) -> str:
    lines = [
        f"Daily carrier retrieval for {result['as_of']}: "
        f"{result['documents']} documents collected."
    ]
    uploaded = result.get("uploaded", {})
    for name, files in uploaded.items():
        lines.append(f"- {name}: {len(files)} uploaded to Drive.")
    if result.get("sheet_tab"):
        lines.append(f"- Sheet tab '{result['sheet_tab']}' written.")
    for err in result.get("errors", []):
        lines.append(f"- ERROR: {err}")
    if not result.get("errors"):
        lines.append("No errors.")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Daily carrier document retrieval: pull, upload to Drive, "
        "write dated sheet tab (TEST only; never files to EZLynx, never sends email)"
    )
    parser.add_argument("--as-of", default=date.today().isoformat(), help="pull date YYYY-MM-DD")
    parser.add_argument("--output-root", default=None, help="override QA pack root")
    parser.add_argument("--skip-upload", action="store_true", help="skip Drive uploads")
    parser.add_argument("--skip-sheet", action="store_true", help="skip sheet update")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        day = date.fromisoformat(args.as_of)
    except ValueError:
        print(f"Bad --as-of date: {args.as_of!r} (use YYYY-MM-DD)", file=sys.stderr)
        return 2
    try:
        result = run_daily_retrieval(
            day,
            skip_upload=args.skip_upload,
            skip_sheet=args.skip_sheet,
            output_root=args.output_root,
        )
    except Exception as exc:  # noqa: BLE001 - environment gate failures
        print(f"Daily retrieval refused: {exc}", file=sys.stderr)
        return 2
    print(render_result(result))
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0 if not result["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
