# Commercial Inland Marine Policy Change & Entry SOP

## Line of Business Overview
Inland Marine covers mobile, high-value, or specialized property in transit, on job sites, or off-premises. The most common commercial application is the **Contractor's Equipment Floater** covering heavy machinery (woodchippers, excavators, skid steers, pavers), followed by Installation Floaters, Motor Truck Cargo, and Electronic Data Processing (EDP).

---

## 1. Primary Change Types & Required Data Elements

### A. Equipment Additions / Deletions (Scheduled Equipment)
- **Equipment Schedule Details**:
  - Item description (e.g., Woodchipper, Mini-Excavator).
  - Year, Manufacturer (Make), Model (e.g., 2020 Vermeer SC382).
  - Serial Number / PIN (mandatory for theft tracking and identification).
  - Stated Valuation / Amount of Insurance (e.g., $35,000).
- **Valuation Rule (The 5-Year Age Rule)**:
  - Equipment **$\le$ 5 years old** from date of manufacture: Valued at **Replacement Cost (RC)**, typically with 100% Coinsurance.
  - Equipment **> 5 years old**: Reverts to **Actual Cash Value (ACV)** unless an explicit agreed value endorsement is attached.
- **Equipment Storage Locations**:
  - Address where machinery is garaged when not on job sites (e.g., primary shop/yard).
  - Maximum value stored inside vs. maximum value stored outside.

### B. Unscheduled Equipment (Miscellaneous Tools Floater)
- Blanket coverage for small hand tools and light equipment not individually scheduled:
  - Max item limit (typically `$1,000` per tool).
  - Blanket amount of insurance (e.g., `$25,000`).
  - Coinsurance percentage (typically `80%`).

### C. Financing / Loss Payees
- Lenders financing machinery (e.g., Vermeer Financial, John Deere Financial, Ally) must be added as **Loss Payee** linked specifically to the financed unit.

---

## 2. Three-Way Verification Matrix (Inland Marine)

| Element | Client Request / Ticket | Carrier Endorsement / Dec | EZLynx Policy Record |
| :--- | :--- | :--- | :--- |
| **Equipment Description** | Year, Make, Model | Declarations / Scheduled Item Schedule | Equipment Floater Schedule |
| **Serial / PIN Number** | Exact VIN or Serial # | Policy Equipment Schedule | ID / Serial # Field in EZLynx |
| **Insured Valuation** | Requested equipment value | Limit of Insurance on dec | Amount of Insurance / Valuation |
| **Valuation Basis** | RC (if $\le$ 5 yrs) or ACV | Valuation clause in policy forms | RC vs ACV dropdown |
| **Loss Payee** | Financing agreement / Bank | Loss Payable endorsement | Additional Interest tab (Loss Payee) |
| **Deductible** | Quoted deductible | Carrier schedule deductible | Policy-level or item deductible |
| **Effective Date** | Bill of sale / acquisition date | Endorsement effective date | Transaction effective date |
| **Premium Change** | Quoted AP / RP | Carrier invoice / endorsement premium | EZLynx Transaction premium |

---

## 3. Video SOP: Step-by-Step EZLynx Policy Entry & Endorsement Rules

> [!NOTE]
> **Source Walkthrough**: [Inland Marine Loom Walkthrough](https://www.loom.com/share/421ff2e1099b4073b346d8a8e8fde402) demonstrated on *The Tree Guy Service LLC* (`MX9307982648590`, Allianz Global Corporate & Specialty / Gridiron Insurance Underwriters).

### Step 1: Named Insured & Mandatory Building Record
- Verify legal entity name and mailing address against the Inland Marine declarations.
- **Mandatory Location Building**: Key the equipment storage yard under Locations, and ensure an active building record (`Building # 1`) is added.

### Step 2: Class of Business Selection
- In EZLynx, check **Contractor's Equipment (Equipment Floater)**.
- If the policy covers goods in transit, check *Motor Truck Cargo*; if building materials in transit to installation, check *Installation Floater*.

### Step 3: Equipment Storage Parameters
- Under the **Equipment Floater** tab:
  - Key the Storage Location (Location 1, Building 1).
  - Enter **\$ Max Value Inside** and **\$ Max Value Outside** (e.g., `$35,000.00`).

### Step 4: Scheduled Equipment Entry
- Click **Add Scheduled Equipment Item**:
  - Item Description: e.g., `Woodchipper`
  - Year: `2020`
  - Make: `Vermeer`
  - Model: `SC382`
  - Serial Number: `100G79418593`
  - Amount of Insurance: `$35,000.00`
  - Valuation: Select **RC** (Replacement Cost) if $\le$ 5 years old; select **ACV** if older.
  - Coinsurance: `100%`.
  - Cause of Loss: Policy Level (`Yes`), Deductible: e.g., `$2,500`.

### Step 5: Unscheduled Small Tools Floater
- If dec page carries miscellaneous tool coverage:
  - Description: `Small Tools`
  - Maximum Item: `$1,000.00`
  - Amount of Insurance: `$25,000.00`
  - Coinsurance: `80%`.

### Step 6: Critical E&O Checks
- **Theft Coverage Check**: Review the policy forms to ensure theft is covered and not restricted by warranty exclusions (e.g., locked compound, hitch lock warranties).
- **Lienholder / Loss Payee Linking**: If financed, add the lender under Additional Interests with interest type = **Loss Payee** linked to the specific equipment unit number.

---

## 4. Discussion & Closing Protocol
- **Activity Note**: Always note: `"Commercial Inland Marine Endorsement verified: [Equipment added/deleted: Year Make Model, Serial #, Value $XX,XXX]. Valuation: [RC / ACV]. Loss Payee: [Bank name]. Premium AP/RP: [$XX]. Information Page attached. Robie was here"`
