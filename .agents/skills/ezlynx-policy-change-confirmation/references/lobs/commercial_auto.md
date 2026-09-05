# Commercial Auto (CA) Policy Change & Endorsement SOP

## Line of Business Overview
Commercial Automobile policies cover business-owned vehicles, hired/non-owned autos, and associated drivers. Policy changes are predominantly vehicle additions/deletions, driver additions/exclusions, limit increases, or garaging address adjustments.

---

## 1. Primary Change Types & Required Data Elements

### A. Vehicle Additions
- **VIN (Vehicle Identification Number)**: Must be 17 characters (1981+). Validate VIN format and decode year/make/model.
- **Year, Make, Model, Body Type, Gross Vehicle Weight (GVW)**.
- **Garaging Zip Code & Physical Address**: Crucial for territorial rating.
- **Coverage & Deductibles**:
  - Comprehensive / Other Than Collision deductible ($500, $1,000, etc.).
  - Collision deductible ($500, $1,000, etc.).
  - Stated Value / Stated Amount vs. Actual Cash Value (ACV).
  - Hired / Non-Owned Auto Liability (HNOA) if applicable.
- **Lienholder / Loss Payee / Additional Insured - Lessor**: Name and full mailing address.
- **Auto ID Card Issuance**: Must be issued immediately upon vehicle binding.

### B. Vehicle Deletions
- **VIN & Unit Number**: Must exactly match the carrier's active schedule of vehicles.
- **Effective Date of Removal**: Ensure date matches client request or bill of sale.
- **Lienholder Notification**: Note if lender requires certificate of cancellation or release of interest.

### C. Driver Additions / Exclusions
- **Full Legal Name** (First, Middle, Last).
- **Date of Birth (DOB)**.
- **Driver's License Number & State of Issuance**.
- **Motor Vehicle Record (MVR) Status**: Carrier will run MVR; flag major violations (DUI, reckless driving, suspension).
- **Excluded Driver Endorsements**: If a driver is excluded, carrier requires signed **Named Driver Exclusion Form** before endorsement issue.

---

## 2. Three-Way Verification Matrix (CA)

| Element | Client Request / Ticket | Carrier Endorsement / Dec | EZLynx Policy Record |
| :--- | :--- | :--- | :--- |
| **VIN** | 17-digit string from registration | Schedule of Covered Autos | Vehicle schedule in Policy Details |
| **Year/Make/Model** | Registration / Bill of Sale | Schedule of Covered Autos | Policy vehicle list |
| **Garaging Address** | Insured's yard or premises | Garaging Schedule | Location schedule |
| **Comprehensive/Collision**| Requested deductibles | Vehicle coverage block | Vehicle coverage fields |
| **Effective Date** | Requested change date | Endorsement effective date | Transaction effective date |
| **Premium Change** | Quoted AP / RP or $0 | Endorsement premium ($ AP/RP) | EZLynx Transaction premium |

---

## 3. Major Carrier Specifics

### Progressive Commercial
- **Portal URL**: ForAgentsOnly (FAO)
- **Direct Processing**: Most vehicle & driver changes can be endorsed directly in FAO with instant updated ID cards and endorsement decs.
- **Downloads**: Progressive typically downloads endorsements via IVANS overnight. If immediate dec is needed, download from FAO Policy Documents.

### Selective Insurance
- **Portal**: Selective eSelect
- **Policy Prefix**: Typically `S` (e.g., `S 2472200`).
- **Processing**: Direct online endorsement or email to Commercial Underwriter. Vehicle endorsements trigger overnight eDocs download.

### Travelers / National General / Guard
- Verify driver eligibility and vehicle class codes (service, commercial, retail).

---

## 4. Discussion & Closing Protocol
- **Activity Note**: Always note: `"Comm Auto Endorsement verified: Added/Removed [Year Make Model VIN: XXXX]. Deductibles: [Comp/Coll]. Premium AP/RP: [$XX]. Auto ID card issued to client. EZLynx vehicle schedule updated. Robie was here"`
- **Client Email**: If Automation Center does not auto-dispatch, or for manual policies, verify client received Auto ID Card.
