# Garage & Dealers Policy SOP

## Line of Business Overview
Garage and Dealers policies provide hybrid liability, physical damage, and bailee (garagekeepers) protection for automotive-related risks: repair shops, service stations, towing operators, mobile mechanics, body shops, and auto dealerships (franchised and non-franchised).

---

## 1. Primary Change Types & Required Data Elements

### A. Operations Classification (Service vs. Dealership)
- **Auto Service Operations / Trailer Sales (Non-Dealer)**:
  - Repair Shop, Service Station, Storage/Public Parking, Mobile Repair Shop.
  - Exposure Base: Annual Gross Receipts / Sales and Annual Estimated Payroll/Remuneration.
  - % of Operations Off-Premises (e.g., 100% for mobile mechanics).
- **Auto Dealers (Franchised vs. Non-Franchised)**:
  - Inventory breakdown (% Cars, Truck-Tractors, Motorcycles, RVs).
  - Number of Dealer Plates, Transporter Plates, and Hoists.
  - Employee counts: Class I Regular Operators, Class I All Others, Class II Non-Employees under age 25.

### B. Covered Auto Symbols
- **Symbol 21**: Any Auto.
- **Symbol 27**: Specifically Described Autos.
- **Symbol 28**: Hired Autos Only.
- **Symbol 29**: Non-Owned Autos Used in Garage Business.
- **Symbol 30**: Autos Left with Insured for Service, Repair, or Storage (Garagekeepers).
- **Symbol 31**: Dealers Autos (Physical Damage inventory).

### C. Garagekeepers Liability (Bailee Coverage)
- **Basis of Coverage**:
  - **Legal Liability**: Carrier pays only if the shop was legally negligent.
  - **Direct Primary**: Carrier pays regardless of fault (e.g. storm, vandalism, theft while in care, custody, and control).
  - **Direct Excess**: Carrier pays in excess of vehicle owner's personal auto policy.
- **Limits & Deductibles**:
  - Total Location Limit (e.g., $250,000).
  - Per Auto Deductible (e.g., $1,000) and Max Loss Deductible (e.g., $5,000).

---

## 2. Three-Way Verification Matrix (Garage & Dealers)

| Element | Client Request / Ticket | Carrier Endorsement / Dec | EZLynx Policy Record |
| :--- | :--- | :--- | :--- |
| **Operations Type** | Service shop vs Dealership | Dec Page classification | Operations tab in EZLynx |
| **Liability Limits** | Auto / Other Than Auto limits | Garage Liability schedule | Coverages tab |
| **Covered Auto Symbols**| Symbol 21, 27, 29 | Schedule of Covered Autos | Covered Auto Symbols dropdown |
| **Garagekeepers Basis**| Legal Liability / Direct Primary | Garagekeepers Endorsement | Garagekeepers Basis dropdown |
| **Garagekeepers Limit**| Total location lot limit | Limit of Insurance schedule | Limit - Location field |
| **Drivers** | New mechanic or driver | Driver schedule on endorsement | Drivers tab (mandatory >= 1) |
| **Dealer Plates** | Added plate numbers | Dealer plate schedule | Dealer/Rep Plates field |
| **Effective Date** | Requested endorsement date | Endorsement effective date | Transaction effective date |
| **Premium Change** | Quoted AP / RP | Carrier invoice / endorsement | EZLynx Transaction premium |

---

## 3. Video SOP: Step-by-Step EZLynx Policy Entry & Endorsement Rules

> [!NOTE]
> **Source Walkthrough**: [Garage & Dealers Loom Walkthrough](https://www.loom.com/share/2e53898e240a4036a0c8c63c2b796a9e) demonstrated on *Autonomy Service LLC* (`PACI5675561PC`, AmWINS MGA).

### Step 1: Named Insured & Mandatory Location Building
- Line of Business: **Garage and Dealers Policy**.
- Ensure shop premises or home office is entered under Locations with an active building record (`Building # 1`).

### Step 2: Operations Tab Setup
- **Determine Operation Type**:
  - **Auto Service Operations**: Check *Repair Shop*, *Service Station*, or *Other (Define): Mobile repair shop*.
  - For mobile service: Key `% of off-premises installation, service or repair work` = `100%`.
- **Non-Dealer Premises & Operations**:
  - Key Estimated Annual Remuneration (Payroll, e.g., `$100,000.00`) and number of employees.
  - Service or Repair Shops: Key Annual Gross Sales (e.g., `$100,000.00`).
- **Dealer Operations (if applicable)**:
  - If a car lot, enter % inventory mix and number of Class I/II employees. If service-only, leave dealer fields empty.

### Step 3: Coverages & Covered Auto Symbols
- **Applies to**: Select **Automobile & Premises Operations** (or Automobile Only / Premises Operations Only).
- **Covered Auto Symbols**: Select Symbol `29` (Non-Owned Autos) or Symbol `21` (Any Auto).
- **Limits Schedule**:
  - Auto Only: `$1,000,000 Each Accident / $2,000,000 Aggregate`.
  - Other Than Auto: `$1,000,000 Each Accident / $2,000,000 Aggregate`.
  - Products-Completed Ops: `$2,000,000`, Personal & Advertising Injury: `$1,000,000`.

### Step 4: Garagekeepers Coverage Entry
- Basis: Select **Legal Liability** (or Direct Primary / Direct Excess).
- Symbol: Select Symbol `30`.
- Limits: Key Total Location Limit (e.g., `$250,000`), Per Auto Deductible (e.g., `$1,000`), and Max Per Loss (e.g., `$5,000`).

### Step 5: Drivers (MANDATORY REQUIREMENT)
- **Every Garage policy MUST have at least one active driver listed** (e.g., Jordan Boland, DOB, Driver License # and State).
- If additional mechanics or tow drivers are hired, verify MVR eligibility before adding.

---

## 4. Discussion & Closing Protocol
- **Activity Note**: Always note: `"Garage & Dealers Endorsement verified: [Garagekeepers adjusted / Driver added / Operations updated]. Symbols: [29, 30]. GK Limit: [$XXX,XXX Legal Liability]. Driver: [Name]. Premium AP/RP: [$XX]. Robie was here"`
