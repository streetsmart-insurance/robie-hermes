# SOP automation pilot — four separate workflows

## Authoritative sources and latest decisions

Website page verified: https://sites.google.com/streetsmart.insurance/siteswiki/sops-standard-operating-procedures/mailhellodocument-retrieval

It embeds Mail, Hello & Document Retrieval:
https://docs.google.com/document/d/1ypTB9sf1TaSPA0F_hW8ODezvFQpQRln-BywiNwcGW40/edit
(Drive modified August 9, 2026). Certificates source:
https://docs.google.com/document/d/18-rHfPe4pe0pUon_I3Vr9NpVFX3DNij_GvkhgcNslOU/edit

Jake's latest pilot instruction supersedes the earlier intake assignment plan:
Hello goes to Alejandro; Certificates goes to the certificates user. These are
initial intake owners; licensed service decisions still need the SOP's review.
Use `HelloPilotIntake` and `CertificatesPilotIntake` from `pilot_intake.py` for
this pilot, not the older generic SOP routing classes. The server must implement
`lookup_pilot_assignee(key)` returning one fresh authoritative `ReadResult` row
with `assignment_key` and verified numerical `user_id`. Keys are `hello_alejandro`
and `certificates_user`; keys are configuration names, not EZLynx usernames.
Certificates additionally requires verified team membership. Missing mappings
hold; no username or numerical ID is guessed.

## Work now implemented

- Separate original-message intake components, account/policy matching contracts,
  private source preservation, duplicate checks and independent task read-back.
- Explicit pilot owner selection above, with active-user validation.
- A review-only document planner for scanned mail and carrier documents. Given
  human-confirmed document boundaries, codes and policy identifiers, it produces
  SOP document titles and task-review instructions. It rejects overlapping or
  omitted pages, ambiguous PD (Policy versus Declarations Page), unknown types
  and missing policy identifiers. Quote proposals are marked sharing prohibited.
  Recommendations require an existing-work check and required workflow handling.
- Stable page-range/source-digest keys for later reconciliation; these do not
  replace server-enforced duplicate protection.

The planner does not perform OCR, split PDF bytes, rename Drive files, upload,
classify emails, create real tasks, share with clients or update carrier trackers.
Its account/policy fields are human-confirmed input, not verified API evidence.
All outputs remain REVIEW_ONLY until live matching and destination checks occur.

## SOP distinctions that must survive implementation

Check Activities and Documents before creating work. Same-date/reason cancellation
notices attach to existing work; different dates/reasons may need new work.
Non-renewal and cancellation require separate workflows. Finance notices without
a cancellation date are Additional Information; finance intent does not authorize
a manual policy cancellation. Inspection announcements are Correspondence;
inspection results with recommendations use Recommendations. Quote proposals
must not be shared through this flow. AR audit escalation and policy status
changes remain human review; no automated coverage decisions are implemented.

Use the exact document-type title in the source directory. The readable source
does not establish a universal date/carrier/policy filename suffix or all folder
destinations. Do not invent those. Additional source-linked examples are needed
before bulk renaming. Originals remain preserved. Client sharing, labels and
archive actions stay pending until the relevant workflow and evidence are complete.

## Next execution sequence

1. Connect the existing Test API service to matching, exact pilot user IDs,
   task lookup/create/read-back and document upload/library. The Postman collection
   lists document operations; tenant capability is still unverified. Retain the
   manual-upload obligation until actual upload and policy association read back.
2. Confirm exact Hello/Certificates mailbox addresses and the scanned-mail folder.
   Add dedicated read-only ingestion, durable source/version checkpoints and a
   review queue. Do not mark messages or files processed on mere receipt.
3. Run the document planner on approved sample scans and carrier files. Verify
   every page, code, title, policy association and existing-work decision. Add OCR
   and PDF splitting only after these examples establish the rules.
4. Connect Progressive separately: FAO Policy Activity by processed date,
   unticked Policies Need Service, and BOP/Contractor GL Pending Cancel for
   Nonpayment. Include missed days/weekends/holidays and check prior delivery.
5. Update indexing/retrieval trackers only after the related work is verified.
   The SOP requires SL number, insured, policy, folder, document number and action.
   Website-linked retrieval sheet ID: 1HL6Uw5nAJjZ3qtCleUzXUtOC_xmhFPmy0LPbz89v7vw.
   Connector returned 403, so actual tabs/checkpoints were not verified.
6. Reliability/QA verifies each flow, duplicates, session expiry, ambiguous matches,
   wrong owners, combined scans and restart after uncertain writes. Record Test
   release/digest, rollback target and destination evidence before promotion.

## Validation and status

58 focused synthetic intake tests passed locally, including eight new pilot and
document-plan tests. The prior 50 intake tests remain passing. GitHub CI validates
the combined branch separately. No live tasks, uploads, policy changes, file
renames or deployment occurred. Test release/digest and rollback remain UNVERIFIED.
The draft email to Nicole/Carlo/Gabby remains unsent.
