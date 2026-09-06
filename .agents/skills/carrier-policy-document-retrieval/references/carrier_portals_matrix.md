# Carrier Portals & Direct Download Matrix

This reference documents agent portal URLs, login methods, and download click paths for instant carrier policy change document retrieval.

---

## 1. Direct Carrier Portal Access & Retrieval

| Carrier | Portal Name & URL | Document Download Navigation | IVANS eDocs Support | Notes & Automation |
| :--- | :--- | :--- | :---: | :--- |
| **Selective Insurance** | **eSelect**<br>`https://home.selectiveinsurance.com/WebApplications/EDS/eSelect/Home_Agent.aspx` | Search Policy # -> *Documents / eDocs* tab -> Download revised Dec Page / Endorsement PDF. | **Yes (Overnight)** | If downloaded via IVANS, the document syncs to EZLynx eDocs feed automatically. Check account eDocs folder first before portal login. |
| **Progressive Commercial** | **ForAgentsOnly (FAO)**<br>`https://www.foragentsonly.com` | Enter Agent Code `33617` -> Policy Inquiry -> Enter Policy # -> Select *Policy Documents* -> View/Download *Endorsement Declaration* and *Commercial Auto ID Cards*. | **Yes** | Instant document availability immediately following online vehicle/driver transaction submission. |
| **National General** | **NatGen Agency Portal**<br>`http://natgenagency.com` | Enter Agency Code `9010158` -> Policy Search -> Click Policy Details -> *Documents* -> Select Endorsement # -> Download PDF. | **Yes** | Commercial Auto endorsements (e.g., RAM ProMaster additions) reflect within minutes of online binding. |
| **Geico Commercial** | **Geico Agent Gateway**<br>`https://gateway.geico.com/` | Login with Agency ID `G01589` -> Search Policy # -> *Policy Activity / History* -> Select Endorsement transaction -> Download PDF confirmation. | **No (Manual)** | Driver removals and vehicle additions require manual portal PDF extraction and upload to EZLynx. |
| **Coterie Insurance** | **Coterie Agent Dashboard**<br>`https://dashboard.coterieinsurance.com` | Dashboard -> Search Named Insured / Policy # -> *Policy Documents* -> Download *Endorsement Binder / Dec Page*. | **Partial** | Instant online endorsement for small business BOP and CGL policies. |
| **AmTrust North America** | **AmTrust Online**<br>`https://agents.amtrustgroup.com` | Login Agent Code `58388` -> Policy Management -> Policy Search -> *Documents* tab -> Select revised Workers Comp or Package Dec. | **Yes** | Real-time policy change quoting and document generation for payroll and officer changes. |
| **Franklin Mutual (FMI)** | **FMI Innovation Portal**<br>`https://fmi.iscs.com/innovation` | Agent Code `165100` -> Search Policy # -> *Documents* -> View *Endorsement Declaration*. | **Yes** | Manual email follow-up to `clunderwriting@fmiweb.com` required if portal does not display processed endorsement after 48 hours. |
| **Utica First Insurance** | **Utica First Agent Portal**<br>`https://www.uticafirst.com` | Policy Inquiry -> Search Policy # -> *Policy View* -> Download endorsement letters. | **No (Manual)** | Most commercial endorsements handled manually by underwriter (Heather Burgdoff). Verify via email `cl@uticafirst.com`. |

---

## 2. Managing General Agency (MGA) Portals

| MGA / Wholesaler | Portal Link | Document Retrieval Method |
| :--- | :--- | :--- |
| **RT Specialty (Interstate)** | Direct Connector / Portal | Check Connector first. If absent, email `Caroline.shaddow@rtspeciality.com` or `interstate.endorsements@rtspecialty.com`. |
| **JIMCOR Agencies** | **OASIS Portal**<br>`https://login.jimcor.com` | Login Agent Code `Agt8572` -> Policy Inquiries -> Download Markel excess endorsements once released by underwriter. |
| **TAPCO Underwriters** | **TAPCO Online**<br>`https://www.gotapco.com` | Search policy -> *Policy Documents* -> Download revised dec pages. |
| **Cover Whale** | **Cover Whale Platform**<br>`https://app.coverwhale.com` | Search Trucking / Auto liability policy -> Download revised dec and loss payee schedules. |

---

## 3. Document Download Quality Gates

Before filing downloaded documents into EZLynx:
1. **Named Insured Check**: Compare Named Insured on document against the specific policy's **Summary Tab** ("Named Insured As Listed On The Policy").
2. **Policy Number & Term Match**: Confirm policy number, suffix/term, and effective date of the change.
3. **Readable PDF**: Ensure the PDF is not blank, corrupted, or password-protected.
4. **Filing Standard**:
   - Target Folder: `Policy Changes` (or `Declarations` if full dec page).
   - Descriptive File Name: `[Carrier] - [Endorsement Type] - [Effective Date].pdf` (e.g., `National General - Add 2015 RAM ProMaster - 2026-08-10.pdf`).
   - Label: `Endorsement` (or `Signed Change Form`).
   - Policy Association: Link explicitly to the active policy record.
