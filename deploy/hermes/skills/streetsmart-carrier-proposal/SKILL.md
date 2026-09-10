---
name: streetsmart-carrier-proposal
description: Turns a raw commercial trucking/auto insurance carrier quote (GEICO, Progressive, Cover Whale, or any other carrier's PDF) into a clean, StreetSmart-branded client proposal — stripping out the carrier's name and letterhead, keeping only coverages/limits (no per-line premiums), listing all quoted drivers and the radius of operation, and presenting premium/payment terms with any user-authorized broker amount labeled only as "Taxes and fees." Use this whenever Jake uploads or mentions a carrier quote, proposal, or "quote PDF" and wants it turned into a proposal to send a client, even if he doesn't name the skill directly. Also use if he asks to "make a StreetSmart proposal" or "clean up this quote."
job_type: carrier.proposal
production_ready: true
---

# StreetSmart Carrier Proposal Generator

## What this does

Jake (StreetSmart Insurance) gets commercial auto/trucking quotes from many different
carriers, and every carrier formats its quote PDF completely differently. He never wants
to hand a client the raw carrier document — he wants his own branded StreetSmart proposal
that shows only: the coverages and limits (no per-line premiums), all quoted drivers, the
radius of operation, the total annual premium, the required initial (down) payment, the
payment plan, and the monthly installment.

This skill reads whatever carrier PDF Jake gives you, pulls out the numbers, and renders
them into the fixed StreetSmart template via `scripts/generate_proposal.js`. The template's
branding, marketing pages, and partner pages are already built — you are only ever supplying
the customer-specific data.

## Step-by-step

1. **Read the carrier quote PDF(s) the user uploads.** Carriers vary hugely in layout
   (tables, nested sections, multi-page schedules) — don't expect a fixed structure. Read
   it like a person would and find the pieces you need. If more than one PDF is uploaded
   for the same client (e.g. a quote plus an endorsement), use them together.

2. **Extract the named-insured / customer info:**
   - `business_name`, `address`, `phone`, `email` — from the "Insured"/"Prepared For"/policyholder section.
   - `business_type` — the described operation (e.g. "Refrigerated Goods Hauling/Trucking").
   - `usdot_number` — if shown; leave blank if not present in the quote.
   - `policy_period` — the effective-to-expiration dates.
   - `rated_drivers` — every driver listed in the quote, preserving the name and any shown
     date of birth, points, or additional information. Never omit a listed driver.
   - `radius_of_operation` — the quoted operating radius. If multiple scheduled vehicles
     show different radii, list each distinct radius with the applicable vehicle.

3. **Extract coverages, grouped and limit-only.** Group by section as the carrier
   presents them (e.g. "Commercial Automobile Liability", "Automobile Physical Damage",
   "Motor Truck Cargo", "Truckers General Liability", per-vehicle physical damage, etc).
   For each line, capture just the coverage name and its limit/deductible —
   **never carry over the per-line premium dollar amount**, and never mention the carrier's
   name anywhere in the extracted data. The whole point is a clean, carrier-agnostic
   coverage summary.

4. **Figure out the premium and payment terms:**
   - `total_annual_premium` — the fully-loaded total (including all carrier taxes/fees).
   - If the carrier offers **one** payment plan, use it.
   - If the carrier offers **multiple** payment plans (common with GEICO/Progressive-style
     quotes), default to the plan with the **lowest down payment / most installments**
     (e.g. GEICO's "Monthly 11 Pay" column, Progressive's "11 Payments, 16.67% Down" EFT
     row). Only ask Jake which plan to use if it's genuinely ambiguous which one qualifies.
   - `required_initial_payment` — the carrier's stated down payment / initial payment for
     the selected plan, before any separately authorized taxes-and-fees amount (see next step).
   - `payment_plan_label` — a short human label for the plan you picked, e.g.
     `"11 Payments, 16.67% Down"` or `"10 Monthly Payments"`.
   - `monthly_installment` — the recurring payment amount for the selected plan.

5. **Apply only an explicitly authorized additional amount.** Never add a default broker,
   agency, service, or StreetSmart fee. If Jake specifies an additional broker amount for
   a proposal, put that amount in `taxes_and_fees`; the generator folds it into the initial
   payment. On the client proposal, describe it **only** as `Taxes and fees` — never as an
   agency fee, service fee, broker fee, StreetSmart fee, or similar wording. Do not expose
   a breakdown unless Jake explicitly asks for one. If Jake says not to apply fees, omit
   `taxes_and_fees` or set it to `$0.00`, and the initial payment must equal the carrier's
   stated initial payment.

6. **Write a `data.json`** matching this schema (see the full example in the header
   comment of `scripts/generate_proposal.js`):

   ```json
   {
     "business_name": "...",
     "address": "...",
     "phone": "...",
     "email": "...",
     "business_type": "...",
     "usdot_number": "...",
    "policy_period": "MM/DD/YYYY to MM/DD/YYYY",
    "rated_drivers": [
      { "name": "...", "date_of_birth": "MM/DD/YYYY", "points": "0", "additional_information": "" }
    ],
    "radius_of_operation": "500 miles",
     "coverage_groups": [
       { "title": "Commercial Automobile Liability",
         "items": [ { "name": "Bodily Injury / Property Damage", "limit": "$1,000,000 CSL" } ] }
     ],
     "total_annual_premium": "$X,XXX.XX",
     "required_initial_payment": "$X,XXX.XX",
    "taxes_and_fees": "$0.00",
    "required_initial_payment_includes_taxes_and_fees": false,
     "payment_plan_label": "...",
     "monthly_installment": "$X,XXX.XX/month"
   }
   ```

7. **Use the deterministic renderer.** Run the bundled wrapper and pass an output prefix
   without a filename extension:
   ```bash
   bash scripts/render_proposal.sh data.json "/absolute/output/path/<Client Name>_StreetSmart_Proposal"
   ```
   The wrapper installs the pinned Node dependency when needed, runs
   `generate_proposal.js`, converts the DOCX to PDF with LibreOffice, and performs basic
   PDF integrity checks.

   **Rendering contract:**
   - Always use `scripts/generate_proposal.js` through `scripts/render_proposal.sh`.
   - Never rebuild the proposal as HTML or CSS.
   - Never use Chrome, Chromium, Playwright, Puppeteer, `wkhtmltopdf`, or browser Print to
     PDF as a fallback. Browser printing can crop the logo and photo, remove Word layout,
     merge intended pages, and expose local file paths, timestamps, and page URLs.
   - If `soffice`/LibreOffice is unavailable, stop and report that dependency. Install it
     only when the user or system administrator authorizes installation; do not improvise
     another document format or renderer.

8. **Render and inspect every page.** Follow the documents/PDF visual-verification
   workflow. At minimum, confirm all of the following before delivery:
   - The StreetSmart logo and Jake's photo are fully visible and proportional.
   - Intended page breaks are preserved; marketing, coverage, payment, and disclaimer
     sections do not collapse into one continuous browser page.
   - No browser date/time, `file:///` path, source URL, or browser page counter appears.
   - Tables are readable, no text is clipped or overlapping, and all pages use US Letter.
   - The PDF title does not end in `.html` and its producer is not a browser print engine.

9. **Deliver both the .docx and the .pdf** as user-facing output files. Briefly note which
   payment plan you picked and why, since that's the one
   judgment call baked into an otherwise mechanical process.

## What NOT to touch

The template already contains, fully built and Jake-approved — don't regenerate or
improvise these from scratch, don't re-derive them from the carrier PDF:

- Cover page layout, StreetSmart logo/branding, and the diagonal stripe header on every page.
- The "Your Insurance Quote" page (Jake's photo linking to
  `https://www.streetsmart.insurance/quotevids/your-insurance-quote/`, plus a large
  highlighted "CLICK HERE!" button).
- The "Why do business with StreetSmart?" page, including the highlighted lead-in phrases.
- The "Our Partners" pages (CNS Compliance Navigation Specialists, and RTS Financial for
  factoring) — these are static marketing content, not derived from any carrier quote.
- Binding Requirements (shown in bold red so they can't be missed) and the Legal Disclaimer.

If Jake asks to change any of this static content (wording, images, new partners, sizing),
edit `scripts/generate_proposal.js` directly rather than working around it in `data.json` —
`data.json` should only ever carry customer-specific facts.

## Assets bundled with this skill

`scripts/assets/` contains the StreetSmart logo, stripe graphic, Jake's photo, and the CNS
logo — all already wired into `generate_proposal.js`. If Jake provides a real RTS Financial
logo file (a proper file attachment, not a pasted chat image — pasted images aren't
accessible on disk), save it as `scripts/assets/rts_logo.png` and it will automatically
replace the current text-only "RTS FINANCIAL" placeholder — no code change needed, the
script already checks for that file.

## A note on carrier variety

Don't build a rigid per-carrier parser — new carriers will show up that don't match any
pattern seen before. Read each PDF on its own terms the way a knowledgeable insurance
assistant would, using the extraction guidance above as a checklist of what to look for
rather than a fixed template to pattern-match against.
