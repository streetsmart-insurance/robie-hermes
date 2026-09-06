# Carrier & MGA Phone Directory & IVR Navigation Matrix

This directory provides the authoritative phone numbers, agency producer codes, and IVR navigation pathways for outbound voice follow-ups on open policy change requests.

---

## 1. Direct Carriers (Commercial & Personal)

| Carrier | Servicing Phone | Agency Code | Primary LOB | IVR Guidance & Department | Operating Hours |
| :--- | :---: | :---: | :--- | :--- | :---: |
| **Merchants Insurance Group** | `(800) 462-1077`<br>Direct: `(856) 235-8890` | `84409` | Commercial Auto, BOP, Package, CGL | Press 2 for Commercial Lines, then 1 for Policy Servicing / Endorsements.<br>UW Contact: Maritza Santiago (`msantiago@merchantsgroup.com`). | 8:00 AM - 4:30 PM ET |
| **Progressive Commercial** | `(877) 776-2436` | `33617` | Commercial Auto | Enter Agent Code `33617`. Press 2 for Existing Commercial Auto Policy Service.<br>Document upload: `Upload@progressiveagent.com`. | 8:00 AM - 6:00 PM ET |
| **National General** | `(888) 325-1190` | `9010158` / `9039647` | Commercial Auto, Personal Auto | Enter Agency Code `9010158`. Press 2 for Commercial Auto Endorsements & Policy Servicing. | 8:00 AM - 5:00 PM ET |
| **Selective Insurance** | `(877) 744-3125` | `008911` | CGL, Package, CA, WC | Press 2 for Agency Servicing, enter Agent Code `008911`. Press 3 for Commercial Lines Endorsements. | 8:00 AM - 5:00 PM ET |
| **Utica First Insurance** | `(800) 456-4556` | Agency Code on File | Garage, BOP, CGL | Press 2 for Commercial Underwriting / Endorsement Processing.<br>Underwriter Contact: Heather Burgdoff (`hburgdoff@uticafirst.com`). | 8:30 AM - 4:45 PM ET |
| **Franklin Mutual (FMI)** | `(973) 948-3120` | `165100` | BOP, Dwelling Fire, Commercial Prop | Press 1 for Commercial Lines Underwriting, then 2 for Policy Changes.<br>Email: `clunderwriting@fmiweb.com`. | 8:00 AM - 4:30 PM ET |
| **Geico Commercial** | `(800) 624-2513` | `G01589` | Commercial Auto | Agency ID `G01589`. Press 2 for Agency Service, then 3 for Commercial Auto Policy Changes. | 8:00 AM - 7:00 PM ET |
| **AmTrust North America** | `(877) 528-7878` | `58388` | Workers Comp, Commercial Package | Enter Agent Code `58388`. Press 2 for Workers Comp Underwriting & Policy Changes. | 8:00 AM - 5:00 PM ET |
| **Coterie Insurance** | `(855) 566-1011` | Agency Direct | Small Business BOP / CGL | Press 1 for Agent Support & Endorsements.<br>Online ops form: `coterieinsurance.com/agents-brokers/`. | 8:00 AM - 6:00 PM ET |

---

## 2. Managing General Agencies (MGAs) & Wholesalers

| Wholesaler / MGA | Phone Number | Producer ID | Target LOB | Routing Instructions & Guardrails |
| :--- | :---: | :---: | :--- | :--- |
| **RT Specialty / Interstate MGA** | `(877) 275-9578` | On File | Commercial Lines (CGL, Excess, CA) | Ask for Caroline Shaddow or Commercial Endorsement Processing.<br>**GUARDRAIL**: Never route commercial lines to QuickHome! |
| **RT Specialty / QuickHome** | `(877) 275-9578` | On File | Personal Lines ONLY | QuickHome is strictly Personal Lines (Home/Condo/Dwelling). Direct email: `quickhome@allrisks.com`. |
| **JIMCOR Agencies** | `(201) 573-8200` | `Agt8572` | Excess, Umbrella, Hard-to-Place | Ask for Arlene Rivera (`ARivera@jimcor.com`) or Markel Excess Underwriting team. |
| **XS Brokers** | `(800) 343-7049` | On File | Excess & Specialty Commercial | Commercial endorsement service line. Have policy number & insured name ready. |
| **TAPCO Underwriters** | `(800) 334-5579` | On File | Commercial Property, Excess Lines | Press 2 for Commercial Policy Servicing. Direct email: `renewals@gotapco.com`. |

---

## 3. Autonomous Phone Call Protocol (Bland AI Voice Engine)

When an outbound call is dispatched via the phone system (`scripts/carrier_policy_change_caller.py`), the voice AI agent adheres to the following parameters:

1. **Caller Identification**:
   - Outbound Caller ID: `+1 (732) 298-6745` (StreetSmart verified agency line).
   - Introduction: *"Hello! My name is Robie calling from StreetSmart Insurance. Our agency producer code is [Agency Code]."*
2. **Identification of Change**:
   - Reference the exact policy number, insured name, and change submission date.
   - Example: *"I am following up on a commercial auto endorsement submitted on August 10th for Omega General Construction LLC, policy number 2021047341."*
3. **Primary Call Inquiries**:
   - **Issuance Check**: Has the revised declarations page or endorsement schedule been processed and issued?
   - **Document Transmission**: If issued, please email a copy to `robie@streetsmart.insurance` or advise if it is available on the agent portal.
   - **Outstanding Requirements**: If pending, what specific information or signed forms (e.g., driver exclusion, MVR, loss payee clause) are required by underwriting to release the change?
4. **Hold & IVR Navigation**:
   - Patiently wait through music and carrier hold chimes without disconnecting.
   - Do not speak until a live human agent answers.
5. **Call Conclusion & Post-Processing**:
   - Note the representative's name, department, and ticket/case number (if provided).
   - Log the call recording URL, transcript, and action summary into the EZLynx account discussion ending with `ROBIE was here`.
