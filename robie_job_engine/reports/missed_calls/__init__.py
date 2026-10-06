"""Missed Call Report automation.

Pipeline: RingCentral call-log pull (inbound, missed/voicemail) -> per-day
phone dedupe -> Monday rule (Fri/Sat/Sun separate) -> offline phone-match
against the EZLynx full-book phone index -> append-only write into the
"Missed Calls Report 2026" sheet's dated tabs (M/D).

Hard rules (see ASSUMPTIONS.md):
  * The "Was addressed?" callback determination is HUMAN-ONLY. This pipeline
    always leaves it blank and never presents a heuristic as a determination
    (Sandeep questions M8/M9/M10 pending; feasibility finding 2).
  * Phone lookup is offline against the phone index JSON. On ambiguity or a
    stale/missing index the pipeline FAILS CLOSED (lookup state
    "ambiguous"/"unavailable") and never writes "No Account" for a number it
    did not actually check (finding 1).
  * Sheet writes are APPEND-ONLY and never modify an existing row, so a
    human-filled row can never be overwritten (M13/M14).
"""
