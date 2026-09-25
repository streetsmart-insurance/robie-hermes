# Monthly WOW report workflow

## Period and working copy

- Use the complete prior calendar month unless the user names a different month.
- Calculate dates in `America/New_York` and use inclusive first-day through last-day filters.
- Copy the approved master WOW spreadsheet. Name the copy `WOW Customer Service - <Month YYYY>` unless the approved naming convention differs.
- Remove prior-period raw rows and filters only from the designated input area in the working copy. Never clear formulas, pivot definitions, protected cells, validation, or scripts.

## EZLynx report extraction

Open EZLynx Reports, Shared Reports, and select `WOW Customer Service ALL EMPLOYEES - UPDATED 2025 New` or its approved replacement.

Set:

- Created Date: first through last day of the reporting month.
- Employees: the current approved WOW-eligible roster. Do not maintain eligibility by deleting names from a historical list. Stop when a new or departed employee is ambiguous.
- Activity types: include `Note`, `Task Creation Note`, and `Task Note`. The walkthrough showed incomplete results when only one note type was selected.
- Labels: use the exact current labels listed in the qualification reference. Do not use transcription variants, abbreviations, or a retired `Value Added` label.

Export CSV. The current UI may package the CSV in an archive; extract it before import. Prefer direct parsing or a native Sheets import over downloading, renaming, uploading, and manually copying columns.

## Canonical raw-data schema

Map the export into these fields while retaining every untouched source column in evidence:

| Canonical field | Observed EZLynx meaning |
| --- | --- |
| `source_activity_id` | Stable task/activity identifier when supplied |
| `account_name` | Account Name |
| `activity_type` | Activity Type |
| `created_at` | Task Created Date / Date Created |
| `discussion` | Discussion |
| `note` | Note |
| `policy_number` | Policy Number |
| `policy_premium` | Policy Premium |
| `created_by` | Task Created By / Created By |
| `labels_raw` | Original label value or values |
| `report_year` | Reporting year |
| `report_month` | Reporting month |

Normalize whitespace and dates but retain the original value. If the export has no stable identifier, create a deterministic audit key from the complete original row; do not use that fallback to merge genuinely distinct activities.

The walkthrough identified a recurring manual-copy defect in which `created_by` did not paste with the other columns. Validate this column independently after every import. A blank or shifted creator is an exception, not permission to infer the employee.

## Candidate construction

- Split only on the exact label representation returned by EZLynx.
- Create one candidate per source activity and exact label.
- Keep a link to the shared source activity so one account with multiple labels remains auditable.
- Deduplicate exact re-exports by stable activity identifier plus label. Do not deduplicate separate qualifying events merely because the account and label match.
- Add `qualification_status`, `qualification_reason`, `evidence_locator`, and `verified_at` fields.

## Spreadsheet load and summaries

1. Confirm the working copy matches the master topology.
2. Clear only the approved raw-data input range.
3. Write normalized candidate rows in a batch and read back the row count and creator column.
4. Refresh the existing pivot tables or formulas.
5. Run the exact approved Apps Script function embedded in the template only after its name and target ranges are verified. The walkthrough called it the updated top-sheet routine; do not guess or create a similarly named function.
6. Read back the account-manager summary and verify that every eligible employee with candidate data appears.

## Reconciliation

The run fails closed unless all explainable relationships hold:

- exported source rows = accepted source rows + rejected malformed rows;
- candidate rows = exact-label occurrences after documented deduplication;
- candidate rows by label = `VERIFIED` + `TRACKING_ONLY` + `NOT_QUALIFIED` + `UNVERIFIED`;
- candidate rows by employee and label = pivot rows by employee and label;
- verified summary totals = verified detail rows;
- every incentive amount maps to a verified record or an explicitly defined grouped threshold;
- no inactive or ambiguous employee receives silent credit.

Record every rejected, duplicate, missing-creator, and unverified row in the exception output.

