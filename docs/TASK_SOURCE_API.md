# Task source: EZLynx API with the emailed report as the fallback

`ROBIE_TASK_SOURCE` chooses where the task intake reads Robie's tasks.

| Value | Behavior |
|---|---|
| unset, `report`, anything else | The emailed "Robie AI - Task Check-In" report. Exactly as before. **This is the default.** |
| `api` | Ask EZLynx for tasks assigned to Robie AI (SSRobie, 438318). If the answer is not complete and well formed, use the report for that run. |

Nothing downstream of the source changed: seen-task store and first-run baseline, one-time hold notes, batch cap, daily call cap (Prod 5), dedupe, calling window, kill switch, write-scope guard, lease, "Robie Call dials only a typed number", callback number. Labeled-note pickup stays as configured (off on Prod). A task with no call label is left untouched, whatever the source.

## What is and is not confirmed (read this before turning it on)

Confirmed, in the repo and in EZLynx's own words (Web Services, 2026-10-02):
- A task is a note of type `TaskCreationNote`; `task.assignedUserId` is the assignee.
- There is no standalone task endpoint. `PUT/POST /v8/tasks/:taskId` update one task by id.
- Reads are by note id (`POST /v8/notes/query` with `noteIds`) or per discussion (`GET /v8/discussions/{id}/with-notes`).
- `GET /v8/discussions/by-applicant` lists one applicant's discussions.
- The 2026-10-03 label probe found no label fields on discussions and 404 on every label route.
- Our OAuth request for a `TaskApi` scope returns `400 invalid_scope` (2026-09-27).

**Not confirmed: any endpoint that lists tasks by assignee, and any place on a note or task where the call label (`Activity Labels`) can be read through the API.** The report is the only source we know that shows both. So the live listing call is **UNVERIFIED**, and the API source cannot start a call until both exist: an unlabeled task is never dialed.

`ROBIE_TASK_API_LIST_PATH` is unset. While it is unset, `ROBIE_TASK_SOURCE=api` logs "no verified EZLynx endpoint lists tasks by assignee" and the report is used, so turning the flag on today is a harmless no-op. To find out whether an endpoint exists, run the read-only probe in an approved window:

    python scripts/probe_task_list_endpoint.py --path v8/<candidate>

It does one GET with the existing login, prints status and key names (never values), and never writes. If a path answers with a list that carries task ids, applicant ids, discussion ids, assignee ids, creation times (with zone) and labels, set `ROBIE_TASK_API_LIST_PATH` to it. Ask EZLynx Web Services at the same time: "is there a read endpoint that lists tasks by assigned user, and does a note expose its labels?". If the answer is no, a push source (EZLynx's Zapier "Note Created" trigger carries Task Assignee, Labels, Discussion ID and Note ID; the note text is then read by id) is the documented alternative.

## What the API source returns

A flat record, or a `TaskCreationNote` shaped like the Discussion API's notes, per task: task id, applicant id, discussion id, assignee id, status, note text, labels, creation time **with a zone**, last modified. Closed tasks and other assignees are skipped. A row that cannot be read, a duplicate task id, or a listing not marked complete makes the whole listing unusable (fall back to the report). There is no 500-row cap on this path.

The listing becomes a report (`message_id` `api:<digest>`; the same listing is the same delivery, a changed task is a new one). `newest_created_et` is the time of the read, so the health probe measures how fresh the source is.

## Auth

The existing Task API login (`ezlynx_task_api`): `vendor_data_access` acting as `ROBIE_EZLYNX_TASK_API_ACT_AS_USERNAME` (carlo1), the Production EZLynx API secret, the token cache, and the one-failed-login 60 minute stop, shared with task creation. `ROBIE_EZLYNX_DIRECT_TASK_API_ENABLED=0` turns the API source off too. The module only reads.

## Note write-back follow-ups in the same change

| Symptom | Cause | Status |
|---|---|---|
| "no matching discussion" after a successful call, outside-window queue note "uncertain" (task 63594085, Oct 9 8:25-9:02 AM ET) | The task's discussion was untitled; the pinned id was filtered like a guess | Fixed by #823 (and #825 for the Ascend preview). Not redone. Journal has no recurrence since Oct 9 6 PM ET (there have been no labeled calls). |
| Hold note (too old / could not tell when) recorded "attempted, no note id" although it posted | `_post_hold_note` needed a note id in the POST response; EZLynx returns none. #831 fixed only the task worker | Fixed here: record note ids first, accept only exactly one new id with the exact text, never post twice |
| A pinned discussion the applicant list omits (made moments ago) or written `851287313.0` | Exact string match against the list only | Fixed here: ids compare as digits; if the id is not in the list at all, one direct read must show this applicant's id, not deleted. A list row marked deleted is never second-guessed. |
