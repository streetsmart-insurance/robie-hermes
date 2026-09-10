# EZLynx & Sales Center Field Mappings for Robie Lead Engine

## 1. Applicant & Lead Field Mapping

| EZLynx Field (Portal/Classic API) | Robie Model (`ApplicantLead`) | Purpose / Usage |
| :--- | :--- | :--- |
| `applicantID` / `Applicant.ApplicantId` | `applicant_id` | Unique primary key across state machines and notes. |
| `FirstName` / `Applicant.FirstName` | `first_name` | Prospect first name. |
| `LastName` / `Applicant.LastName` | `last_name` | Prospect last name. |
| `CellPhone` or `Phone` | `phone` | Standardized to E.164 (`+1XXXXXXXXXX`) for Bland AI calls and SMS. |
| `Email` / `Applicant.Email` | `email` | Outreach recipient for multi-touch follow-up emails. |
| `Applicant.Assignment.AssignedTo` | `assigned_producer` | Verified against `pilot_config.json` (`Jake Ferrara`). |
| `LeadSource` / `Opportunity.LeadSource` | `lead_source` | Verified against approved sources list. |
| `ApplicantType` / `Status` | `client_status` | Must be `ProspectLead`. Halted if `ActiveClient`. |

---

## 2. Opportunity Field Mapping

| EZLynx Field (`GetOpportunitiesForApplicant`) | Robie Model (`Opportunity`) | Validation / Cadence Logic |
| :--- | :--- | :--- |
| `opportunityId` / `id` | `opportunity_id` | Ties touch attempts to specific opportunity card. |
| `lineOfBusiness` / `lob` | `line_of_business` | Injected into spoken voice script and email subjects. |
| `status` / `stage` | `stage` | `New`, `Unreached`, `Quoted`, `Won`, `Lost`, `Dead`. Won/Lost halts cadence immediately. |
| `producerName` / `assignedTo` | `producer_name` | Matches assigned producer. |
| `policyExpirationDate` | `expiration_date` | Used for X-Date cadence trigger calculation (T-45, T-30, T-14). |

---

## 3. Completed Quote Field Mapping

| EZLynx Field (`/Quote/GetCompletedQuote/{id}`) | Robie Model (`QuoteSummary`) | Grounding Invariants |
| :--- | :--- | :--- |
| `quoteId` | `quote_id` | Reference ID for proposal discussions. |
| `carrierName` | `carrier_name` | Carrier name (e.g. Progressive, Travelers, Plymouth Rock). |
| `quotedPremium` / `annualPremium` | `quoted_premium` | Injected into summary notes and proposal emails. |
| `quoteExpirationDate` | `verified_expiration_date` | **Mandatory Grounding**: Spoken scripts may ONLY cite expiration if this date exists in EZLynx. |
| `rateGuaranteeDate` | `verified_rate_guarantee_date` | **Mandatory Grounding**: May ONLY claim price lock if verified. |

---

## 4. EZLynx Discussion Note Specifications

Every note posted back to EZLynx must include:
1. Touch attempt timestamp and channel (`VOICE`, `SMS`, `EMAIL`).
2. Bland AI Call ID and recording link (if voice call).
3. Call disposition or stop reason.
4. Mandatory signature on a new line:
   ```text
   ROBIE was here
   ```
