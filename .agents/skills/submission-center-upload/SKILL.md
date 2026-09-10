---
name: "submission-center-upload"
description: "Add carriers (preferred and non-preferred), enter quoted premiums, fees, and taxes, upload quote proposals into matching submission folders, and document quote details in EZLynx Submission Center and Activity discussions. Use for EZLynx Submission Center V2 workflows, non-download manual quote intake, MGA/wholesaler proposals, and new business submission processing."
---

# EZLynx Submission Center Upload & Quote Entry SOP

This skill defines the complete standard operating procedure for ingesting, entering, uploading, and documenting carrier quotes in **EZLynx Submission Center V2**, as well as logging audit trails in the account Activity discussions.

---

## 1. Core Workflow Overview

When insurance proposals or quotes are received outside of automated agency download (e.g., from MGAs, surplus lines wholesalers, or direct carrier portals like Specialty Coverage Insurance Agency / SPCIA, Diesel Insurance Solutions, Trinity Underwriters, RT Specialty, Cover Whale, or AmWINS):

1. **Add Carrier to Submission Center**: Search and add the issuing carrier or MGA to the active commercial submission. Enable `Show Non-Preferred Carriers` when the carrier is not on the agency's primary preferred list.
2. **Enter Financial Terms & Set Status**: Set the submission and Line of Business (LOB) status to `Quoted`. Enter the Base Premium, MEP (if applicable), Policy/Inspection Fees, and Surplus Lines Taxes.
3. **Upload Quote & App into Submission Folder**: Upload the authentic quote proposal and application PDFs into the document library folder whose name matches the submission title: `Submission Folder for {Submission Name}`.
4. **Link Documents to Carrier Submission**: Verify the documents are checked and saved against the carrier card.
5. **Log Activity Audit Note**: Post a structured summary note in EZLynx Activity under the appropriate discussion (e.g., `New Business- Contacted`) and folder category (`New Business`), signed with `Robie was here`.
6. **MANDATORY Policy Association**: Every note must explicitly associate to the policy via `--policy-number <Policy#>` and include the top header `Policy: #{policy_number} ({line_of_business} - {carrier_name})`.
7. **EZLynx API Utilities (Fast Operations)**:
   - Check applicant profile & assigned producer: `scripts/ezlynx_cli.py applicant {applicantId} --json`
   - Verify document uploads in Document Library: `scripts/ezlynx_cli.py documents {applicantId} --json`
   - Post discussion audit note (policy associated): `scripts/ezlynx_cli.py note {applicantId} "{NoteText}" --policy-number {policyNumber} --lob "{lob}" --carrier "{carrier}"`

---

## 2. Submission Center Navigation & Carrier Addition

### URL Pattern
```
https://app.ezlynx.com/web/account/{applicant_id}/submissions-version-2/{submission_id}/carrier-submissions
```

### Steps to Add Carrier
1. Click **`Add carrier`** button (`button:has-text("Add carrier")`).
2. **Non-Preferred Filter**: If the carrier or MGA is a surplus lines company, wholesaler, or broker (e.g., `Specialty Coverage Insurance Agency MGA`), check the **`Show Non-Preferred Carriers`** checkbox:
   ```python
   # Playwright selector
   await page.locator('mat-checkbox:has-text("Show Non-Preferred Carriers")').click()
   ```
3. **Search Carrier**: Locate the search input (typically input index 1 in the modal) and type the carrier name:
   ```python
   carrier_input = page.locator('input').nth(1)
   await carrier_input.fill("Specialty Coverage")
   await page.wait_for_timeout(1000)
   ```
4. **Select Carrier & Line of Business**:
   - Check the checkbox next to the carrier name row.
   - Check the checkbox next to the specific Line of Business (e.g., `Auto (Commercial)`, `General Liability`, `Property`).
5. **Commit**: Click **`Save & continue`** (`button:has-text("Save & continue")`).

---

## 3. Entering Quoted Terms & Financial Breakdown

1. Click **`Manage submission`** on the target carrier card (`button:has-text("Manage submission")`).
2. **Update Carrier Status**: In the top carrier card header dropdown (`mat-select.first`), change the status to **`Quoted`**:
   ```python
   status_select = page.locator('mat-select').first
   await status_select.click()
   await page.locator('mat-option:has-text("Quoted")').click()
   ```
3. **Expand Line of Business**: Click the expand chevron icon on the right side of the LOB row (`mat-icon:has-text("keyboard_arrow_down")`).
4. **Set LOB Status to Quoted**: In the expanded LOB status dropdown (`mat-select.nth(3)`), select **`Quoted`**. This dynamically renders the numeric financial input fields:
   - Input #3: **`*Premium`** (Annual Base Premium or full premium as required by line).
   - Input #4: **`MEP`** (Minimum Earned Premium, if stated in quote).
   - Input #5: **`Fees`** (Inspection fees, policy fees, stamping fees).
   - Input #6: **`Taxes`** (State surplus lines taxes).
5. **Fill Financials**:
   ```python
   # Example: Auto (Commercial) with $5,329.82 base, $400 fees, $223.46 taxes
   inputs = page.locator('input')
   await inputs.nth(3).fill("5953.28")  # Total/Quoted Premium
   await inputs.nth(5).fill("400.00")   # Fees
   await inputs.nth(6).fill("223.46")   # Taxes
   ```
6. **Auto-Save**: EZLynx auto-saves when blurring fields or clicking off (`Auto saved all changes.`).

---

## 4. Staging & Uploading Documents into the Submission Folder

EZLynx requires that all quote-related documents reside in the client Document Library under the specific folder created for that submission:
`Submission Folder for {Submission Title}` (e.g., `Submission Folder for CA, US DOT# 3824641`).

### Upload Workflow
1. In the **Manage submission** view, click **`Manage documents`** (`button:has-text("Manage documents")`).
2. In the folder hierarchy tree modal:
   - Locate and click the row for **`Submission Folder for {submission_name}`**.
3. Click the **`Upload documents`** button inside the modal (`button:has-text("Upload documents")`).
4. Stage the files into the dropzone file input:
   ```python
   file_input = page.locator('input[type="file"]')
   await file_input.set_input_files([
       "/path/to/Carrier_Quote_Proposal.pdf",
       "/path/to/Carrier_Application.pdf"
   ])
   ```
5. Click **`Upload`** (`button:has-text("Upload")`) and wait for the upload progress bar to complete.
6. Verify that the badge count next to the target folder increments (e.g., `(2)`), and ensure the uploaded files have their checkboxes checked.
7. Click **`Save`** to link the documents to the carrier submission card.

---

## 5. Activity Discussion Documentation & Audit Trail

Every quote entered into Submission Center must be accompanied by an audit note in the client's **Activity** tab.

### Navigation & Selectors
- URL: `https://app.ezlynx.com/web/account/{applicant_id}/activity`
- Open Note Panel: `button:has(mat-icon:has-text("note_add")).first`
- Discussion Search Autocomplete: `#txtDiscussionTitle`
- Note Body Textarea: `#txtNote`
- Policy / Folder Association: `#btnAssociatetoAPolicy`
- Save Button: `#btnSaveNote`

### Audit Note Template
```text
{Carrier Name} / {Underwriter/MGA Name}
{Line of Business} Quote Received & Processed

- Policy Type: {Commercial Auto / General Liability / BOP / Cargo}
- MGA / Underwriter: {Diesel Insurance Solutions / RT Specialty / etc.}
- Issuing Carrier: {Carrier Name}
- Quote Number: {Quote #}
- Term / Effective Dates: {MM/DD/YYYY} – {MM/DD/YYYY} ({Term Length})
- Scheduled Units / Locations: {VINs, Addresses, or Description}
- Coverages & Limits:
  * {Coverage 1}: ${Limit} (${Deductible} Deductible) - ${Sub-premium}
  * {Coverage 2}: ${Limit} (${Deductible} Deductible) - ${Sub-premium}
- Total Annual Premium: ${Total}
  * Base Premium: ${Base}
  * Policy / Inspection Fees: ${Fees}
  * Surplus Lines Taxes: ${Taxes}

EZLynx Actions Completed:
1. Added Carrier "{Carrier Name}" to Submission Center (Submission #{ID} - {Submission Name}).
2. Set Submission & {LOB} Status to "Quoted".
3. Populated Premium (${Total}), Fees (${Fees}), and Taxes (${Taxes}) into Submission Center.
4. Uploaded Carrier Quote Proposal ({Quote #}) and Application PDF into EZLynx Documents under "Submission Folder for {Submission Name}".

Robie was here
```

---

## 6. Automation Reference Snippet

```python
import asyncio
from playwright.async_api import async_playwright

async def add_carrier_and_quote(applicant_id: str, sub_id: str, carrier_name: str, premium: str, fees: str, taxes: str, quote_pdf: str, app_pdf: str):
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        
        # 1. Navigate to Carrier Submissions
        url = f"https://app.ezlynx.com/web/account/{applicant_id}/submissions-version-2/{sub_id}/carrier-submissions"
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        
        # 2. Add Carrier
        await page.locator("button:has-text("Add carrier")").click()
        await page.wait_for_timeout(1000)
        
        # Toggle Non-Preferred if needed
        np_box = page.locator("mat-checkbox:has-text("Show Non-Preferred Carriers")")
        if await np_box.is_visible():
            await np_box.click()
            await page.wait_for_timeout(1000)
            
        search_input = page.locator("input").nth(1)
        await search_input.fill(carrier_name)
        await page.wait_for_timeout(1500)
        
        # Select matching carrier and LOB
        await page.locator(f"tr:has-text("{carrier_name}") mat-checkbox").first.click()
        await page.locator("button:has-text("Save & continue")").click()
        await page.wait_for_timeout(3000)
        
        # 3. Manage Submission & Enter Premium
        card = page.locator(f"div.carrier-card:has-text("{carrier_name}")")
        await card.locator("button:has-text("Manage submission")").click()
        await page.wait_for_timeout(2000)
        
        # Set overall status to Quoted
        await page.locator("mat-select").first.click()
        await page.locator("mat-option:has-text("Quoted")").click()
        await page.wait_for_timeout(1000)
        
        # Expand LOB and set Quoted
        await page.locator("mat-icon:has-text("keyboard_arrow_down")").first.click()
        await page.wait_for_timeout(1000)
        await page.locator("mat-select").nth(3).click()
        await page.locator("mat-option:has-text("Quoted")").click()
        await page.wait_for_timeout(1000)
        
        # Enter financials
        inputs = page.locator("input")
        await inputs.nth(3).fill(premium)
        await inputs.nth(5).fill(fees)
        await inputs.nth(6).fill(taxes)
        await page.locator("body").click()
        await page.wait_for_timeout(2000)
        
        # 4. Upload Documents to Submission Folder
        await page.locator("button:has-text("Manage documents")").click()
        await page.wait_for_timeout(2000)
        
        folder = page.locator("mat-tree-node:has-text("Submission Folder")").first
        await folder.click()
        await page.wait_for_timeout(1000)
        
        await page.locator("button:has-text("Upload documents")").click()
        await page.wait_for_timeout(1000)
        
        file_input = page.locator("input[type="file"]")
        await file_input.set_input_files([quote_pdf, app_pdf])
        await page.locator("button:has-text("Upload")").click()
        await page.wait_for_timeout(4000)
        
        await page.locator("button:has-text("Save")").click()
        await page.wait_for_timeout(2000)
        
        await page.close()
