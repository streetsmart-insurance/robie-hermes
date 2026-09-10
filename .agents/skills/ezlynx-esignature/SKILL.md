---
name: ezlynx-esignature
description: Automated EZLynx AssureSign E-Signature envelope creation, multi-signer setup (client + agent custom contacts), RadPdf DOM and coordinate mapping, tab placement, and sending.
---

# EZLynx AssureSign E-Signature Automation

This skill documents and standardizes autonomous creation, configuration, field placement, and dispatch of E-Signature envelopes within EZLynx using AssureSign and RadPdf.

## Architecture & Lifecycle

The EZLynx E-Signature wizard operates across 4 steps:
1. **Add Document(s)** (`/web/account/{applicant_id}/documents/esignature`):
   - Locate the target document in the client's Document Library table.
   - Click the `Add` action button on the corresponding row.
   - Click `Next`.

2. **Setup Envelope**:
   - Envelope Name: Standard format `[Carrier Name] [Line of Business] Application - [Named Insured]`
   - Reference Number: Policy number, submission number, or quote ID (e.g. `7468202`).
   - Signer 1: Primary Named Insured (auto-populated by EZLynx as Recipient 1).
   - Signer 2 (Agent): Click `Add Signer` -> `Add Custom Contact` -> Enter Agent Name (`Carlo Ferrara`) & Email (`carlo@streetsmart.insurance`) -> Save.
   - Note / Instructions: Detailed instructions explaining the document and signing process.
   - Click `Next`.

3. **Setup Signature** (`/web/account/{applicant_id}/documents/esignature/setup-signature`):
   - RadPdf operates inside two nested iframes:
     - Outer frame: `https://pdfedit.ezlynx.com/EZLynxPdfPortal/...`
     - Inner frame: `RadPdf.axd` (`radPdfWebControl`)
   - Signature & Date fields are placed via the RadPdf JavaScript API in the outer frame:
     - Pages: `window.oRadPdf.getPage(pageNum)` (1-indexed).
     - Recipients: `window.arRecipients` array containing `{ ID, RecipientName, ... }`.
     - Field Creation: `curPage.addObject(objectType, left, top, width, height)` followed by `window.setObjectProperties(obj, properties)`.
     - Validation: `window.validateTabRule()` guarantees all recipients have required signature tabs.
   - Advance: Click `Review and send` (`#next`).

4. **Review and Send** (`/web/account/{applicant_id}/documents/esignature/review`):
   - Review envelope overview, recipient mapping, and page previews.
   - Click `Send` (`#next`).
   - The envelope is dispatched to all signers via AssureSign, an automatic audit note is recorded in EZLynx Activity/Discussions, and the browser navigates back to `/documents`.

## Python Module Usage

```python
from src.ezlynx.esignature_sender import EZLynxEsignatureSender

sender = EZLynxEsignatureSender(cdp_url="http://localhost:9222")
await sender.send_envelope(
    applicant_id="221228031",
    document_name="Franklin Mutual Insurance Homeowners Application.pdf",
    envelope_name="Franklin Mutual Insurance Homeowners Application - Fidel Cuatlacuatl",
    reference_number="7468202",
    agent_signer={
        "name": "Carlo Ferrara",
        "email": "carlo@streetsmart.insurance"
    },
    note="Please review and sign the attached Franklin Mutual Insurance Homeowners Application for 2 Cascaes Way, Freehold, NJ. Thank you!"
)
```
