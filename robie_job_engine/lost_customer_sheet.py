"""Publish the monthly lost-customer retention review as a shared Google Sheet.

The Prod VM credential only carries ``spreadsheets`` + ``drive.file`` scopes,
so it can read the Lost Customers Report but cannot export or share it. The
monthly copy is therefore created and shared as the ``robie@`` mailbox via the
same domain-wide-delegated service account used for Gmail (scope: ``drive``).

Fail-closed: the copied tabs are read back and compared with the validated
source values before any permission is granted. Any mismatch raises, and the
caller must not send email without a verified sheet.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from .lost_customer_retention import REQUIRED_REVIEW_HEADERS, normalize, records


DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
SPREADSHEET_MIME = "application/vnd.google-apps.spreadsheet"
FOLDER_MIME = "application/vnd.google-apps.folder"
REVIEW_SOURCE_TAB = "3-Month Account Review"
REVIEW_TAB = "Account Review"
MONTHLY_RANGE = "A1:AK997"
REVIEW_RANGE = "A1:U2000"
VALID_ROLES = {"reader", "commenter", "writer"}
DEFAULT_SHARE_ROLES = {
    "personal": "reader",
    "commercial": "reader",
    "trucking": "reader",
    "carlo": "writer",
    "jake": "writer",
}


def delegated_google_clients(service_account_email: str, subject: str) -> tuple[Any, Any]:
    """Return (sheets, drive) clients acting as ``subject`` via keyless DWD."""
    import google.auth
    from google.auth import iam
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    source, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    signer = iam.Signer(Request(), source, service_account_email)
    delegated = service_account.Credentials(
        signer=signer,
        service_account_email=service_account_email,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=[DRIVE_SCOPE],
        subject=subject,
    )
    return (
        build("sheets", "v4", credentials=delegated, cache_discovery=False),
        build("drive", "v3", credentials=delegated, cache_discovery=False),
    )


def sheet_title(period_label: str) -> str:
    return f"Lost Customer Retention — {period_label}"


def sheet_url(spreadsheet_id: str, gid: int | None = None) -> str:
    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
    return f"{url}#gid={gid}" if gid is not None else url


def share_plan(recipients: Mapping[str, str], roles: Mapping[str, str] | None = None) -> dict[str, str]:
    """Map recipient email -> Drive role. Unknown roles fail closed."""
    roles = dict(DEFAULT_SHARE_ROLES if roles is None else roles)
    plan: dict[str, str] = {}
    for key, email in recipients.items():
        role = roles.get(key)
        if role is None:
            raise ValueError(f"no sheet share role configured for recipient key {key!r}")
        if role not in VALID_ROLES:
            raise ValueError(f"invalid sheet share role {role!r} for {key!r}")
        address = normalize(email).casefold()
        if "@" not in address:
            raise ValueError(f"invalid recipient email for {key!r}")
        # Highest role wins if one address appears under two keys.
        rank = ("reader", "commenter", "writer")
        if address not in plan or rank.index(role) > rank.index(plan[address]):
            plan[address] = role
    return plan


def review_rows_to_delete(values: Sequence[Sequence[Any]], months: Iterable[str]) -> list[tuple[int, int]]:
    """Return descending [start, end) row ranges (0-based) outside ``months``.

    Row 0 is the header and is always kept. Blank rows are kept.
    """
    keep = {normalize(m) for m in months}
    doomed = [
        index for index, row in enumerate(values)
        if index > 0 and any(normalize(c) for c in row) and normalize(row[0] if row else "") not in keep
    ]
    ranges: list[tuple[int, int]] = []
    for index in doomed:
        if ranges and ranges[-1][1] == index:
            ranges[-1] = (ranges[-1][0], index + 1)
        else:
            ranges.append((index, index + 1))
    return sorted(ranges, reverse=True)


def _review_signature(items: Iterable[Mapping[str, Any]]) -> list[tuple[str, ...]]:
    return sorted(tuple(normalize(item.get(h, "")) for h in REQUIRED_REVIEW_HEADERS) for item in items)


def verify_published_values(
    *,
    monthly_expected: Mapping[str, Sequence[Sequence[Any]]],
    monthly_actual: Mapping[str, Sequence[Sequence[Any]]],
    review_expected: Sequence[Mapping[str, Any]],
    review_actual_values: Sequence[Sequence[Any]],
    months: Sequence[str],
) -> dict[str, Any]:
    for month, expected in monthly_expected.items():
        actual = monthly_actual.get(month)
        if [list(r) for r in (actual or [])] != [list(r) for r in expected]:
            raise RuntimeError(f"fail-closed: published '{month}' tab does not match the validated source")
    actual_items = records(review_actual_values)
    stray = sorted({i["Month"] for i in actual_items if i["Month"] not in set(months)})
    if stray:
        raise RuntimeError(f"fail-closed: published Account Review contains other months: {', '.join(stray)}")
    if _review_signature(actual_items) != _review_signature(review_expected):
        raise RuntimeError("fail-closed: published Account Review rows do not match the validated review")
    return {"monthly_tabs": list(monthly_expected), "review_rows": len(actual_items)}


def _values(sheets: Any, spreadsheet_id: str, range_name: str) -> list[list[Any]]:
    return sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=range_name, majorDimension="ROWS",
    ).execute().get("values", [])


def _tabs(sheets: Any, spreadsheet_id: str) -> dict[str, int]:
    meta = sheets.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="sheets.properties(sheetId,title)",
    ).execute()
    return {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta.get("sheets", [])}


def _folder_id(drive: Any, folder_name: str) -> str:
    escaped = folder_name.replace("\\", "\\\\").replace("'", "\\'")
    found = drive.files().list(
        q=f"name = '{escaped}' and mimeType = '{FOLDER_MIME}' and 'root' in parents and trashed = false",
        fields="files(id)", pageSize=2, spaces="drive",
    ).execute().get("files", [])
    if found:
        return found[0]["id"]
    return drive.files().create(
        body={"name": folder_name, "mimeType": FOLDER_MIME}, fields="id",
    ).execute()["id"]


def _existing_spreadsheet(drive: Any, spreadsheet_id: str | None) -> str | None:
    if not spreadsheet_id:
        return None
    try:
        meta = drive.files().get(
            fileId=spreadsheet_id, fields="id,trashed,mimeType", supportsAllDrives=True,
        ).execute()
    except Exception:  # noqa: BLE001 - unreachable/deleted sheet -> create a new one
        return None
    if meta.get("trashed") or meta.get("mimeType") != SPREADSHEET_MIME:
        return None
    return meta["id"]


def _sync_tabs(
    sheets: Any, *, source_id: str, dest_id: str, months: Sequence[str],
) -> dict[str, int]:
    source_tabs = _tabs(sheets, source_id)
    wanted = list(months) + [REVIEW_SOURCE_TAB]
    missing = [t for t in wanted if t not in source_tabs]
    if missing:
        raise RuntimeError(f"fail-closed: source tabs missing: {', '.join(missing)}")
    before = _tabs(sheets, dest_id)
    copied: dict[str, int] = {}
    for title in wanted:
        props = sheets.spreadsheets().sheets().copyTo(
            spreadsheetId=source_id, sheetId=source_tabs[title],
            body={"destinationSpreadsheetId": dest_id},
        ).execute()
        copied[title] = props["sheetId"]
    requests: list[dict[str, Any]] = [{"deleteSheet": {"sheetId": gid}} for gid in before.values()]
    final: dict[str, int] = {}
    for index, title in enumerate(wanted):
        new_title = REVIEW_TAB if title == REVIEW_SOURCE_TAB else title
        final[new_title] = copied[title]
        requests.append({"updateSheetProperties": {
            "properties": {"sheetId": copied[title], "title": new_title, "index": index},
            "fields": "title,index",
        }})
    sheets.spreadsheets().batchUpdate(spreadsheetId=dest_id, body={"requests": requests}).execute()
    review_gid = final[REVIEW_TAB]
    deletes = review_rows_to_delete(_values(sheets, dest_id, f"'{REVIEW_TAB}'!{REVIEW_RANGE}"), months)
    if deletes:
        sheets.spreadsheets().batchUpdate(spreadsheetId=dest_id, body={"requests": [
            {"deleteDimension": {"range": {
                "sheetId": review_gid, "dimension": "ROWS", "startIndex": start, "endIndex": end,
            }}} for start, end in deletes
        ]}).execute()
    return final


def _ensure_permissions(drive: Any, spreadsheet_id: str, plan: Mapping[str, str]) -> list[dict[str, str]]:
    existing = drive.permissions().list(
        fileId=spreadsheet_id, supportsAllDrives=True,
        fields="permissions(id,emailAddress,role,type)",
    ).execute().get("permissions", [])
    by_email = {normalize(p.get("emailAddress")).casefold(): p for p in existing if p.get("type") == "user"}
    applied = []
    for email, role in plan.items():
        current = by_email.get(email)
        if current and current.get("role") in {"owner", "organizer", "fileOrganizer"}:
            applied.append({"email": email, "role": current["role"], "action": "kept"})
            continue
        if current and current.get("role") == role:
            applied.append({"email": email, "role": role, "action": "kept"})
            continue
        if current:
            drive.permissions().update(
                fileId=spreadsheet_id, permissionId=current["id"],
                body={"role": role}, supportsAllDrives=True,
            ).execute()
            applied.append({"email": email, "role": role, "action": "updated"})
            continue
        drive.permissions().create(
            fileId=spreadsheet_id, sendNotificationEmail=False, supportsAllDrives=True,
            body={"type": "user", "role": role, "emailAddress": email}, fields="id",
        ).execute()
        applied.append({"email": email, "role": role, "action": "created"})
    # Read back: every planned address must now hold at least its role.
    rank = {"reader": 0, "commenter": 1, "writer": 2, "fileOrganizer": 3, "organizer": 4, "owner": 5}
    after = drive.permissions().list(
        fileId=spreadsheet_id, supportsAllDrives=True, fields="permissions(emailAddress,role,type)",
    ).execute().get("permissions", [])
    granted = {normalize(p.get("emailAddress")).casefold(): p.get("role") for p in after if p.get("type") == "user"}
    for email, role in plan.items():
        if rank.get(granted.get(email, ""), -1) < rank[role]:
            raise RuntimeError(f"fail-closed: sheet permission for {email} not confirmed ({role})")
    return applied


def publish_retention_sheet(
    *,
    sheets: Any,
    drive: Any,
    source_spreadsheet_id: str,
    months: Sequence[str],
    period_label: str,
    monthly_expected: Mapping[str, Sequence[Sequence[Any]]],
    review_expected: Sequence[Mapping[str, Any]],
    share: Mapping[str, str],
    digest: str,
    existing: Mapping[str, Any] | None = None,
    parent_folder_id: str | None = None,
    folder_name: str = "Lost Customer Retention",
) -> dict[str, Any]:
    """Create (or reuse) and verify the monthly retention Sheet, then share it."""
    existing = dict(existing or {})
    dest_id = _existing_spreadsheet(drive, existing.get("spreadsheet_id"))
    created = False
    if dest_id is None:
        parent = parent_folder_id or _folder_id(drive, folder_name)
        dest_id = drive.files().create(
            body={"name": sheet_title(period_label), "mimeType": SPREADSHEET_MIME, "parents": [parent]},
            fields="id", supportsAllDrives=True,
        ).execute()["id"]
        created = True
    tabs = existing.get("tabs") if not created and existing.get("digest") == digest else None
    resynced = False
    if tabs:
        try:
            verify_published_values(
                monthly_expected=monthly_expected,
                monthly_actual={m: _values(sheets, dest_id, f"'{m}'!{MONTHLY_RANGE}") for m in months},
                review_expected=review_expected,
                review_actual_values=_values(sheets, dest_id, f"'{REVIEW_TAB}'!{REVIEW_RANGE}"),
                months=months,
            )
        except Exception:  # noqa: BLE001 - drifted copy -> rebuild it
            tabs = None
    if not tabs:
        tabs = _sync_tabs(sheets, source_id=source_spreadsheet_id, dest_id=dest_id, months=months)
        resynced = True
    verification = verify_published_values(
        monthly_expected=monthly_expected,
        monthly_actual={m: _values(sheets, dest_id, f"'{m}'!{MONTHLY_RANGE}") for m in months},
        review_expected=review_expected,
        review_actual_values=_values(sheets, dest_id, f"'{REVIEW_TAB}'!{REVIEW_RANGE}"),
        months=months,
    )
    drive.files().update(
        fileId=dest_id, body={"name": sheet_title(period_label)}, supportsAllDrives=True,
    ).execute()
    permissions = _ensure_permissions(drive, dest_id, share)
    first_gid = tabs.get(months[0])
    return {
        "spreadsheet_id": dest_id,
        "title": sheet_title(period_label),
        "url": sheet_url(dest_id),
        "monthly_url": sheet_url(dest_id, first_gid),
        "tabs": tabs,
        "digest": digest,
        "created": created,
        "resynced": resynced,
        "verification": verification,
        "permissions": permissions,
    }
