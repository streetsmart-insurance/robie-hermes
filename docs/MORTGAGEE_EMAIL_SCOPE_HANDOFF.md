# Mortgagee email scope fix — Test candidate, not deployed

Requirement: correct 4372 `skipped_lob=4` on the 32-column mortgagee task CSV;
prove the live email fetch defaults for audits and manual renewals; retain
the three-clean-Test-jobs gate and all destination verification.

Base: `ca14af2834a15b964958d72c9b1313e234f6a151` (#518).
Branch: `codex/mortgagee-email-scope`. Candidate commit/PR/digest are recorded
in the PR handoff after committing and building, not inferred from this file.

## Fixed behavior

- The job-engine worker explicitly fetches `source="email"` for report 4372.
  Missing/wrong-subject email cannot trigger Looker fallback from this worker.
- Closed tasks are counted separately and excluded before policy lookup.
  All-closed/no-in-scope runs still fail independent verification.
- Email projection preserves 4372 task rows. Closed-first duplicates cannot
  hide an Open task. Remaining tasks group by policy; conflicting applicant
  or policy-master identities block rather than selecting the first.
- Open email rows resolve a unique PolicyApi record by policy number,
  applicant ID, and Policy Master ID when supplied. No prefix inference.
- Explicit LOB normalization supports the requested Homeowners/Home/Home NJ/HO,
  Flood/FLD, Dwelling Fire and Condo values. Confirmed Auto/WC/GL are excluded;
  unrecognized/missing values are `unresolved_scope`, with per-policy outcomes.
- Task Due Date is only `due_date`. The policy's actual expiration supplies
  the renewal clock. Missing/unparseable property expiration blocks.
- Lookup output is restricted to policy metadata; lender, loan, producer
  authorization, and portal evidence cannot be supplied by this lookup.
- Independent verification refuses empty work and unresolved scope. Existing
  lender/producer mismatch checks are retained.
- A Prod-environment offline test exercises 4246/4247 default Gmail binding
  with no injected CSV/service parameter and asserts no Looker call.

## Validation and remaining uncertainty

Offline scope tests start with the real 32-column schema. The named battery
scenario `mortgagee:email-metadata-before-lob` exercises the real shared
checkpoint/durable-outcome path in a fresh interpreter. No network or outreach
is used by these tests. Existing mortgagee verifier fixtures now persist their
checkpoints and initialize their durable ledger before fresh read-back.

The PolicyApi response aliases are an explicit parsing contract, NOT a claim
that a live tenant response was observed in this session. On Test, verify the
literal identity/LOB/expiration fields and unique term match. Missing keys,
multiple terms, identity mismatch, auth failure, or a response error remain
blocked. Never relax identity checks or infer LOB from a policy prefix to pass.
The client uses the established `load_ezlynx_api_config()` environment/secret
selection, with no UAT-to-Production fallback.

This fixes ingestion/scope/planning. It does not add a lender upload adapter
or certify end-to-end delivery. Additional Interests remains the established
loan-number source; no loan column, PDF/OCR parsing, or new browser navigation.
Intent evidence is not proof of delivery. COMPLETE still requires the Job
Engine's independent destination verification.

## Test execution and promotion handoff

1. Obtain review and passing Linux CI. Do not run credential-bearing workflows
   from this branch. Use the protected-main Test deployment workflow only.
2. Before any Test change, inventory the actual installed release/digest,
   open jobs and leases; preserve the rollback target. Documentation's old
   release SHA is not a runtime observation. Confirm SSRobie ownership through
   the Moe board; never dual-drive the identity.
3. Deploy the reviewed candidate with the Test identity and record the archive
   digest, install proof and loaded version. Keep schedules HOLD.
4. Replay the four historical Closed tasks: expect `skipped_closed=4`,
   `skipped_lob=0`, `in_scope_count=0`, no upload, and no gate credit.
5. Identify an existing sanctioned Open/current Test property policy with
   the correct renewal timing. Verify actual PolicyApi fields/read-back.
   Do not reopen Closed tasks or relabel real policies to manufacture a pass.
6. Run three distinct jobs serially on `hermes-test-01`, inspecting the prior
   job before starting the next. Require at least one Open in-scope property
   policy and a persisted `post_job_audit.verdict=PASS` on each. Retain the
   exact job IDs, release digest, scope/outcome evidence, and any required
   destination receipts. Voice remains disabled. Preserve idempotency.
7. Independently review the three job audits; do not count these offline tests
   or an empty queue as a business pass. Record the promotion evidence with:

   ```sh
   ROBIE_ENV=TEST python3 scripts/check-job-type-gate.py record-from-db \
     --db /opt/streetsmart-hermes-test/robie-job-engine/data/jobs.db \
     --job-type mortgagee_verification
   ```

8. Hand the exact QA-certified digest to Release / Production for Carlo GO.
   Verify Prod Gmail delegation and loaded code independently. Then run the
   supervised three renewals and three audits before a full queue or schedule
   release. Policy Change Checking and Production JE-KILL remain untouched.

## Blockers observed while preparing this candidate

- GitHub Test deployment run `35517115058` did not start: its check annotation
  states failed account payments or spending-limit exhaustion. Organization
  billing must be cleared before CI/protected Test workflow can execute.
- On this macOS builder the full battery reaches two pre-existing failures
  in `tests/test_test_deploy_workflow.py`: GNU `install -D` semantics are not
  supported by the Mac command. Both reproduce on unchanged base `ca14af28`.
  Do not mark the Linux release gate passed from this local result.
- No Test/Prod host access, live metadata response, deployment proof, or three
  qualifying Test jobs has been established by this candidate. Gate stays 0/3.

QA owners must verify: Closed-first duplicate policy; all Closed; Open Home,
Flood, Dwelling Fire and Condo; unknown LOB; wrong/ambiguous policy identity;
unknown task status; real expiration versus stale task due; wrong CSV/subject;
missing credentials; lender mismatch; no unauthorized upload/call; and 4246/4247
default live email-source selection. No self-certification or Production GO
is implied by this document.
