# Selector and page-object inventory

Status: **uncertified Test candidates**. No item below authorizes a write.

| Area | Repository source | Draft status | QA requirement |
|---|---|---|---|
| Policy shell | `robie_job_engine/ezlynx_policy_setup.py` | Homeowners Test fixture proved exact Add/Edit controls; legacy multi-LOB orchestrator remains locked | Preserve duplicate check, `$1.00` header proof, and Test account attestation |
| Homeowners FormEntry | runtime Playwright guard + `ezlynx_write_scope.py` | Numeric route may write only after visible ROBIE Test / `TEST-HO-` / Homeowners / `$1.00` attestation | Re-prove after any EZLynx markup change; no static policy id |
| LOB form entry | `robie_job_engine/ezlynx_policy_setup.py` | Incomplete for profiles other than the narrow Homeowners Test drill | One page object per approved LOB screen and explicit state waits |
| Locator candidates | `robie_job_engine/locators/ezlynx_policy.json` | Candidate registry; not Policy Setup certification | Prove uniqueness and semantic ownership in Test DOM snapshots |
| Duplicate search | Not implemented in browser layer | Blocked | Exact applicant/carrier/policy/term/LOB lookup and zero/one/many handling |
| Checkpoint/resume | Business contract only | Blocked | Durable before/after-Save checkpoints and destination inspection on resume |
| Reopen verification | Structured comparator in `ezlynx_policy_setup_profiles.py` | Unit-tested; browser readback not wired | Reopen the exact policy and populate actual values from destination DOM |
| Evidence | Existing trace/snapshot/job evidence utilities | Available components; not wired to this skill | Screenshot, trace, checkpoint log, and structured verification per fixture |

## Homeowners Test observations — 2026-08-31

- Account authority: exact visible link titled `Go to Applicant Overview` with
  href `/web/account/220250093/overview` and text `ROBIE Test LLC`.
- Form identity header: visible `Policy Number: TEST-HO-...`, `Line of
  Business: Homeowners`, and `Full Term Premium: $1.00`.
- Global navigation controls: `#finishButton-header`, `#cancelButton-header`,
  `#previous-header`, and `#next-header` were unique.
- Underwriting location: `select[name="workData.Form88.Location"]:visible`.
- No-animals answer:
  `#Residential_Question_KBCCode_A_Off:visible`.
- Named-insured-is-owner answer:
  `#Residential_Question_KDZCode_A_On:visible`.
- Additional-interest fields used exact visible ids and the Location selector
  `select[name="Form88.AdditionalInterest_Item_LocationProducerIdentifier_A"]:visible`.
- Interest type is a custom multiselect. Select the visible exact Mortgagee
  option; never choose a hidden duplicate or resolve by position.
- Add Note is a separate right-side website pane and is not part of the
  underlying FormEntry page.
- The observed manually created Homeowners policy rendered no coverage-limit
  inputs in `Dwelling Information / Coverages`; absence is a stop condition,
  not permission to use Remarks.

Coordinates, positional locators (`first`, `last`, `nth`), ambiguous selector
lists, sleeps used as readiness, and visual/AI clicking are not approved primary
selectors. Visual/AI interaction may be proposed only as a documented,
fail-closed fallback after deterministic Test evidence is exhausted.
