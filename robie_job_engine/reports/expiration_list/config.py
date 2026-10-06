"""Static configuration for the Weekly Expiration List report.

Sandeep Yadav's answers (E1-E15) are LOCKED as of 2026-10-05 (see
ASSUMPTIONS.md). Items marked LOCKED are Sandeep's rules, not heuristics.
"""

from __future__ import annotations

# X1 (LOCKED 2026-10-06): canonical sheet CONFIRMED by Sandeep —
# https://docs.google.com/spreadsheets/d/1wj8ywd5zMs_ub2ugOxNEulRm09bF60BlYIjE44zUvTg/edit (gid=0).
# Edit access on THIS sheet was NOT explicitly confirmed (his 2026-10-06
# "edit access already given" was about the change-request sheet). Default
# mode stays dry-run: zero sheet writes unless --write is passed explicitly.
# Confirm edit access + eyeball one dry-run before any --write.
SHEET_ID = "1wj8ywd5zMs_ub2ugOxNEulRm09bF60BlYIjE44zUvTg"
# E13 (LOCKED 2026-10-05): each week writes to a NEW dated tab
# "<base> YYYY-MM-DD". Sheet1 is the read-only template — NEVER written.
TEMPLATE_TAB = "Sheet1"
TAB_NAME_BASE = "Expiration List"  # E13
DEFAULT_TAB = TAB_NAME_BASE  # --tab default (the dated base; the writer appends YYYY-MM-DD).

TIMEZONE = "America/New_York"  # X2 assumption.

# E1 (LOCKED): team-agreed window is 30 days.
WINDOW_DAYS = 30
# Docx Step 2: "expirationDate between today and today+31 days".
POLICY_WINDOW_DAYS = 31
# E5 (LOCKED 2026-10-05): renewal cycle start per line of business —
# 90 days for personal lines, 120 days for commercial/trucking.
# cycle_days_for_lob() maps the PolicyCard `lob` text; unknown or missing
# LOB falls back to DEFAULT_CYCLE_DAYS and is flagged in the run summary.
PERSONAL_LINES_CYCLE_DAYS = 90
DEFAULT_CYCLE_DAYS = 120
# INFERRED personal-lines markers (Sandeep did not give the full LOB
# vocabulary — see ASSUMPTIONS.md E5).
PERSONAL_LOB_MARKERS = (
    "personal auto",
    "homeowners",
    "home owner",
    "dwelling",
    "condo",
    "renters",
    "umbrella",
    "motorcycle",
    "boat",
    "rv",
    "personal lines",
)
# INFERRED commercial/trucking markers (120-day cycle). Anything present
# but matching NEITHER list -> 120-day fallback and flagged in the run
# summary for review.
COMMERCIAL_LOB_MARKERS = (
    "commercial",
    "business owners",
    "bop",
    "general liability",
    "workers comp",
    "trucking",
    "motor carrier",
    "cargo",
    "garage",
    "inland marine",
    "professional liability",
    "surety",
    "cyber",
    "farm",
)

# E3 (LOCKED): Retention Center paging; continue to the next page iff the
# last row of the current page has DaysToExpiration <= WINDOW_DAYS and
# another page exists.
RETENTION_PAGE_SIZE = 100

# E12 (LOCKED): truncate at the last sentence boundary (". "/"! "/"? ")
# at or before char 450 (punctuation included); if none, word boundary;
# append "…".
NOTE_CHAR_CAP = 450

# E7 (LOCKED): non-renewal marker — exact string, en dash.
NONRENEWAL_NOTE_PREFIX = "NON-RENEWAL – "
# E15 (LOCKED): pending-Cancellation marker — exact string, en dash.
CANCELLATION_NOTE_PREFIX = "CANCELLATION PENDING – "

# E4: no renewal discussion tied to the expiring policy -> this text.
NO_NOTES_TEXT = "Upcoming renewal add notes here - No notes"

# Colors (Sheets API RGB). Formatting via the Sheets API is
# UNPROVEN-UNTIL-LIVE (feasibility finding 4).
COLOR_SECTION_YELLOW = {"red": 1.0, "green": 1.0, "blue": 0.0}
COLOR_PRODUCER_BLUE = {"red": 0.812, "green": 0.886, "blue": 0.953}  # #cfe2f3
# E15 (LOCKED): red fill for pending-Cancellation rows (#f4cccc).
COLOR_CANCELLATION_RED = {"red": 244 / 255, "green": 204 / 255, "blue": 204 / 255}

# Column widths (px) per docx Step 6.
COLUMN_WIDTHS = {"A": 180, "B": 340, "C": 420, "D": 250, "E": 110}

# E10 (LOCKED 2026-10-05): keep the hardcoded roster for now. Sandeep's
# roster/department source is the AppSheet employees view
# (https://www.appsheet.com/start/a1d9136f-5471-48b3-bcd7-9137cdbf6b15?platform=desktop#appName=NewApp-868850307&view=Employees)
# but a bounded Drive search found no backing sheet for that app
# (its Drive folder NewApp-868850307 contains only _recoveryData), so
# there is nothing automatable to read yet. See ASSUMPTIONS.md E10.
SECTIONS = [
    ("Commercial", ["Taylor Cimei", "Andrea Illanes", "Sandy Santana", "Angie Valladarez"]),
    ("Personal", ["Daniela Aguilar", "Jazmin Molina", "Ashley Huntley"]),
    ("Trucking", ["Maria Bara", "Ricardo Aguilar"]),
]
# Display labels: Daniela Aguilar -> "Daniela", Ashley Huntley -> "Ashley".
PRODUCER_LABELS = {
    "Daniela Aguilar": "Daniela",
    "Ashley Huntley": "Ashley",
}

# E4 (LOCKED 2026-10-05): renewal-discussion title matching.
# Include (case-insensitive substring): renew / renwal / non-renew /
# "mortgage verification"; plus whole-word NR.
# Reject (case-insensitive substring, checked FIRST): the six markers below.
# The discussion must ALSO be tied to the expiring policy number
# (in the title OR in its notes), else Notes = no-notes text.
RENEWAL_TITLE_PATTERNS = (
    "renew",      # covers renewal, renew, non-renewal, nonrenewal
    "renwal",     # common misspelling called out in the docx
    "non-renew",  # explicit, per Sandeep
    "mortgage verification",
)
# Whole-word patterns (checked separately so "NR" doesn't match inside words).
RENEWAL_TITLE_WORD_PATTERNS = ("nr",)
# Rejected titles, case-insensitive substring on the title.
RENEWAL_TITLE_REJECT = (
    "master certificate renewal",
    "policy change request",
    "certificate",
    "coi",
    "audit",
    "eva",
)

# Non-renewal activity markers (title or note text), per docx Step 2b.
NONRENEWAL_PATTERNS = ("non-renewal", "nonrenewal", "non-renewed", "upcoming nr")
NONRENEWAL_WORD_PATTERNS = ("nr",)

# Step 3 bot/automation skip heuristic (UNVERIFIED, finding 5 / E4).
BOT_TEXT_MARKERS = (
    "robie was here",
    "automation center",
    "text sent by",
    "lead info lead id",
    "underwriter email response received",
)
BOT_AUTHOR_MARKERS = ("robie", "automation", "system", "accounting")
# Bare placeholder note text that counts as "no staff note".
PLACEHOLDER_NOTE_TEXT = "upcoming renewal add notes here"

# Google auth: same pattern as streetsmart_agency_exports_sheets_push.py.
# Token path resolution order: env ROBIE_GOOGLE_TOKEN_FILE, then default.
DEFAULT_GOOGLE_TOKEN_FILE = "/opt/streetsmart-hermes/.hermes/google_token.json"
GOOGLE_TOKEN_ENV = "ROBIE_GOOGLE_TOKEN_FILE"

# State dir for run fingerprints (env EXPIRATION_LIST_STATE_DIR overrides).
STATE_DIR_ENV = "EXPIRATION_LIST_STATE_DIR"

# E14 (LOCKED 2026-10-05): verification checklist — printed in every run
# summary, not a code gate.
VERIFICATION_CHECKLIST = (
    "E14: keep the verification step on every run until a few consecutive "
    "clean runs, then spot-check ~10 accounts per run; re-run the batch if "
    "more than 5% of rows are corrected."
)

# X6 (LOCKED 2026-10-06): the three reports (change tracker, expiration
# list, missed calls) run every Monday. Suggested time (~8:00 AM ET) is
# PROPOSED but UNCONFIRMED. No timer is installed (merge/timer freeze in
# effect) — this constant records the decision only.
SCHEDULE_WEEKDAY = "Monday"
