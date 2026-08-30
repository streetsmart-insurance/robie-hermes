# Selector and page-object inventory

Status: **uncertified Test candidates**. No item below authorizes a write.

| Area | Repository source | Draft status | QA requirement |
|---|---|---|---|
| Policy shell | `robie_job_engine/ezlynx_policy_setup.py` | Legacy candidate; write orchestrator locked | Prove unique stable selectors on sanitized Test fixture |
| LOB form entry | `robie_job_engine/ezlynx_policy_setup.py` | Incomplete for the 12-profile package | One page object per approved LOB screen and explicit state waits |
| Locator candidates | `robie_job_engine/locators/ezlynx_policy.json` | Candidate registry; not Policy Setup certification | Prove uniqueness and semantic ownership in Test DOM snapshots |
| Duplicate search | Not implemented in browser layer | Blocked | Exact applicant/carrier/policy/term/LOB lookup and zero/one/many handling |
| Checkpoint/resume | Business contract only | Blocked | Durable before/after-Save checkpoints and destination inspection on resume |
| Reopen verification | Structured comparator in `ezlynx_policy_setup_profiles.py` | Unit-tested; browser readback not wired | Reopen the exact policy and populate actual values from destination DOM |
| Evidence | Existing trace/snapshot/job evidence utilities | Available components; not wired to this skill | Screenshot, trace, checkpoint log, and structured verification per fixture |

Coordinates, positional locators (`first`, `last`, `nth`), ambiguous selector
lists, sleeps used as readiness, and visual/AI clicking are not approved primary
selectors. Visual/AI interaction may be proposed only as a documented,
fail-closed fallback after deterministic Test evidence is exhausted.
