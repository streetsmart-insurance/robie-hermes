# EZLynx Discussion API Label Probe — Results
**Date:** 2026-10-03
**Probed from:** hermes-poc-01 (box), via `robie_job_engine.ezlynx_api`
**Test account:** 25486692 (Jake N Ferrara — has labeled test notes)

## Answer: NO — the Discussion API does NOT expose labels

### What was tested

1. **`GET /DiscussionApi/v8/discussions/by-applicant?applicantId=25486692`**
   - HTTP 200, 480 discussions returned
   - Fields on every discussion: `applicantId`, `created`, `createdById`, `deleted`,
     `discussionId`, `lastModified`, `lastModifiedById`, `mostRecentNoteId`,
     `noteCount`, `opportunityId`, `organizationId`, `title`, `watcherUserIds`
   - **Zero label-related fields.** No `label`, `labels`, `tag`, `tags`, `category`, or similar
     across all 480 discussions.

2. **Label-specific endpoints** — all HTTP 404 (do not exist):
   - `/v8/labels`
   - `/v8/discussions/labels`
   - `/v8/organizationlabels`
   - `/v8/discussionlabels`
   - `/v8/notes/labels`

3. **v1 endpoint** (`/DiscussionApi/discussion/v1/applicant/{id}`) — HTTP 404.
   This is what `EzlynxApiClient.get_applicant_discussions()` calls; it silently
   returns `[]` on the 404. **That method is broken** — it never returns data.

### Implication for the polling architecture

Carlo's idea (poll EZLynx every 10–20 min for new labeled notes instead of relying
on the webhook to pull note data) **cannot work for label filtering** — the poller
has no way to see which discussions/notes carry a given label. The Zapier
"New Note" trigger remains the ONLY source of label information.

### What the API *does* give us (useful for the dispatcher)

- `title` — the discussion title IS returned. If the freeform instruction goes in
  the **discussion title** instead of the note body, the dispatcher CAN read it via
  the v8 API. This makes option (b) from the note-body discussion viable with no
  browser work.
- `mostRecentNoteId`, `noteCount`, `lastModified` — useful for detecting new activity
  in a poll loop, but without labels the poller can't tell *why* the activity happened.
- `opportunityId` — links the discussion to an opportunity.

### Recommended architecture (given these findings)

- **Keep the Zapier → webhook path for label detection.** It's the only label source.
  The webhook already receives `applicant_id` + `label_name` + `campaign_id` — that's sufficient.
- **For freeform note text:** put the instruction in the discussion **title** (not the
  note body). The dispatcher fetches the latest discussion via the v8 API and reads
  `title`. No browser task needed, no Portal API gamble.
- **Do NOT build a label-polling loop** — it can't see labels, so it adds complexity
  with no benefit over the webhook.
