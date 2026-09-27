# WOW Customer Service - Monthly Automation Design
# Target: Running by October 1, 2026

## Monthly Schedule

### 24th - Early Pull & Staff Outreach
**Automated:**
- Pull EZLynx report (all WOW labels for current month)
- Pull Google Reviews (Business Profile + Chat)
- Cross-reference reviews with EZLynx contacts

**Staff email (via distribution list):**
- List ambiguous reviews needing attribution
- Ask: "Who does this belong to? Anything missing?"
- Clarification only, not "we caught you"

### 27th - Second Pass
**Automated:**
- Re-pull EZLynx (catch late entries)
- Re-pull Google Reviews

**Staff email (only if no response):**
- Follow up on unanswered 24th requests
- One follow-up only

### 1st - Final Reconciliation
**Automated:**
- Final data pull
- Run verification rules per label
- Generate payout report

**No third outreach.** Make decisions based on responses received.

## Per-Label Automation

### Google Reviews
- **Source:** Google Business Profile API + Chat (Zapier)
- **Logic:** Direct name match = auto-credit. Unnamed = flag for staff.
- **Output:** $10 per verified review by employee

### Autopay Setup
- **Source:** Carrier portals (Progressive, Travelers, etc.)
- **Logic:** 
  - Auto-reject if note contains "paid in full" or "mortgage billed"
  - Portal check for EFT enrollment
  - Flag for manual review if portal blocked
- **Output:** $10 per verified enrollment

### New Customer CSR
- **Source:** EZLynx new business reports + carrier downloads
- **Logic:**
  - Auto-reject if note contains "app sent", "shell", "waiting on download"
  - Verify policy issued in EZLynx
  - Verify carrier download received
- **Output:** Verified new customers by CSR

### Cross Sell
- **Source:** EZLynx policy data
- **Logic:**
  - Verify client had prior policy
  - Verify new line issued
  - Auto-reject BOR/renewals
- **Output:** Verified cross-sells by employee

### Coverage Enhancement
- **Source:** EZLynx endorsements
- **Logic:**
  - Verify enhancement on existing policy
  - Check against three-enhancement rule
  - Auto-reject new policies (mislabeled)
- **Output:** Verified enhancements by employee

### Referral Won
- **Source:** EZLynx referral source field + notes
- **Logic:**
  - Require referral source documented
  - Auto-reject if no source found
- **Output:** Verified referrals by employee

### All Star Call
- **Source:** Magellan/Sonant call analytics
- **Logic:**
  - Match EZLynx notes to call records
  - Check sentiment score threshold
  - Auto-reject if no call record
- **Output:** Verified All Star calls by employee

## Data Flow
```
EZLynx Report → WOW_Raw_Import (AppSheet) → Verification Engine → Payout Report
Google Reviews → Review Matcher → Verification Engine → Payout Report
Carrier Portals → Portal Checks → Verification Engine → Payout Report
Magellan → Call Matcher → Verification Engine → Payout Report
```

## Outputs
1. **Employee payout summary** (by label, by employee)
2. **Rejected items report** (with reasons)
3. **Pending verification** (blocked items)
4. **AppSheet upload** (WOW_Raw_Import for the month)

## Manual Intervention Points
- Carrier portal blocks (credentials, bot detection)
- Ambiguous Google Reviews (staff clarification)
- Magellan sentiment edge cases
- Alejandro reviews rejections before payout
