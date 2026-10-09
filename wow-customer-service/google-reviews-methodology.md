# Google Reviews Verification Methodology

## Overview
Verify every Google Review WOW label ($10) against actual posted reviews. A label is a claim, not proof.

## Sources (both required)
1. **😀 StreetSmart Google Chat space** — Zapier auto-posts "New Google Review" messages
2. **Google Business Profile** (business.google.com/reviews) — read directly, signed in

**Why both:** Zapier misses posts. The Business Profile is the ground truth. Chat alone is insufficient.

## Process

### 1. Pull from Chat space
- Search the 😀 StreetSmart space for "New Google Review" with date operators (after:YYYY/M/D before:YYYY/M/D)
- Record: reviewer name, post date/time, star rating, full review text, employee mentioned

### 2. Pull from Business Profile
- Navigate to business.google.com/reviews, signed in as Carlo Ferrara
- Reviews sorted newest-first; identify the target month by relative dates
- For each review: reviewer name, relative date, star count (visual), full text (expand truncated), employee mentioned
- "Rating only" counts — star-only reviews are valid per Carlo 2026-09-27

### 3. Reconcile
- Chat and Profile lists will differ. Combine into a master list.
- Note which source each review came from.

### 4. Match reviewer to EZLynx
- Search EZLynx for the reviewer name
- Account for: alternate spellings, nicknames, business names, spouse names
- Record: applicant ID, account status, assigned producer/CSR

### 5. Determine employee attribution
**Rule (Carlo 2026-09-27): If the review says someone's name, that person gets the credit.**
- Review names an employee → that employee gets $10
- Review names no one → match via EZLynx (assigned producer/CSR) or ask the team
- Exclude: Jake-attributed and Carlo-attributed reviews (Carlo's rule)

### 6. Month assignment
- Count the review in the month it was publicly posted, not the EZLynx note date
- Do not double-count edited reviews without proving two separate review records

### 7. Verify labels
- For each labeled item in the WOW report: find the matching review
- If the note doesn't describe a review → REJECTED
- If no matching review exists → UNVERIFIED (do not call "bogus" — usernames can differ)

## Team Notification Process (24th/27th/1st)

### 24th — Early pull
- After verification, email employees directly for unlabeled reviews: "Hey, we caught this one for you. This is yours."
- Email the staff distribution list (streetsmart@streetsmart.insurance) about ambiguous reviews: "Does anyone know whose client this is?"
- Replies go to robie@streetsmart.insurance

### 27th — Second pass
- Check for team responses
- Follow up ONLY on unanswered items (no duplicates)
- Catch new reviews posted since the 24th

### 1st — Final reconciliation
- Last check for responses
- No new outreach — unresolved items go to Carlo for a call
- Produce payroll-ready topsheet

## Anti-duplication
- Track every notification in working files
- Never email the same person about the same review twice
- The 1st is for decisions, not new emails
