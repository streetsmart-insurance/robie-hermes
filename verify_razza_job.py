from __future__ import annotations

import json
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

from robie_job_engine.models import JobStatus, VerificationEvidence
from robie_job_engine.store import JobStore


JOB_ID = "5d88bd97-d820-4c26-b1a8-888299aaf3b1"
DB_PATH = "/opt/streetsmart-hermes/robie-job-engine/data/jobs.db"
ACCOUNT_OVERVIEW = "https://app.ezlynx.com/web/account/31897605/overview"
SUBMISSION = (
    "https://app.ezlynx.com/web/account/31897605/submissions-version-2/150908"
)
DESTINATION = (
    "https://app.ezlynx.com/web/account/31897605/submissions-version-2/"
    "150908/carrier-submissions/404114"
)
DOCUMENTS = "https://app.ezlynx.com/web/account/31897605/documents"
SUBMISSION_FOLDER = "Submission Folder for Razza Renewal"

EXPECTED = {
    "submission_activity_title": "Submission Added",
    "attachment_filename": "Razza Renewal - The Hartford Workers Compensation Quote.pdf",
    "attachment_note": (
        "ROBIE was here. The Hartford Workers Compensation quote premium is $24,622.00."
    ),
    "workers_compensation_status": "Quoted",
    "gross_premium": "$24,622.00",
}


def main() -> int:
    observed: dict[str, object] = {
        "fresh_navigation": False,
        "destination_url": DESTINATION,
    }
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp("http://127.0.0.1:9222")
        pages = [page for context in browser.contexts for page in context.pages]
        page = next(
            (page for page in pages if "app.ezlynx.com" in page.url),
            pages[0] if pages else None,
        )
        if page is None:
            raise RuntimeError("persistent EZLynx browser has no page")

        page.goto(ACCOUNT_OVERVIEW, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(2_000)
        page.goto(DESTINATION, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(4_000)
        page.reload(wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(4_000)
        body = page.locator("body").inner_text(timeout=20_000)
        observed.update(
            {
                "fresh_navigation": True,
                "final_url": page.url,
                "workers_compensation_status": (
                    "Quoted"
                    if "Workers Compensation" in body and "Quoted" in body
                    else None
                ),
                "gross_premium": (
                    EXPECTED["gross_premium"] if EXPECTED["gross_premium"] in body else None
                ),
            }
        )

        # Verify the note in the submission's own activity surface, not in the
        # carrier-submission detail DOM.
        page.goto(SUBMISSION, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(4_000)
        submission_body = page.locator("body").inner_text(timeout=20_000)
        if "Quoted" in submission_body:
            observed["workers_compensation_status"] = "Quoted"
        view_activity = page.get_by_role("button", name="View activity")
        if view_activity.count():
            view_activity.first.click()
            page.wait_for_timeout(4_000)
        activity_body = page.locator("body").inner_text(timeout=20_000)
        observed.update(
            {
                "submission_activity_title": (
                    EXPECTED["submission_activity_title"]
                    if EXPECTED["submission_activity_title"] in activity_body
                    else None
                ),
                "attachment_note": (
                    EXPECTED["attachment_note"]
                    if EXPECTED["attachment_note"] in activity_body
                    else None
                ),
            }
        )

        # The account root can contain the file while the required submission
        # folder does not. Open the exact folder and verify inside it.
        page.goto(ACCOUNT_OVERVIEW, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(2_000)
        page.goto(DOCUMENTS, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(5_000)
        root_body = page.locator("body").inner_text(timeout=20_000)
        folder = page.get_by_text(SUBMISSION_FOLDER, exact=True)
        folder_found = bool(folder.count())
        if folder_found:
            folder.first.click()
            page.wait_for_timeout(4_000)
        folder_body = page.locator("body").inner_text(timeout=20_000)
        observed.update(
            {
                "attachment_exists_in_account_documents": (
                    EXPECTED["attachment_filename"] in root_body
                ),
                "submission_folder": SUBMISSION_FOLDER if folder_found else None,
                "attachment_filename": (
                    EXPECTED["attachment_filename"]
                    if folder_found and EXPECTED["attachment_filename"] in folder_body
                    else None
                ),
            }
        )

    verified = bool(
        observed.get("fresh_navigation")
        and observed.get("final_url") == DESTINATION
        and all(observed.get(key) == value for key, value in EXPECTED.items())
    )
    captured = datetime.now(timezone.utc).isoformat()
    store = JobStore(DB_PATH)
    store.increment(JOB_ID, "verification_count")
    store.add_evidence(
        JOB_ID,
        verified,
        VerificationEvidence(
            method="fresh_page_reload",
            source="EZLynx fresh submission detail, submission activity, and document-folder DOM",
            expected=EXPECTED,
            observed=observed,
            authoritative=True,
            captured_at=captured,
            locator=DESTINATION,
        ),
    )
    if verified:
        job = store.get_job(JOB_ID)
        if job["status"] == JobStatus.UNVERIFIED:
            store.transition(
                JOB_ID,
                JobStatus.COMPLETE,
                expected={JobStatus.UNVERIFIED},
                release_lease=True,
            )
    print(json.dumps({"job_id": JOB_ID, "verified": verified, "observed": observed}))
    return 0 if verified else 2


if __name__ == "__main__":
    raise SystemExit(main())
