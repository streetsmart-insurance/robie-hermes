# RH-012 — Magellan Sad-call collection QA candidate

Date: 2026-09-10  
Branch: `fix/rh-012-magellan-sad-call-qa`  
Environment: local engineering checkout only  
Production changed: no  
Test deployed: no  

## Requirement

Make the read-only Magellan Sad-call collector safe for operational QA by
covering the remaining acceptance gaps from PR #179:

- verified empty-result handling;
- newest-first ordering before the older-date early stop;
- bounded two-page pagination and fail-closed page-limit exhaustion;
- deterministic output ordering and duplicate suppression; and
- truthful phone masking in management-facing `sad_calls` rows.

The collector must not open call details, collect transcripts, or click
Magellan's handled-state control.

## Candidate behavior

- Waits for either a Sad-call row or Magellan's visible empty state. A verified
  empty state produces an available snapshot with zero calls and
  `result_status=empty`; it is not treated as a loading timeout.
- Verifies newest-to-oldest order within each page and across page boundaries.
  An unsorted result fails closed instead of using an unsafe early-stop.
- Stops only at a verified older-date boundary or a disabled Next control.
  Reaching `max_pages` while Next remains enabled raises an error and writes no
  output snapshot.
- Deduplicates by call ID and sorts collected records deterministically.
- Keeps raw From/To values in the restricted `records` collection needed for
  RingCentral reconciliation. Management-facing `sad_calls` rows omit both raw
  phone fields and expose only `***-***-NNNN`.

## Local evidence

Focused command:

```text
PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_magellan_collection.py
```

Result before candidate changes: `5 passed`.  
Result after candidate changes: `9 passed`.

No Magellan login, transcript, live client call, handled-state write, Test
deployment, report delivery, or Production action was performed.

## Required Reliability / QA checks

1. Run the repository's Linux verification gate against the exact candidate
   commit.
2. Exercise the exact immutable artifact on `hermes-test-01` with a read-only
   Magellan profile.
3. Prove these scenarios with stored evidence:
   - a target date spanning two pages, including a duplicate call ID;
   - a verified empty Sad result;
   - newest-first rows through the older-date boundary;
   - an intentionally unsorted fixture that fails closed;
   - `max_pages` exhaustion that writes no snapshot;
   - expired authentication or partial page replacement that writes no
     successful snapshot; and
   - management digest output containing no raw From/To number in a
     `sad_calls` row.
4. Spot-check report totals and source rows without opening call details or
   changing handled state.

Status remains `QA required` until the Test evidence and privacy review pass.
Rollback target is the current verified Test digest recorded immediately before
deployment; it must be captured by Reliability / QA rather than guessed here.
