# EZLynx Ascend NOC org-label auth

Status: code-path documentation. Not a Production deploy. Not a Bland dial.

## Root cause (token, not role)

Ralph 2026-09-18 proved SSRobie can apply org label exactly `Ascend NOC`
to Buster Brown note `1128873902` in the EZLynx UI with no errors; the
label stuck after reload. Same access as Carlo1.

The hermes sim then got **HTTP 403** for SSRobie / `x-ezlynx-user u438318`
on Portal:

- `POST /EZLynxPortalAPI/Notes/{id}/OrganizationLabels`
- related applicant OrganizationLabels routes

DiscussionApi / Notes writes with the same automation **succeed**. Only
the Portal org-label **write** fails.

So the 403 is the OAuth `vendor_data_access` Bearer token (no Portal
label-write scope), not an SSRobie role problem.

Production label Ralph used: id `110248`, name `Ascend NOC`,
`enabledIn` Activities. Hermes still resolves the id by exact name at
runtime and fails closed if the name is missing or duplicated. It does
not hard-code `110248`.

## Auth path that works

**CDP session cookies** — the persistent EZLynx Chrome session the UI
and working CDP `fetch()` already use.

- Cookie header from that session (`ROBIE_BROWSER_CDP_URL`, default
  `http://127.0.0.1:9222`)
- `POST {origin}/EZLynxPortalAPI/Notes/{noteId}/OrganizationLabels`
- Body: `{"organizationLabelIds": ["<resolved-id>"]}`
- No `Authorization: Bearer`
- No `x-ezlynx-user` (sim still 403'd with it on OAuth)

Cookie values are never logged.

## Auth path that does not work

**OAuth Bearer** on Portal OrganizationLabels (PR #479). Same token is
correct for DiscussionApi note append, DocumentApi, and PolicyApi.

`EzlynxApiClient.apply_applicant_organization_label` is that 403 path
and is not called for Ascend NOC.

## Fail-closed

- Label name not exactly `Ascend NOC` (including `Cancellation`)
- Zero or more than one exact name match
- Filed note has no id
- Session cookies missing
- HTTP 403 / any apply transport error

A failed apply after the note is filed leaves the mail unread.

## QA

Test only. Never PAWIVA. Never bind. Never Bland dial. No Production
zip from this change.
