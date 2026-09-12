# Canary arming protocol

The L3 canary-watch (`/.github/workflows/canary-watch.yml`) does nothing until
it is armed. Arming = the file `canary/ARMED.json` exists at the checked-out
ref and parses as JSON with `"armed": true`.

While the file is absent, every canary firing (05:30 daily, post-deploy)
reports `NOT_ARMED_NO_BASELINE`, uploads a `canary-watch-status-<run_id>`
artifact, and exits 0 — quiet. Not red, not green, no notification. Ralph's
06:30 review layer stays silent on not-armed verdicts.

## What arms it

The first successful end-to-end policy-setup run. "Successful" means the
Phase 5 acceptance gate passed with destination evidence: the policy exists
in PolicyApi, applicant identity agrees, job rows agree, Robie's reply
agrees, and document/note evidence was read back from the destination.

Arming is a required step of recording that success — not a separate flag
anyone has to remember. Whoever records the Phase 5 pass writes this file in
the same change, with the evidence inline:

```json
{
  "armed": true,
  "armed_at": "2026-09-15T12:00:00+00:00",
  "event": "first successful end-to-end policy-setup run (Phase 5 gate)",
  "evidence": {
    "policy_number": "TEST-HO-20260911-E01",
    "policy_id": "83651751",
    "verification_run_id": 0,
    "verified_at": "2026-09-15T12:00:00+00:00"
  }
}
```

The future L2 canary writes the same file on its first green run, if the
Phase 5 email gets there first.

## Why

Judging red/green against a baseline from a path that has never succeeded is
noise. The canary's job is proving the whole path still works; until the path
has worked once, there is nothing to prove and nothing to wake Carlo about.
