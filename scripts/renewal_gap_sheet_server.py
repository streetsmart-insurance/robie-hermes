#!/usr/bin/env python3
"""Build/rebuild the weekly Renewal Gaps Google Sheet.

Reads: /tmp/renewal_deepdive.json (deep-dive: discussions + documents per account)
Writes: Google Sheet "Renewal Gaps — No Renewal in System" (tabs Personal, Commercial, Trucking)

Layout per tab:
  Row 1: info blurb (merged A1:M1) — flood-policy caveat
  Row 2: headers (frozen)
  Row 3+: data sorted by expiration ascending

Used by the weekly-renewal-gap-report cron (Wednesdays ~12 PM ET).
"""
import json, sys
from datetime import date
import google.auth
from googleapiclient.discovery import build as gbuild

SPREADSHEET_ID = "1yn5BBigaZTAfzzcEUKikar5syG4VoJ0nwh52h_Ocbi4"
SHEET_URL = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit"
DEEPDIVE = "/tmp/renewal_deepdive.json"

BLURB = ("Note: Flood insurance policies may not appear until the flood policy is actually paid. "
         "Some carriers do not release the renewal until it is paid and entered in the system. "
         "If your policy falls in one of these categories, it may not show here even though "
         "the renewal is in motion.")

COLUMNS = ["Expires", "Policy #", "LOB", "Status", "Insured", "Carrier", "Premium",
           "Producer", "CSR", "Days Left", "Last Activity",
           "What's Going On", "AI Assessment"]
# 0-indexed column widths in pixels
WIDTHS = [90, 160, 130, 200, 220, 190, 90, 130, 130, 75, 170, 380, 460]

TABS = ["Personal", "Commercial", "Trucking"]
ENTRY_TAB = "Renewal Entry Queue"   # fourth tab: data-entry worklist, NOT alarms
ALL_TABS = TABS + [ENTRY_TAB]
NCOLS = len(COLUMNS)

ENTRY_BLURB = ("DATA-ENTRY WORKLIST (not an alarm list). These renewals have paperwork "
               "in the EZLynx file — renewal offers, dec pages, EzSign apps, carrier paperwork — "
               "but no renewal term was entered. The CSR/AM needs to key each one in. "
               "Per Carlo's rule: if we have a renewal, it is NOT a gap.")

# Classification labels (Carlo 2026-10-06: every policy with no renewal term
# entered is on the report AS MISSING — paperwork or not)
STATUS_NEEDS_ENTRY = "MISSING \u2014 RENEWAL PAPERWORK IN FILE, NEEDS ENTRY"
STATUS_CHASE = "MISSING \u2014 NO RENEWAL IN SYSTEM, CHASE CARRIER"

# Row highlight colors (Sheets API RGB 0-1)
NEEDS_ENTRY_COLOR = {"red": 1.0, "green": 0.98, "blue": 0.80}   # light yellow
CHASE_COLOR = {"red": 1.0, "green": 0.85, "blue": 0.85}          # light red/pink

# Row layout: 0-indexed for API, 1-indexed comments
BLURB_ROW = 0      # row 1
HEADER_ROW = 1     # row 2
DATA_START = 2     # row 3


def days_left(expires):
    try:
        return (date.fromisoformat(expires[:10]) - date.today()).days
    except Exception:
        return None


def classify(rec):
    """Classify renewal status.

    - "RENEWAL PAPERWORK IN FILE — NEEDS ENTRY": renewal-related content (RWL
      documents, renewal offers/quotes, dec pages, EzSign apps, or even
      renewal folder structure) is in the EZLynx file but no renewal term was
      entered. This is a DATA-ENTRY task for the CSR/AM.
    - "NO RENEWAL PAPERWORK — CHASE CARRIER": nothing renewal-related in the
      file at all. This is a CARRIER-CHASE task.

    Uses the deep-dive's pre-filtered renewal_doc_names (non-empty = renewal
    activity in the file). Non-renewal notices alone do not count as actionable
    paperwork, but policies with both are flagged NEEDS ENTRY with a verify note.
    """
    def is_actionable_renewal(name):
        n = (name or "").strip().lower()
        if not n:
            return False
        if "non-renewal" in n or "nonrenewal" in n or "non renewal" in n:
            # A pure non-renewal notice is not paperwork to enter — but if the
            # file ALSO has real renewal docs, it still needs human review.
            return False
        return True

    rdn = [n for n in (rec.get("renewal_doc_names") or []) if is_actionable_renewal(n)]
    if rdn:
        return STATUS_NEEDS_ENTRY
    # Fallback: scan raw documents for renewal indicators
    for d in rec.get("documents") or []:
        n = (d.get("name") or "").lower()
        if n and is_actionable_renewal(n) and any(
                kw in n for kw in ("rwl", "renewal", "ezsign", "quote", "proposal")):
            return STATUS_NEEDS_ENTRY
    return STATUS_CHASE


def analyze(rec):
    # Browser-verified data (triple-check) always wins over generated analysis.
    if rec.get("verified_ai_assessment"):
        return (rec.get("verified_last_activity") or "Verified in EZLynx",
                rec.get("verified_whats_going_on") or "",
                rec["verified_ai_assessment"])
    dl = days_left(rec.get("expires", ""))
    dl_str = f"{dl} days left" if dl is not None else "date unknown"
    urgent = f"URGENT — {dl_str}." if dl is not None and dl <= 7 else f"{dl_str}."

    discs = sorted(rec.get("discussions", []),
                   key=lambda d: d.get("lastModified", ""), reverse=True)
    policy_discs = rec.get("policy_discussions", [])
    docs = rec.get("documents", [])

    if discs:
        d0 = discs[0]
        last_activity = f"{d0['title'][:50]} ({d0['lastModified'][:10]})"
    elif docs:
        last_activity = f"Document filed: {docs[0]['name'][:50]}"
    else:
        last_activity = "No activity found"

    parts = []
    if policy_discs:
        pd = policy_discs[0]
        parts.append(f"Policy-specific renewal thread: '{pd['title'][:60]}' — "
                     f"{pd['noteCount']} notes, last {pd['lastModified'][:10]}.")
    elif discs:
        d0 = discs[0]
        parts.append(f"Most recent renewal thread (account-level): '{d0['title'][:60]}' — "
                     f"{d0['noteCount']} notes, last {d0['lastModified'][:10]}.")
    else:
        parts.append("No renewal discussions on the account.")
    if docs:
        names = "; ".join(d["name"][:50] for d in docs[:3])
        parts.append(f"Renewal document(s) in file: {names}.")
    else:
        parts.append("No renewal documents in file.")
    whats_going_on = " ".join(parts)

    has_recent_thread = False
    thread_age = None
    if discs:
        try:
            thread_age = (date.today() - date.fromisoformat(discs[0]["lastModified"][:10])).days
            has_recent_thread = thread_age <= 14
        except Exception:
            pass

    if docs and has_recent_thread:
        ai = (f"Renewal is being worked (thread active {thread_age}d ago) and the offer is in the file, "
              f"but the renewal term is NOT entered in EZLynx. Team needs to key it in now. {urgent}")
    elif docs and not has_recent_thread:
        ai = (f"Renewal offer is sitting in the file but no active renewal thread and nothing entered. "
              f"Team needs to pick this up and enter the renewal. {urgent}")
    elif not docs and has_recent_thread:
        ai = (f"Renewal thread is active ({thread_age}d ago) but no offer document filed and no term entered. "
              f"Chase the carrier/client for the renewal offer. {urgent}")
    else:
        ai = (f"No renewal thread, no documents — this renewal hasn't been started. "
              f"Team needs to chase it immediately. {urgent}")

    return last_activity, whats_going_on, ai


_SVC = None
def _svc():
    global _SVC
    if _SVC is None:
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/spreadsheets"])
        _SVC = gbuild("sheets", "v4", credentials=creds)
    return _SVC

def api(*parts, params=None, body=None):
    """Same call signature as the hatch_gws_cli wrapper, backed by the
    Sheets API directly (server-side: VM service account)."""
    s = _svc()
    params = dict(params or {})
    try:
        if parts == ("spreadsheets", "get"):
            return s.spreadsheets().get(**params).execute()
        if parts == ("spreadsheets", "batchUpdate"):
            return s.spreadsheets().batchUpdate(
                spreadsheetId=params["spreadsheetId"], body=body).execute()
        if parts == ("spreadsheets", "values", "clear"):
            return s.spreadsheets().values().clear(**params).execute()
        if parts == ("spreadsheets", "values", "update"):
            return s.spreadsheets().values().update(body=body, **params).execute()
        raise ValueError(f"unsupported api call: {parts}")
    except Exception as e:
        print(f"API ERROR {' '.join(parts)}: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    with open(DEEPDIVE) as f:
        deepdive = json.load(f)
    # Carlo's rules:
    # - "If we do have a renewal, it is NOT a gap." (2026-10-05)
    # - "Anytime there is no entry we need this on the report as missing."
    #   (2026-10-06)
    # - "They need to be on each tab." (2026-10-06) — every policy with no
    #   renewal term entered appears on its department tab, labeled MISSING.
    #   The Status column carries the action distinction (chase carrier vs
    #   needs entry). The Entry Queue tab remains the consolidated worklist.
    by_dept = {"Personal": [], "Commercial": [], "Trucking": []}
    entry_queue = []
    for rec in deepdive:
        by_dept.setdefault(rec.get("dept", "Commercial"), []).append(rec)
        if classify(rec) == STATUS_NEEDS_ENTRY:
            entry_queue.append(rec)
    for recs in by_dept.values():
        recs.sort(key=lambda r: r.get("expires", ""))
    entry_queue.sort(key=lambda r: r.get("expires", ""))

    # Get tab sheet IDs (create the Entry Queue tab if missing)
    meta = api("spreadsheets", "get",
               params={"spreadsheetId": SPREADSHEET_ID,
                       "fields": "sheets(properties(sheetId,title))"})
    sheet_ids = {}
    for s in meta["sheets"]:
        t = s["properties"]["title"]
        if t in ALL_TABS:
            sheet_ids[t] = s["properties"]["sheetId"]
    if ENTRY_TAB not in sheet_ids:
        add = api("spreadsheets", "batchUpdate",
                  params={"spreadsheetId": SPREADSHEET_ID},
                  body={"requests": [{"addSheet": {"properties": {"title": ENTRY_TAB}}}]})
        new_id = add["replies"][0]["addSheet"]["properties"]["sheetId"]
        sheet_ids[ENTRY_TAB] = new_id
        print(f"Created tab '{ENTRY_TAB}' (sheetId {new_id})")
    missing = [t for t in ALL_TABS if t not in sheet_ids]
    if missing:
        print(f"Missing tabs: {missing}", file=sys.stderr)
        sys.exit(1)
    print(f"Sheet IDs: {sheet_ids}")

    def write_tab(tab, records, blurb_text):
        rows = [[blurb_text], COLUMNS]
        status_rows = []  # (0-indexed row number, status) for color-coding
        urgent_rows = []  # 0-indexed row numbers in the sheet
        for rec in records:
            dl = days_left(rec.get("expires", ""))
            status = classify(rec)
            last_act, going_on, assessment = analyze(rec)
            rows.append([
                rec.get("expires", "")[:10], rec.get("policy"), rec.get("lob", ""),
                status,
                rec.get("insured"), rec.get("carrier"), rec.get("premium"),
                rec.get("producer"), rec.get("csr"), dl,
                last_act, going_on, assessment,
            ])
            status_rows.append((len(rows) - 1, status))
            if dl is not None and dl <= 7:
                urgent_rows.append(len(rows) - 1)

        sid = sheet_ids[tab]
        nrows = len(rows)

        # 1. Clear the tab first (stale rows from a bigger previous week)
        api("spreadsheets", "values", "clear",
            params={"spreadsheetId": SPREADSHEET_ID, "range": f"{tab}!A1:M1000"})

        # 2. Write values (blurb row, header row, data)
        api("spreadsheets", "values", "update",
            params={"spreadsheetId": SPREADSHEET_ID,
                    "range": f"{tab}!A1:M{nrows}",
                    "valueInputOption": "USER_ENTERED"},
            body={"values": rows})

        # 3. Format everything in one batchUpdate
        reqs = [
            # Merge blurb row
            {"mergeCells": {"range": {"sheetId": sid,
                                      "startRowIndex": BLURB_ROW,
                                      "endRowIndex": BLURB_ROW + 1,
                                      "startColumnIndex": 0,
                                      "endColumnIndex": NCOLS},
                            "mergeType": "MERGE_ALL"}},
            # Blurb styling: info blue background, italic, wrapped
            {"repeatCell": {
                "range": {"sheetId": sid,
                          "startRowIndex": BLURB_ROW, "endRowIndex": BLURB_ROW + 1},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": {"red": 0.85, "green": 0.92, "blue": 0.99},
                    "textFormat": {"italic": True, "fontSize": 10,
                                   "foregroundColor": {"red": 0.15, "green": 0.25, "blue": 0.4}},
                    "wrapStrategy": "WRAP", "verticalAlignment": "MIDDLE"}},
                "fields": "userEnteredFormat(backgroundColor,textFormat,wrapStrategy,verticalAlignment)"}},
            # Header styling
            {"repeatCell": {
                "range": {"sheetId": sid,
                          "startRowIndex": HEADER_ROW, "endRowIndex": HEADER_ROW + 1},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": {"red": 0.12, "green": 0.31, "blue": 0.47},
                    "textFormat": {"bold": True,
                                   "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                                   "fontSize": 11},
                    "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
                    "wrapStrategy": "WRAP"}},
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment,wrapStrategy)"}},
            # Freeze blurb + header rows
            {"updateSheetProperties": {
                "properties": {"sheetId": sid,
                               "gridProperties": {"frozenRowCount": 2}},
                "fields": "gridProperties.frozenRowCount"}},
            # Blurb row height (taller for wrapped text)
            {"updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "ROWS",
                          "startIndex": BLURB_ROW, "endIndex": BLURB_ROW + 1},
                "properties": {"pixelSize": 54},
                "fields": "pixelSize"}},
        ]
        # Filter on the header row
        if nrows > 2:
            reqs.append({"setBasicFilter": {
                "filter": {"range": {"sheetId": sid,
                                     "startRowIndex": HEADER_ROW,
                                     "endRowIndex": nrows,
                                     "startColumnIndex": 0,
                                     "endColumnIndex": NCOLS}}}})
        # Column widths
        for ci, px in enumerate(WIDTHS):
            reqs.append({"updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "COLUMNS",
                          "startIndex": ci, "endIndex": ci + 1},
                "properties": {"pixelSize": px},
                "fields": "pixelSize"}})
        # Status color-coding on the FULL data row (applied first):
        # NEEDS ENTRY = light yellow, CHASE CARRIER = light red/pink.
        for ri, status in status_rows:
            color = NEEDS_ENTRY_COLOR if status == STATUS_NEEDS_ENTRY else CHASE_COLOR
            reqs.append({"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": ri, "endRowIndex": ri + 1,
                          "startColumnIndex": 0, "endColumnIndex": NCOLS},
                "cell": {"userEnteredFormat": {"backgroundColor": color}},
                "fields": "userEnteredFormat.backgroundColor"}})
        # Bold the Status cell text on top of the row color
        for ri, status in status_rows:
            reqs.append({"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": ri, "endRowIndex": ri + 1,
                          "startColumnIndex": 3, "endColumnIndex": 4},
                "cell": {"userEnteredFormat": {
                    "textFormat": {"bold": True, "fontSize": 10}}},
                "fields": "userEnteredFormat.textFormat"}})
        # Urgent rows (<=7 days) highlighted pink — applied LAST so urgency wins
        for ri in urgent_rows:
            reqs.append({"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": ri, "endRowIndex": ri + 1},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": {"red": 0.99, "green": 0.89, "blue": 0.92}}},
                "fields": "userEnteredFormat.backgroundColor"}})

        api("spreadsheets", "batchUpdate",
            params={"spreadsheetId": SPREADSHEET_ID},
            body={"requests": reqs})
        print(f"{tab}: {nrows - 2} data rows written, {len(urgent_rows)} urgent, blurb set")

    # Main tabs: ONLY true gaps (no term, no documents) — Carlo's rule
    for tab in TABS:
        write_tab(tab, by_dept.get(tab, []), BLURB)
    # Fourth tab: the data-entry worklist (renewal paperwork in file, needs keying in)
    write_tab(ENTRY_TAB, entry_queue, ENTRY_BLURB)

    print(f"DONE — {SHEET_URL}")


if __name__ == "__main__":
    main()
