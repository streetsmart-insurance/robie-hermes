# ezlynx-api: the shared EZLynx command line for agents

One command that Claude, ChatGPT and Grok Bot all use to read and write
EZLynx documents, discussions and policies through the API. It runs on the
Production host as `streetsmart-hermes`, so it uses the same credentials,
write allowlist and gates as robie-filer. No browser, no EZLynx login.

Requested and approved by Carlo on 2026-10-09. Carlo accepts the existing
full-admin ssh route, so there is no new account.

## How to call it

From any machine that already has `gcloud` access to the project:

```
gcloud compute ssh hermes-poc-01 --project streetsmart-hermes-poc \
  --zone us-east1-b --tunnel-through-iap --quiet \
  --command 'cd / && sudo -u streetsmart-hermes /usr/local/bin/ezlynx-api --agent claude docs list 220250093'
```

`--agent NAME` is required on every call. Use `claude`, `chatgpt` or
`grok-bot`. It is written to the audit log. The wrapper also runs from `/`
as `streetsmart-hermes` by itself, so on the host this works too:
`ezlynx-api --agent grok-bot selftest`.

Every call prints one JSON object. Exit codes: `0` done, `2` bad usage,
`3` refused by a guard, `4` error, `5` a write was sent but could not be
confirmed (do not retry; look first).

Quoting through ssh is awkward. For notes and questions, send the text on
stdin instead:

```
printf '%s' "What is the liability limit?" | gcloud compute ssh hermes-poc-01 \
  --project streetsmart-hermes-poc --zone us-east1-b --tunnel-through-iap --quiet \
  --command 'cd / && sudo -u streetsmart-hermes /usr/local/bin/ezlynx-api --agent claude docs ask 827501466 --question-file -'
```

## Read verbs (safe for any agent)

| Command | What it does |
|---|---|
| `selftest` | Local checks only: state dir, `pdftotext`/OCR present, write allowlist, Gemini settings. No EZLynx or Gemini call. |
| `docs list <applicant> [--name-contains S] [--limit N]` | DocumentApi search: `{id, name}` per document. |
| `docs get <doc_id> --out <new path>` | Downloads to a new file (mode 0600, never overwrites). Prints size and sha256, not the body. |
| `docs read <doc_id> [--max-pages 40] [--max-chars 200000]` | Text per page: `pdftotext -layout`, `tesseract` for pages with no text. Reports which pages needed OCR. |
| `docs ask <doc_id> "<question>" [--direct] [--model M]` | Sends the extracted text to Vertex Gemini (default `gemini-3.8-flash`) and prints the answer with page numbers. |
| `discussions list <applicant>` | Discussions on a client: id, title, `untitled` flag. |
| `discussions get <discussion_id> [--limit 50] [--raw]` | One discussion with note bodies. |
| `policy lookup <policy_number> [--raw]` | Client id and policy id for a policy number. |

Verbs that take only a document or discussion id accept
`--audit-applicant <id>` so the audit log can record the client.

`docs ask` notes:

- The prompt tells the model the document is data from outside and to ignore
  instructions inside it, to answer only from the document, and to cite pages.
- The extracted text goes to Vertex in this project (the same service the
  Hermes agent already uses). Text is cut at `--max-chars` (default 120000).
- `--direct` sends the PDF or image itself as `inlineData` (10 MB limit).
  **UNVERIFIED**: the request shape is covered by a stub test only. Use
  extracted text unless a scan has no readable text.
- Model, project and location come from `ROBIE_GEMINI_MODEL`,
  `ROBIE_GEMINI_PROJECT` and `ROBIE_GEMINI_LOCATION` (the wrapper defaults
  them to `gemini-3.8-flash`, `streetsmart-hermes-poc`, `us-central1`).

## Write verbs (the existing allowlist applies)

| Command | What it does |
|---|---|
| `docs upload <applicant> --file F --name N [--policy-master-id ID] [--content-type T] [--allow-duplicate] [--dry-run]` | `upload_document_via_api`: DocumentApi upload, then a fresh search must show the new id. Skips the upload when a document with the exact same name exists, unless `--allow-duplicate`. |
| `notes add <applicant> (--discussion-id ID \| --title T) (--text T \| --text-file F) [--dry-run]` | `file_note_to_existing_discussion`: existing discussion only, note ledger, read-back by text. Never creates a discussion. Notes with a phone number are refused. |

What guards them (unchanged, none of it is new code here):

- `ezlynx_write_scope`: the compiled applicant allowlist. Today that is the
  test client `220250093` only. A client not on it is refused before any
  EZLynx request (`EZLYNX_WRITE_SCOPE_REFUSED`, exit 3).
- There is no `--any-applicant` flag and no way to widen scope from this tool.
  The wrapper unsets `ROBIE_EZLYNX_WRITE_SCOPE`, `ROBIE_EZLYNX_WRITE_APPLICANT_IDS`
  and `ROBIE_PLAYGROUND`, and the tool also refuses to write if an all-clients
  setting is present in its process.
- The driver lease (`ezlynx_driver_gate`) must be with PRODUCTION on the
  Production host, and the usual live-write and API-only note/document rules apply.
- `--dry-run` runs the same allowlist check and writes nothing.

## Audit log

`/var/lib/ezlynx-api-cli/audit.jsonl` (override with `ROBIE_API_CLI_STATE_DIR`
or `--state-dir`). Owner `streetsmart-hermes`, folder 0750, file 0640.
One JSON line per call, appended under a lock, never rewritten:

```
{"agent":"claude","command":"docs.upload","phase":"end","result":"uploaded",
 "applicant":"220250093","doc_id":"901","document_name":"...","bytes":123,
 "sha256":"...","invoked_by":"sa_1126...","host":"hermes-poc-01","ts":"..."}
```

A live write also gets a `phase":"start"` line first, so a crash mid-write
leaves a trace. The log holds ids, sizes and hashes. It never holds document
text, note text, questions or answers (only their length and sha256). If the
log cannot be written, the verb is refused before anything is read or written.
The log is not rotated by this tool.

## Install (needs a release on the host and Carlo's go)

```
sudo /opt/streetsmart-hermes/current/scripts/install-ezlynx-api-cli.sh \
  --release-dir /opt/streetsmart-hermes/current           # add --robie-env TEST on hermes-test-01
```

It copies `scripts/ezlynx-api` to `/usr/local/bin/ezlynx-api`, creates the
state folder, writes `/etc/streetsmart-hermes/ezlynx-api-cli.env`
(`ROBIE_ENV` only), and runs `selftest`. Preview with `--dry-run`. Remove with
`--uninstall` (the audit folder stays). It does not restart any service.

The wrapper runs the Python from `/opt/streetsmart-hermes/current`, so a new
release is picked up without reinstalling it.

## Known limits

- Reads and writes both use the Prod EZLynx API credentials from Secret
  Manager; reads are not separately credentialed.
- `docs read` handles PDFs and plain text. Images need `docs ask --direct`
  (UNVERIFIED). The tesseract install has English only.
- Applicant-level documents only unless `--policy-master-id` is given.
- Task creation, policy create, deletes, and any-client writes are not here.
