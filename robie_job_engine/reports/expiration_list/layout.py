"""Sheet layout for the Weekly Expiration List report.

Docx Step 5/6 layout, reproduced via the Sheets API instead of the
clipboard-paste browser workflow:

  Columns: A (producer) | B Account Name | C Notes | D Policy No | E Last Activity by
  Row 1 header (A blank, B-E bold).
  Sections in order; each section is a yellow (#ffff00) bold row across A-E.
  Producer name only on the first row of their group (bold, light blue
  #cfe2f3); accounts in days-to-expiration order; one blank row after each
  producer. Account Name is a hyperlink to
  https://app.ezlynx.com/web/account/{id}/overview.
  E8 (LOCKED 2026-10-05): renewed policies are EXCLUDED from the list —
  there is no green highlight and no carryover.
  E15 (LOCKED 2026-10-05): rows with a pending Cancellation get a red
  (#f4cccc) fill across A-E.

Sheets API formatting is UNPROVEN-UNTIL-LIVE (feasibility finding 4):
dry-run dumps the exact request payloads so they can be reviewed before
any write is enabled.
"""

from __future__ import annotations

import html as html_lib
from dataclasses import dataclass

from . import config
from .logic import AccountRow, group_rows, producer_label

HEADER = ["", "Account Name", "Notes", "Policy No", "Last Activity by"]
ACCOUNT_URL = "https://app.ezlynx.com/web/account/{id}/overview"


@dataclass
class SheetRow:
    """One physical sheet row: values A-E plus formatting intents."""
    values: list[str]
    bold: bool = False
    background: dict | None = None
    background_cols: tuple[int, int] = (0, 5)  # [start, end) column indexes
    is_section: bool = False
    is_producer_first: bool = False
    is_cancellation: bool = False  # E15: pending-Cancellation red fill
    is_blank: bool = False


def account_hyperlink(row: AccountRow) -> str:
    url = ACCOUNT_URL.format(id=row.applicant_id)
    label = row.account_name.replace('"', '""')
    return f'=HYPERLINK("{url}","{label}")'


def build_sheet_rows(rows: list[AccountRow]) -> list[SheetRow]:
    """Assemble the full tab: header, section rows, producer groups, blanks."""
    out: list[SheetRow] = [SheetRow(values=list(HEADER), bold=True)]
    for section, producer, group in group_rows(rows):
        out.append(
            SheetRow(
                values=[section, "", "", "", ""],
                bold=True,
                background=config.COLOR_SECTION_YELLOW,
                is_section=True,
            )
        )
        for i, row in enumerate(group):
            policy_cell = "\n".join(row.policy_lines)
            producer_cell = producer_label(producer) if i == 0 else ""
            sheet_row = SheetRow(
                values=[producer_cell, account_hyperlink(row), row.notes, policy_cell, row.last_activity_by],
                bold=bool(producer_cell),
                is_producer_first=bool(producer_cell),
                is_cancellation=row.cancellation_pending,
            )
            if producer_cell:
                sheet_row.background = config.COLOR_PRODUCER_BLUE
                sheet_row.background_cols = (0, 1)  # col A only, like the docx
            if row.cancellation_pending:
                # E15: red fill across the whole row (wins over producer blue).
                sheet_row.background = config.COLOR_CANCELLATION_RED
                sheet_row.background_cols = (0, 5)
            out.append(sheet_row)
        out.append(SheetRow(values=["", "", "", "", ""], is_blank=True))
    return out


def build_html_table(rows: list[AccountRow]) -> str:
    """Reference HTML table mirroring the docx Step 6 clipboard payload.

    Kept for parity/debugging; the server path writes via the Sheets API.
    """
    parts = ['<table border="1" cellpadding="4" cellspacing="0">']
    parts.append("<tr>" + "".join(
        f"<th><b>{html_lib.escape(h)}</b></th>" if h else "<th></th>" for h in HEADER
    ) + "</tr>")
    for section, producer, group in group_rows(rows):
        parts.append(
            f'<tr style="background-color:#ffff00;font-weight:bold">'
            f"<td>{html_lib.escape(section)}</td><td></td><td></td><td></td><td></td></tr>"
        )
        for i, row in enumerate(group):
            producer_cell = (
                f'<td style="background-color:#cfe2f3;font-weight:bold">'
                f"{html_lib.escape(producer_label(producer))}</td>" if i == 0 else "<td></td>"
            )
            url = ACCOUNT_URL.format(id=row.applicant_id)
            policies = "<br>".join(html_lib.escape(p) for p in row.policy_lines)
            notes = html_lib.escape(row.notes).replace("\n", "<br>")
            # E15: red fill marks pending-Cancellation rows in the reference table.
            style = ' style="background-color:#f4cccc"' if row.cancellation_pending else ""
            parts.append(
                f"<tr{style}>{producer_cell}"
                f'<td><a href="{url}">{html_lib.escape(row.account_name)}</a></td>'
                f"<td>{notes}</td><td>{policies}</td>"
                f"<td>{html_lib.escape(row.last_activity_by)}</td></tr>"
            )
        parts.append("<tr><td></td><td></td><td></td><td></td><td></td></tr>")
    parts.append("</table>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Sheets API request builders (payloads only; sending lives in sheets.py)
# ---------------------------------------------------------------------------


def _rgb(color: dict) -> dict:
    return {"red": color["red"], "green": color["green"], "blue": color["blue"]}


def format_requests(sheet_id: int, rows: list[SheetRow]) -> list[dict]:
    """Build the batchUpdate request list for colors/bold/widths/wrap."""
    requests: list[dict] = []
    for idx, row in enumerate(rows):
        if row.background:
            requests.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": idx,
                            "endRowIndex": idx + 1,
                            "startColumnIndex": row.background_cols[0],
                            "endColumnIndex": row.background_cols[1],
                        },
                        "cell": {
                            "userEnteredFormat": {"backgroundColor": _rgb(row.background)}
                        },
                        "fields": "userEnteredFormat.backgroundColor",
                    }
                }
            )
        if row.bold:
            requests.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": idx,
                            "endRowIndex": idx + 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": 5,
                        },
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat.bold",
                    }
                }
            )
    # Column widths per docx Step 6.
    for col_idx, (_, width) in enumerate(
        [("A", 180), ("B", 340), ("C", 420), ("D", 250), ("E", 110)]
    ):
        requests.append(
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": col_idx,
                        "endIndex": col_idx + 1,
                    },
                "properties": {"pixelSize": width},
                    "fields": "pixelSize",
                }
            }
        )
    # Wrap text on Notes (column C).
    requests.append(
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": len(rows),
                    "startColumnIndex": 2,
                    "endColumnIndex": 3,
                },
                "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP"}},
                "fields": "userEnteredFormat.wrapStrategy",
            }
        }
    )
    return requests
