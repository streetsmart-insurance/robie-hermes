# Ascend API program creation

`ascend.create_program` is the operational path for creating an Ascend
program and its quote billables. It does not use Playwright and it does not
replace `ascend.locator_artifact_audit`, which remains the non-saving test for
Ascend's Import document UI.

## Safety boundary

- Planning is the default and does not load a credential or contact Ascend.
- The operator script permits execution only with `ROBIE_ENV=TEST`.
- TEST accepts only `https://sandbox.api.useascend.com`.
- Production accepts only `https://api.useascend.com`, requires a second
  explicit enable flag, and remains subject to the existing action gate.
- API credentials are accepted only through a full Secret Manager version
  reference in `ROBIE_ASCEND_API_KEY_SECRET`. Never put a key in a payload,
  command line, log, or chat.
- PAWIVA and account `221398001` are rejected recursively before a credential
  is loaded.
- The worker performs no email, checkout, payment, or bind operation.

Do not configure the Test secret reference until Test/Production Secret
Manager isolation is confirmed. Use a sandbox credential for TEST.

## Required payload

The program must refer to an existing sandbox insured and sandbox users. The
billable uses stable carrier and coverage identifiers from Ascend's reference
endpoints. Money is expressed in integer cents and dates use `YYYY-MM-DD`.

```json
{
  "program": {
    "insured_id": "00000000-0000-0000-0000-000000000001",
    "producer_id": "00000000-0000-0000-0000-000000000002",
    "account_manager_id": "00000000-0000-0000-0000-000000000002"
  },
  "billables": [
    {
      "billable_identifier": "TEST-QUOTE-1",
      "carrier_identifier": "sandbox-carrier-identifier",
      "coverage_identifier": "commercial_auto",
      "effective_date": "2026-09-01",
      "expiration_date": "2027-09-01",
      "premium_cents": 125000,
      "agency_fees_cents": 50000
    }
  ]
}
```

## Plan without contacting Ascend

```bash
python scripts/run-ascend-api-program.py --payload /path/to/payload.json
```

The result lists the bounded requests and includes
`"network_performed": false`.

## Sandbox execution

After a sandbox key has been stored in an isolated Secret Manager secret and
the sandbox UUIDs/identifiers are known:

```bash
export ROBIE_ENV=TEST
export ROBIE_ASCEND_API_ENABLED=1
export ROBIE_ASCEND_API_BASE_URL=https://sandbox.api.useascend.com
export ROBIE_ASCEND_API_KEY_SECRET=projects/PROJECT/secrets/SECRET/versions/VERSION
python scripts/run-ascend-api-program.py \
  --payload /path/to/payload.json \
  --db /opt/streetsmart-hermes-test/robie-job-engine/data/jobs.db \
  --execute
```

The Job Engine creates one program, creates each billable under it, and then
uses fresh GET requests to verify the program and every returned billable ID.
If program creation succeeds but a billable fails, it persists the program ID
and refuses an automatic create retry so it cannot silently duplicate the
program.
